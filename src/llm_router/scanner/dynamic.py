"""S2.10-0.5 · 动态 Scanner 编排器(轮询→diff→面试→入库→清退,侧挂循环)。

验收(specs/free-model-scanner Req「动态 diff 抓新免费模型」):每 h 轮询 NVIDIA/OpenRouter
→ diff vs 上次快照 → 新增免费模型自动面试 → 合格入库贴标 → 过期清退。本拆片串起 0.1-0.4
为完整编排 + 侧挂后台循环 + 候选池桥。

设计:
- `DynamicScanner.tick()`:单次轮询编排(纯逻辑,可单测)——
  1. poll_all(0.2)抓当前快照
  2. 对每 source:load_snapshot(0.3)取 prev → diff_snapshots(0.1)→ added/removed
  3. interview_batch(0.4)面 added,合格 → upsert_entry(0.3)入池 active
  4. removed → expire_entry(0.3)清退
  5. save_snapshot(0.3)存当前快照(下次 prev)
  返回 TickResult(每 source 的 added/removed/interviewed/passed/expired 计数,可观测)。
- `run_loop(stop_event)`:侧挂后台循环(同 health-probe 模式,stdlib asyncio while+sleep,
  每 interval;shutdown stop_event.set 优雅退出)。tick 异常不崩循环(记 log 继续下次)。
- `build_dynamic_adapters`:把 scanner.db active_models 转 OpenAIProvider 候选三元组
  (镜像 mnfst.build_adapters 接口,供 Phase B 接 app)。**Phase1 不自动接 production Cascade**
  (routing-change-safety:真实热入池留 Phase B,需先 propose 守门)。

红线(守 routing-change-safety + routing-priority-principle + ollama-qwen3-rules):
- 只读 GET 外部 API,不改路由配置
- 排序键字典序;动态 entry is_free=True/cost=0 与静态免费 provider 同档竞争;tier 只进能力匹配首槽
- 面试 probe 打远端免费 provider 自身,零本地 ollama 模型加载
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..providers.openai import OpenAIProvider
from ..config import ProviderEntry
from ..store.scanner_store import ScannerStore
from .interview import ProbeFn, interview_batch
from .pollers import Fetcher, poll_all
from .snapshot import DiscoveredModel, ScannerSource, Snapshot, diff_snapshots

_LOG = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 3600.0  # spec:每 h
_SOURCE_BASE_URL = {
    ScannerSource.NVIDIA: "https://integrate.api.nvidia.com/v1",
    ScannerSource.OPENROUTER: "https://openrouter.ai/api/v1",
}

# Phase B · D1:动态条目 ProviderEntry 默认值。is_free=True/cost=0 与静态免费 provider 同档竞争。
# quota 默认 500000(实现时按 source 调);cooldown_s=30;tier None → 降级 medium
# (ProviderEntry.tier Literal["strong","medium","fast"] 不接受 None)。
_DYNAMIC_QUOTA = 500000
_DYNAMIC_COOLDOWN_S = 30
_TIER_DEFAULT = "medium"

# R37 治本: 按 source 调 quota, 跟 L153 _SOURCE_BASE_URL.get(model.source, "") 模式同款.
# ScannerSource 仅 NVIDIA + OPENROUTER 2 成员 (snapshot.py:27-31), 真实查表.
# NVIDIA NIM 免费档 ~5000/h, OpenRouter free 档 ~1000000 (差异大, 写死不合理).
_DYNAMIC_QUOTA_BY_SOURCE: dict[ScannerSource, int] = {
    ScannerSource.NVIDIA: 5000,
    ScannerSource.OPENROUTER: 1000000,
}
_DYNAMIC_QUOTA_DEFAULT: int = _DYNAMIC_QUOTA  # fallback for future ScannerSource 成员


def _get_quota_for_source(source: ScannerSource) -> int:
    """R37 治本: 按 source 调 quota, 跟 _SOURCE_BASE_URL.get 同款查表模式.

    Returns:
        int: 该 source 的推荐 quota. 未知 source 走 _DYNAMIC_QUOTA_DEFAULT (跟 _DYNAMIC_QUOTA 一致, 500000).
    """
    return _DYNAMIC_QUOTA_BY_SOURCE.get(source, _DYNAMIC_QUOTA_DEFAULT)


def _dynamic_name(model: DiscoveredModel) -> str:
    """动态条目稳定唯一 name = `dyn-{source}-{flat_id}`(同 build_dynamic_adapters,守一致)。

    flat_id = model_id 去斜杠(防 "/" 在 CB key / entries dict key 里惹麻烦)。
    EpsilonGreedy._rank 按 name 查 entries dict,故 entry.name 必须等于候选三元组 name。
    """
    flat_id = model.model_id.replace("/", ":")
    return f"dyn-{model.source.value}-{flat_id}"


def dynamic_entry_to_provider_entry(model: DiscoveredModel) -> ProviderEntry:
    """单个动态 DiscoveredModel → ProviderEntry(进 EpsilonGreedy entries dict)。

    Phase B · D1:造 ProviderEntry 而非绕过 entries dict(EpsilonGreedy._rank 强依赖 entries,
    name 查不到 → missing 报错)。复用现有字典序排序键,不引入加权。

    字段:
      - name = `dyn-{source}-{flat_id}`(同 build_dynamic_adapters,守一致)
      - tier 从 model.tier 取(scanner.db 已贴标);None → 降级 medium
      - is_free=True / cost_multiplier=0.0(与静态免费 provider 同档竞争,守排序键字典序)
      - quota=500000(默认,R37 治本按 source 调: NVIDIA=5000, OPENROUTER=1000000)/ cooldown_s=30
      - 其余字段(base_url/api_key_env/model/entity)留空——动态 adapter 由 build_dynamic_adapters
        造,ProviderEntry 只供排序键 + TierMatcher,不参与 adapter 构造。
    """
    return ProviderEntry(
        name=_dynamic_name(model),
        tier=model.tier if model.tier is not None else _TIER_DEFAULT,
        quota=_get_quota_for_source(model.source),  # R37 治本: 按 source 查表, 替代 _DYNAMIC_QUOTA 写死
        cooldown_s=_DYNAMIC_COOLDOWN_S,
        is_free=True,
        cost_multiplier=0.0,
    )


def build_dynamic_entries(models: list[DiscoveredModel]) -> list[ProviderEntry]:
    """批量造 ProviderEntry,供 EpsilonGreedy entries dict 合并(Phase B · B1.2)。

    按 model_id 去重(同 source 同 model_id 仅保留第一条,跨 display_name 抖动稳定)。
    返回顺序按输入序(去重保留首现),路由选择归 EpsilonGreedy 字典序排序,不由此引入偏好。

    红线:与 build_dynamic_adapters 的 name 集合对齐——入候选池的 name 必须在 entries dict 里有
    对应 ProviderEntry,否则 _rank missing 报错。缺 key 的 source 在 build_dynamic_adapters 跳过,
    但 entry 仍造(无害:未入候选池的 entry 不会被 _rank 查到,留作审计/未来接入)。
    """
    seen: set[str] = set()
    entries: list[ProviderEntry] = []
    for m in models:
        key = f"{m.source.value}:{m.model_id}"
        if key in seen:
            continue
        seen.add(key)
        entries.append(dynamic_entry_to_provider_entry(m))
    return entries


def dynamic_policy_version(active_models: list[DiscoveredModel]) -> str:
    """从 active 模型集算 policy_version(Phase B · B3.2,D3 决:content-hash 非 mtime)。

    WAL 模式下 scanner.db 主文件 mtime 不稳定(写先进 WAL,主文件 mtime 延后 checkpoint),
    故用 active model_id 集合的 sha1 短摘要做 version:
      - 同 active 集 → 同 version → apply_policy noop(幂等,省一次候选池重建)
      - active 集变化 → version 变 → apply_policy 原子替换候选池

    空 active 集 → "scan-empty"(纯静态回退版本号)。
    """
    if not active_models:
        return "scan-empty"
    ids = sorted(m.model_id for m in active_models)
    h = hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:12]
    return f"scan-{h}"


def make_openai_probe_factory(
    *,
    nvidia_key: Optional[str] = None,
    openrouter_key: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> Callable[[DiscoveredModel], "ProbeFn"]:
    """造生产 probe_factory:每模型用 OpenAIProvider 打其 source 端点冒烟(Phase B · D6)。

    DynamicScanner 面试 added 模型时调此 factory(model) 产 probe;probe 打远端免费 provider
    自身(零本地 ollama 模型,守 ollama-qwen3-rules)。key 从 env/参数读(同 build_dynamic_adapters)。
    超时由 interview_model 的 wait_for(probe_timeout) 兜底,probe 自身不重复包。

    缺 key 的 source → probe 用空 key,complete() 抛 ProviderError → 面试失败 → 不入池(无害)。
    """
    import os

    environ = env if env is not None else os.environ
    key_by_source = {
        ScannerSource.NVIDIA: nvidia_key or environ.get("NVIDIA_API_KEY", ""),
        ScannerSource.OPENROUTER: openrouter_key or environ.get("OPENROUTER_API_KEY", ""),
    }

    def factory(model: DiscoveredModel) -> "ProbeFn":
        key = key_by_source.get(model.source, "")
        base_url = _SOURCE_BASE_URL.get(model.source, "")
        provider = OpenAIProvider(
            f"probe-{model.source.value}-{model.model_id.replace('/', ':')}",
            api_key=key,
            base_url=base_url,
            model=model.model_id,
        )

        async def probe(model_id: str) -> str:
            # scanner-interview-quality-gate:用指令遵循 prompt,interview_model 判 PONG。
            # complete 接收 messages 结构(chat-protocol-passthrough)+ 返 ChatResult。
            result = await provider.complete(
                [{"role": "user", "content": "Reply with exactly: PONG"}]
            )
            return result.content

        return probe

    return factory


@dataclass(frozen=True)
class SourceTickStats:
    """单 source 单次 tick 的可观测统计(供日志/审计/测试断言)。"""

    source: ScannerSource
    added: int = 0
    removed: int = 0
    interviewed: int = 0
    passed: int = 0
    expired: int = 0


@dataclass(frozen=True)
class TickResult:
    """单次 tick 的聚合结果(各 source 统计 + 是否出错)。"""

    ok: bool
    error: Optional[str] = None
    stats: dict[ScannerSource, SourceTickStats] = field(default_factory=dict)


class DynamicScanner:
    """动态 Scanner 编排器:轮询→diff→面试→入库→清退 + 侧挂循环。

    依赖注入(确定性测试 + 生产解耦):
      - store: ScannerStore(快照+条目持久化,0.3)
      - probe_factory: (DiscoveredModel) -> ProbeFn 或统一 ProbeFn;0.5 用它给 added 模型
        造面试 probe(打该模型 source 端点)。默认 None → 跳过面试(added 不入池,仅记快照;
        供"只 diff 不入池"的只读模式)。
      - fetcher: poller HTTP 注入(0.2,默认 httpx)。
      - nvidia_key / openrouter_key: poller key(默认读 env,0.2)。
    """

    def __init__(
        self,
        store: ScannerStore,
        *,
        probe_factory: Optional[Callable[[DiscoveredModel], ProbeFn]] = None,
        fetcher: Optional[Fetcher] = None,
        nvidia_key: Optional[str] = None,
        openrouter_key: Optional[str] = None,
        probe_timeout: float = 20.0,
        on_tick_complete: Optional[Callable[[TickResult], Awaitable[None]]] = None,
    ) -> None:
        self._store = store
        self._probe_factory = probe_factory
        self._fetcher = fetcher
        self._nvidia_key = nvidia_key
        self._openrouter_key = openrouter_key
        self._probe_timeout = probe_timeout
        # Phase B · B3.1:tick 有变更(added/expired>0)→ 调此回调(生产用 app 传 apply_policy 重建;
        # 测试注入 fake)。无变更不调。回调异常不崩 tick(记 log,同 run_loop 健壮性纪律)。
        self._on_tick_complete = on_tick_complete

    async def tick(self) -> TickResult:
        """单次轮询编排(可单测)。返回 TickResult(统计 + ok/error)。

        异常被捕获记 error,不抛(后台循环健壮);但 store 基础设施失败应暴露——
        故 store 调用不包 try(同 health-probe #2 纪律),只包 poll/interview 业务层。
        """
        try:
            current = await poll_all(
                fetcher=self._fetcher,
                nvidia_key=self._nvidia_key,
                openrouter_key=self._openrouter_key,
            )
        except Exception as exc:  # poll_all 内各 poll_* 已降级空,此处兜底防崩
            _LOG.error("DynamicScanner tick poll_all 失败: %s", exc)
            return TickResult(ok=False, error=f"poll:{type(exc).__name__}")

        stats: dict[ScannerSource, SourceTickStats] = {}
        for source, curr in current.items():
            stats[source] = await self._tick_source(source, curr)
        result = TickResult(ok=True, stats=stats)
        # Phase B · B3.1:有变更(added 或 expired>0)→ 触发候选池重建回调。
        # 无变更不调(apply_policy 同 version noop,省一次重建)。回调异常不崩 tick。
        if self._on_tick_complete is not None and self._has_pool_changes(stats):
            try:
                await self._on_tick_complete(result)
            except Exception as exc:
                _LOG.error("on_tick_complete 回调异常(不崩 tick): %s", exc)
        return result

    @staticmethod
    def _has_pool_changes(stats: dict[ScannerSource, SourceTickStats]) -> bool:
        """是否有入池/清退变更(added 或 expired>0)。供 B3.1 判是否触发重建回调。"""
        return any(s.added > 0 or s.expired > 0 for s in stats.values())

    async def _tick_source(self, source: ScannerSource, curr: Snapshot) -> SourceTickStats:
        """单 source 的 diff→面试→入库→清退。"""
        prev = await self._store.load_snapshot(source)
        prev_snap = prev if prev is not None else Snapshot.empty(source)
        diff = diff_snapshots(prev_snap, curr)

        added_list = sorted(diff.added, key=lambda m: m.model_id)
        removed_list = sorted(diff.removed, key=lambda m: m.model_id)

        passed_count = 0
        interviewed_count = 0
        if self._probe_factory is not None and added_list:
            # 逐模型造 probe(各模型 source 端点不同),用 adapter 统一成 probe(model_id)->str。
            results = await interview_batch(
                added_list,
                probe=_probe_factory_adapter(self._probe_factory, added_list),
                probe_timeout=self._probe_timeout,
            )
            interviewed_count = len(results)
            for r in results:
                if r.passed:
                    await self._store.upsert_entry(r.model, interview_passed=True)
                    passed_count += 1
                else:
                    # 面试失败的不入池(不入 active),但也不记条目(下次轮询仍发现会重试)。
                    _LOG.info("模型 %s 面试失败(%s),不入池", r.model.model_id, r.reason)

        # removed → 过期清退(只清 active 条目里的)
        expired_count = 0
        for m in removed_list:
            row = await self._store.get_entry(m.model_id)
            if row is not None and row.status == "active":
                await self._store.expire_entry(m.model_id)
                expired_count += 1

        # 存当前快照(下次 prev)
        await self._store.save_snapshot(curr)

        return SourceTickStats(
            source=source,
            added=len(added_list),
            removed=len(removed_list),
            interviewed=interviewed_count,
            passed=passed_count,
            expired=expired_count,
        )

    async def run_loop(self, stop_event: asyncio.Event, *, interval: float = _DEFAULT_INTERVAL) -> None:
        """侧挂后台循环:每 interval 秒 tick,stop_event.set 优雅退出。

        tick 异常已在 tick() 内捕获(返回 TickResult.ok=False),不崩循环。
        shutdown:stop_event 让循环在下个检查点退出;sleep 期被 wait_for(stop_event.wait())
        即时唤醒(同 health-probe.run_loop #3 设计)。
        """
        while not stop_event.is_set():
            try:
                result = await self.tick()
                if not result.ok:
                    _LOG.warning("DynamicScanner tick 失败: %s", result.error)
                else:
                    for s, st in result.stats.items():
                        _LOG.info(
                            "scanner tick %s: +%(added)d -%(removed)d 面试%(interviewed)d 通过%(passed)d 清退%(expired)d",
                            s.value,
                        )
            except Exception as exc:  # 兜底:tick 应自处理,但 store 调用可能抛
                _LOG.error("DynamicScanner run_loop tick 异常(不崩): %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass  # 正常:到点下一轮


def _probe_factory_adapter(
    factory: Callable[[DiscoveredModel], ProbeFn],
    models: list[DiscoveredModel],
) -> ProbeFn:
    """把 probe_factory(model)->ProbeFn 适配成 interview_batch 要的 probe(model_id)->str。

    interview_batch 对每个 model 调 probe(model.model_id);这里按 model_id 路由到对应
    factory(model) 产出的 probe。models 必须含所有会被 probe 的 model_id。
    """
    by_id = {m.model_id: factory(m) for m in models}

    async def probe(model_id: str) -> str:
        return await by_id[model_id](model_id)

    return probe


def build_dynamic_adapters(
    models: list[DiscoveredModel],
    *,
    nvidia_key: Optional[str] = None,
    openrouter_key: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> list[tuple[str, OpenAIProvider, str]]:
    """active_models → 真 OpenAIProvider 候选三元组(镜像 mnfst.build_adapters 接口)。

    **Phase1 不自动接 production Cascade**(routing-change-safety:真实热入池留 Phase B,
    需先 /opsx:propose 把守门写进 tasks)。本函数只产出三元组供 Phase B / 测试 / 审计用。

    每 model 造一个 OpenAIProvider(name=source:model_id 去斜杠,model_id 作 model 名,
    base_url 按 source 取,key 从 env/参数读)。缺 key 的 source 跳过(不崩,同 mnfst 模式)。
    account_key = f"{SOURCE}_API_KEY"(stable,日志/熔断记账用,非 secret 本身)。

    红线:is_free=True/cost=0 的动态 entry 与静态免费 provider 同档竞争;排序键字典序不变。
    """
    import os

    environ = env if env is not None else os.environ
    key_by_source = {
        ScannerSource.NVIDIA: nvidia_key or environ.get("NVIDIA_API_KEY", ""),
        ScannerSource.OPENROUTER: openrouter_key or environ.get("OPENROUTER_API_KEY", ""),
    }
    candidates: list[tuple[str, OpenAIProvider, str]] = []
    for m in models:
        key = key_by_source.get(m.source, "")
        if not key:
            continue  # 该 source 缺 key → 跳过(渐进接入,不崩)
        base_url = _SOURCE_BASE_URL.get(m.source)
        if not base_url:
            continue  # 未知 source(未来扩展)→ 跳过
        # name 稳定且唯一:source + 去斜杠 model_id(防 "/" 在 CB key 里惹麻烦)。
        # 与 dynamic_entry_to_provider_entry 同 _dynamic_name,守 entries dict 对齐。
        name = _dynamic_name(m)
        provider = OpenAIProvider(name, api_key=key, base_url=base_url, model=m.model_id)
        account_key = f"{m.source.value.upper()}_API_KEY"
        candidates.append((name, provider, account_key))
    return candidates
