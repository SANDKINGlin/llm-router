"""三层级联熔断器 (S1.6) — 派生模型(选项 A)。

按 `specs/resilience-cascade/spec.md` + 用户确认的 Gap1/Gap2/Gap3:
  - Gap1:单 key **连续 3 次硬失败** → 该 key OPEN。
  - Gap3(number-free):provider/global 是**派生聚合**(从 key 状态算),无独立状态机:
        provider OPEN ⟺ 该 provider 至少 1 个 key 且全部 key OPEN;
        global  OPEN ⟺ 至少 1 个 provider 且全部 provider OPEN(→ 冻结下层)。
  - Gap2:半开退避 min(30×2ⁿ, 300) → 30/60/120/240/300。
  - jitter:30s + 0~15s。
  - 内容完整性(3 软 = 1 硬):详见 content_integrity.py,SOFT_CONTENT 由 caller 标记。

只有 **key** 有真实状态机(CLOSED/OPEN/HALF_OPEN);provider/global 在读取时派生,
零持久化漂移(最贴合 Gap3 的 number-free 本意)。

key 状态机:CLOSED →[3 连续硬失败]→ OPEN(等 next_probe_at)→[到期]→ HALF_OPEN(放 1 探测)
         → 成功→CLOSED(清计数)/ 失败→OPEN(窗口翻倍)。
allow 判定顺序(global→provider→key = 恢复优先级,spec locked):
  global 派生 OPEN → 拒(global_open);否则按 key 自身状态判定。
  (provider 不独立阻塞请求——它只是 global 冻结的输入;provider 的 key 各自熔断。)

恢复传播(自下而上,record_success 触发):key CLOSED → 派生 provider 自动 CLOSED
  → 派生 global 自动 CLOSED。无需显式级联 close。

HERMES 设计审 [CONSENSUS](2026-06-15) + 自查修正:
  - global 无 timer 自动恢复(灾难态;但因派生,当全部 key 经探测恢复 CLOSED 时自然解冻
    ——这是"全部真实恢复"的最强信号,非 premature)。
  - 半开只放 1 探测(probe_in_flight);崩溃恢复时清零(防 half_open 死锁)。
  - 空 provider 不 trip。
  - 软→硬换算整数除法(// ratio)防浮点漂移。
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class TripReason(str, Enum):
    HARD = "hard"  # 超时/5xx/网络错误
    SOFT_CONTENT = "soft_content"  # 内容完整性软失败(详见 content_integrity.py)


@dataclass
class KeyState:
    """key 级状态(唯一拥有真实状态机的层级)。"""
    state: CircuitState = CircuitState.CLOSED
    hard_failures: int = 0  # 连续硬失败(成功清零);HARD 直接 +1,SOFT 按 ratio 换算
    soft_failures: int = 0  # 未换算的余数软失败(0..ratio-1)
    half_open_failures: int = 0  # 半开连续失败(驱动退避窗口翻倍)
    opened_at: Optional[float] = None  # 进 OPEN 时刻
    next_probe_at: Optional[float] = None  # 允许转 HALF_OPEN 的时刻
    probe_in_flight: bool = False  # 半开窗口内是否已放 1 探测(防并发惊群)


@dataclass
class ProviderState:
    """provider 派生快照(只读,由 key 状态计算;不持久化)。"""
    state: CircuitState = CircuitState.CLOSED
    opened_at: Optional[float] = None


@dataclass
class GlobalState:
    """global 派生快照(只读;不持久化)。"""
    state: CircuitState = CircuitState.CLOSED
    opened_at: Optional[float] = None


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: Optional[int] = None


def _default_jitter(jitter_seconds: int) -> Callable[[], float]:
    def _fn() -> float:
        # secrets.randbelow(jitter_seconds+1) ∈ [0, jitter_seconds];防侧信道时序
        return float(secrets.randbelow(jitter_seconds + 1))

    return _fn


class CircuitBreaker:
    """三层级联熔断器(派生模型)。持久化仅 key 级;provider/global 读取时派生。"""

    def __init__(
        self,
        db_path: Path,
        key_hard_threshold: int = 3,
        soft_to_hard_ratio: int = 3,
        base_backoff_seconds: int = 30,
        jitter_seconds: int = 15,
        backoff_cap_seconds: int = 300,
    ):
        self.db_path = Path(db_path)
        self.key_hard_threshold = key_hard_threshold
        self.soft_to_hard_ratio = soft_to_hard_ratio
        self.base_backoff_seconds = base_backoff_seconds
        self.jitter_seconds = jitter_seconds
        self.backoff_cap_seconds = backoff_cap_seconds

        self._keys: dict[tuple[str, str], KeyState] = {}

        # 测试钩子:clock/jitter 可注入确定值(生产用 time.time / secrets)
        self._now_override: Optional[float] = None
        self._jitter_fn: Callable[[], float] = _default_jitter(jitter_seconds)

        self._init_db()
        self._load_state()

    # ---------- time / window helpers ----------

    def _now(self) -> float:
        return self._now_override if self._now_override is not None else time.time()

    def _recovery_window(self, half_open_failures: int) -> float:
        """Gap2:min(30 × 2^n, 300) → 30/60/120/240/300。"""
        return float(
            min(
                self.base_backoff_seconds * (2 ** half_open_failures),
                self.backoff_cap_seconds,
            )
        )

    def _next_probe_or_far(self, after: float, provider: str, key: str) -> float:
        """测试辅助:返回"能进半开的下一时刻"(next_probe_at 之后一点)。"""
        ks = self._keys.get((provider, key))
        if ks and ks.next_probe_at is not None:
            return max(after, ks.next_probe_at) + 1.0
        return after + 1.0

    # ---------- persistence (keys only) ----------

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS circuit_keys (
                    provider             TEXT NOT NULL,
                    key                  TEXT NOT NULL,
                    state                TEXT NOT NULL,
                    hard_failures        INTEGER NOT NULL DEFAULT 0,
                    soft_failures        INTEGER NOT NULL DEFAULT 0,
                    half_open_failures   INTEGER NOT NULL DEFAULT 0,
                    opened_at            REAL,
                    next_probe_at        REAL,
                    probe_in_flight      INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (provider, key)
                );
                """
            )

    def _load_state(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute(
                "SELECT provider, key, state, hard_failures, soft_failures, "
                "       half_open_failures, opened_at, next_probe_at, probe_in_flight "
                "FROM circuit_keys"
            ):
                p, k, st, hf, sf, hof, oa, npa, pinf = row
                # 崩溃恢复:probe_in_flight 是进程内瞬态标志。若上次进程在
                # allow(放探测) 与 record_* 之间崩溃,持久化的 probe_in_flight=True
                # 会导致永远 half_open_busy(死锁)→ HALF_OPEN 加载时清零。
                state = CircuitState(st)
                self._keys[(p, k)] = KeyState(
                    state=state,
                    hard_failures=hf,
                    soft_failures=sf,
                    half_open_failures=hof,
                    opened_at=oa,
                    next_probe_at=npa,
                    probe_in_flight=False if state == CircuitState.HALF_OPEN else bool(pinf),
                )

    def _persist_key(self, provider: str, key: str, ks: KeyState) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO circuit_keys(provider,key,state,hard_failures,
                   soft_failures,half_open_failures,opened_at,next_probe_at,probe_in_flight)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider,key) DO UPDATE SET
                     state=excluded.state,
                     hard_failures=excluded.hard_failures,
                     soft_failures=excluded.soft_failures,
                     half_open_failures=excluded.half_open_failures,
                     opened_at=excluded.opened_at,
                     next_probe_at=excluded.next_probe_at,
                     probe_in_flight=excluded.probe_in_flight
                """,
                (
                    provider,
                    key,
                    ks.state.value,
                    ks.hard_failures,
                    ks.soft_failures,
                    ks.half_open_failures,
                    ks.opened_at,
                    ks.next_probe_at,
                    int(ks.probe_in_flight),
                ),
            )

    # ---------- derived aggregates (Gap3 number-free) ----------

    def _providers_with_keys(self) -> set[str]:
        return {p for (p, _k) in self._keys}

    def _provider_keys(self, provider: str) -> list[KeyState]:
        return [ks for (p, _k), ks in self._keys.items() if p == provider]

    def _provider_is_open(self, provider: str) -> bool:
        """Gap3:该 provider 至少 1 个 key,且全部 key 都 OPEN。"""
        keys = self._provider_keys(provider)
        return bool(keys) and all(ks.state == CircuitState.OPEN for ks in keys)

    def _global_is_open(self) -> bool:
        """Gap3:至少 1 个 provider(以"有 key 的 provider"为准),且全部 provider 都 OPEN。"""
        providers = self._providers_with_keys()
        if not providers:
            return False
        return all(self._provider_is_open(p) for p in providers)

    # ---------- core API ----------

    def record_failure(self, provider: str, key: str, reason: TripReason) -> None:
        ks = self._keys.setdefault((provider, key), KeyState())
        now = self._now()

        # 半开探测失败 → 重开 + 窗口翻倍(Gap2)
        if ks.state == CircuitState.HALF_OPEN:
            ks.half_open_failures += 1
            ks.probe_in_flight = False
            window = self._recovery_window(ks.half_open_failures)
            ks.state = CircuitState.OPEN
            ks.opened_at = now
            ks.next_probe_at = now + window + self._jitter_fn()
            self._persist_key(provider, key, ks)
            return

        # CLOSED(或 OPEN 再失败)累计硬失败计数
        if reason == TripReason.SOFT_CONTENT:
            ks.soft_failures += 1
            # 整数除法换算(HERMES [CONSENSUS] 防浮点漂移):每 ratio 软 = 1 硬
            ks.hard_failures += ks.soft_failures // self.soft_to_hard_ratio
            ks.soft_failures = ks.soft_failures % self.soft_to_hard_ratio
        else:  # HARD
            ks.hard_failures += 1

        # 达硬阈值 → key OPEN(Gap1)
        if ks.hard_failures >= self.key_hard_threshold and ks.state != CircuitState.OPEN:
            ks.state = CircuitState.OPEN
            ks.opened_at = now
            ks.half_open_failures = 0
            ks.next_probe_at = now + self._recovery_window(0) + self._jitter_fn()
        self._persist_key(provider, key, ks)
        # provider/global 派生,无需显式级联 trip。

    def record_success(self, provider: str, key: str) -> None:
        """成功记录:key 状态机闭环。provider/global 派生,自动随 key 恢复。"""
        ks = self._keys.get((provider, key))
        if ks is None:
            return
        if ks.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            ks.state = CircuitState.CLOSED
            ks.hard_failures = 0
            ks.soft_failures = 0
            ks.half_open_failures = 0
            ks.opened_at = None
            ks.next_probe_at = None
            ks.probe_in_flight = False
        else:  # CLOSED:连续成功重置计数
            ks.hard_failures = 0
            ks.soft_failures = 0
        self._persist_key(provider, key, ks)

    def allow_request(self, provider: str, key: str) -> Decision:
        """判定(global→key;provider 不独立阻塞,仅作 global 输入)。

        global 派生 OPEN → 冻结(global_open);
        key OPEN 且未到期 → 拒(key_open);到期 → HALF_OPEN 放 1 探测;
        HALF_OPEN 已放探测 → 拒(half_open_busy);CLOSED → 放行。
        """
        now = self._now()

        # global 冻结(派生)
        if self._global_is_open():
            return Decision(False, "global_open")

        # key 状态机
        ks = self._keys.get((provider, key))
        if ks is None or ks.state == CircuitState.CLOSED:
            return Decision(True, "")
        if ks.state == CircuitState.OPEN:
            if ks.next_probe_at is not None and now >= ks.next_probe_at:
                ks.state = CircuitState.HALF_OPEN
                ks.probe_in_flight = True
                self._persist_key(provider, key, ks)
                return Decision(True, "key_half_open_probe")
            retry = max(0, int((ks.next_probe_at or now) - now))
            return Decision(False, "key_open", retry)
        # HALF_OPEN
        if ks.probe_in_flight:
            return Decision(False, "half_open_busy")
        ks.probe_in_flight = True
        self._persist_key(provider, key, ks)
        return Decision(True, "key_half_open_probe")

    # ---------- read-only (provider/global 派生) ----------

    def get_key_state(self, provider: str, key: str) -> KeyState:
        return self._keys.get((provider, key), KeyState())

    def get_provider_state(self, provider: str) -> ProviderState:
        """派生:全部 key OPEN → OPEN(opened_at = 最早 OPEN key 的 opened_at);否则 CLOSED。"""
        keys = self._provider_keys(provider)
        if keys and all(ks.state == CircuitState.OPEN for ks in keys):
            opened = next((ks.opened_at for ks in keys if ks.opened_at is not None), None)
            return ProviderState(state=CircuitState.OPEN, opened_at=opened)
        return ProviderState(state=CircuitState.CLOSED)

    def get_global_state(self) -> GlobalState:
        """派生:全部 provider OPEN → OPEN;否则 CLOSED。"""
        if self._global_is_open():
            opened = next(
                (ks.opened_at for ks in self._keys.values() if ks.opened_at is not None),
                None,
            )
            return GlobalState(state=CircuitState.OPEN, opened_at=opened)
        return GlobalState(state=CircuitState.CLOSED)
