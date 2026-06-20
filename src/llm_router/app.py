"""FastAPI 应用:双协议入口 + /healthz。

Phase1 (S2.1b):/v1/chat/completions(OpenAI)+ /v1/messages(Anthropic)
经 Cascade(④ 回退编排)打到候选 provider 链。Phase1 候选只有 mock(router-policy.yaml),
S2.x 接真 provider 时由 Scanner(S2.3)按 entry.base_url/api_key_env 建真 adapter 填候选。
门卫/匹配/路由/熔断在 Cascade 内串起(store+breaker+hop+完整性+strategy.plan)。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .api.cascade import Cascade
from .api.cost_gate import CostGate
from .api.epsilon_greedy import EpsilonGreedy
from .api.gray import derive_session_id
from .api.policy_enforcer import PolicyEnforcer
from .config import policy
from .health.probe import HealthProber
from .providers.base import Provider
from .providers.mock import MockProvider
from .resilience.circuit_breaker import CircuitBreaker
from .scanner.dynamic import (
    DynamicScanner,
    build_dynamic_adapters,
    build_dynamic_entries,
    dynamic_policy_version,
    make_openai_probe_factory,
)
from .scanner.mnfst import build_adapters, load_manifest
from .store.health_store import HealthStore
from .store.scanner_store import ScannerStore, load_active_models_sync
from .store.token_ledger import LedgerStore
from .store.trace import TraceStore

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_SCANNER_DB = _DATA_DIR / "scanner.db"


def _build_three_layer_candidates(
    pol,
    manifest_entries,
) -> tuple[dict, list]:
    """构造三层候选池(entries dict + candidates list):静态真 → 动态 → mock(Phase B · B2.1)。

    单一候选构造源(供 _build_cascade + admin_rollback 复用,DRY 防漂移):两者必须产同形
    candidates,否则 rollback 会静默丢/加动态条目。

    entries dict:policy(mock)+ manifest(真)+ 动态(Phase B),供 EpsilonGreedy._rank 排序键
    + TierMatcher。动态 entry 进 entries(供 _rank 按 name 查到),否则 _rank missing 报错。

    candidates:``[*real_adapters, *dynamic_candidates, *mock_candidates]``。
    顺序关键(修 B1 mock 支配 + D2 动态放静态后 mock 前):三者排序键全平局时 plan() 稳定排序
    保持插入序 → 静态真 → 动态 → mock。

    Phase B 灰度守门(B4.1):gray_percent=0 → 不加载动态(纯静态+mock,向后兼容两层)。
    scanner.db 不存在/空/读失败 → 无动态(同向后兼容)。

    红线:动态 is_free=True/cost=0 与静态免费 provider 同档竞争;排序键字典序不变。
    """
    entries = {e.name: e for e in (*pol.providers, *manifest_entries)}
    real_adapters = build_adapters(manifest_entries)  # 配了 key 的真 adapter
    mock_candidates = [(e.name, MockProvider(), e.name) for e in pol.providers]

    # Phase B · B2.1:动态候选池热入。gray_percent=0(禁用)或 scanner.db 无 active → 无动态。
    dynamic_candidates: list = []
    if pol.gray_percent > 0:
        active_models = load_active_models_sync(_SCANNER_DB)
        if active_models:
            dynamic_entries = build_dynamic_entries(active_models)
            entries.update({e.name: e for e in dynamic_entries})
            dynamic_candidates = build_dynamic_adapters(
                active_models,
                nvidia_key=os.environ.get("NVIDIA_API_KEY"),
                openrouter_key=os.environ.get("OPENROUTER_API_KEY"),
            )

    candidates: list = [*real_adapters, *dynamic_candidates, *mock_candidates]
    return entries, candidates


def _build_cascade() -> Cascade:
    """构造生产 Cascade(模块级单例):三层候选池 静态真→动态→mock(Phase B · B2.1)。

    候选构造委托 ``_build_three_layer_candidates``(单一源,与 admin_rollback 同形)。
    详见该函数 docstring。

    **每次调用读最新 manifest + env + scanner.db**,供
    test_app_build_cascade_orders_real_before_mock 注入临时 manifest 后验证顺序。
    S2.8c:注入共享 HealthStore(data/health.db)——Cascade 路由前 hard-skip 死亡 key(Face 2),
    lifespan 起探活循环写它 + 喂 CB(Face 1/3)。Cascade 不 init(fail-open 读),lifespan init。
    """
    pol = policy()
    manifest_entries = load_manifest()
    entries, candidates = _build_three_layer_candidates(pol, manifest_entries)

    # S2.7 合规门卫:候选 entries(含 mock + 动态)→ 别名归一化 + 同 provider 多账号检测。
    # 动态 entry api_key_env=None(无账号)→ 不参与多账号检测(同 mock),合规放行。
    enforcer = PolicyEnforcer(entries.values())

    # S2.4 Cost Budget Gate:共享 ledger(Cascade writer + CostGate reader 同一实例)+
    # quotas 从 entries 取(ProviderEntry.quota,token 上限)。mock quota=1000000 → 永不超预算兜底。
    ledger = LedgerStore(_DATA_DIR / "ledger.db")
    quotas = {e.name: e.quota for e in entries.values()}
    cost_gate = CostGate(ledger, quotas)

    return Cascade(
        store=TraceStore(_DATA_DIR / "trace.db"),
        breaker=CircuitBreaker(_DATA_DIR / "circuit.db"),
        strategy=EpsilonGreedy(entries),
        candidates=candidates,
        health_store=HealthStore(_DATA_DIR / "health.db"),
        policy_enforcer=enforcer,
        ledger=ledger,
        cost_gate=cost_gate,
    )


_cascade = _build_cascade()


def _refresh_and_apply(
    cascade: Cascade,
    entries: dict,
    candidates: list,
    policy_version: str,
) -> bool:
    """同步刷新 strategy/cost_gate/enforcer + cascade.apply_policy(Phase B · B3.2)。

    单一刷新源(供 /admin/rollback + 动态重建回调复用,DRY 防漂移):两者必须同形刷新,否则
    重建后 strategy entries stale → _rank missing。apply_policy 同 version noop(幂等)。
    ponytail:各组件自己 refresh(职责分离,同 admin_rollback 注释)。
    """
    cascade._strategy.refresh_entries(entries)  # type: ignore[union-attr]
    cascade._cost_gate.update_quotas({e.name: e.quota for e in entries.values()})  # type: ignore[union-attr]
    cascade._policy_enforcer.rebuild(entries.values())  # type: ignore[union-attr]
    return cascade.apply_policy(candidates, policy_version)


def _make_rebuild_callback(cascade: Cascade, store: ScannerStore):
    """Phase B · B3.2:DynamicScanner.tick 有变更 → 重读 active → apply_policy 原子重建候选池。

    回调内:重读 policy/manifest + scanner.db active → 三层 candidates → refresh+apply_policy
    (version = active 集合 content-hash,同集 noop,守 D3)。复用 _build_three_layer_candidates
    (单一候选构造源,与 _build_cascade/admin_rollback 同形)。

    回调异常被 DynamicScanner.tick 捕获(B3.1),不崩 tick/run_loop;此处不额外包 try。
    """
    async def rebuild(_result) -> None:
        pol = policy()
        manifest_entries = load_manifest()
        entries, candidates = _build_three_layer_candidates(pol, manifest_entries)
        active = await store.active_models()
        version = dynamic_policy_version(active)
        _refresh_and_apply(cascade, entries, candidates, version)

    return rebuild


def _production_scanner_factory(cascade: Cascade, store: ScannerStore):
    """Phase B · D6:构造生产 DynamicScanner(run_loop 每 h tick + on_tick_complete 重建)。

    无 key(NVIDIA + OPENROUTER 都缺)→ 返 None(不起 run_loop,无谓空转,同 probe 无目标纪律)。
    probe_factory 用 make_openai_probe_factory(打远端免费 provider 自身,零本地模型)。
    on_tick_complete = _make_rebuild_callback(tick 有变更时原子重建候选池)。
    """
    nv = os.environ.get("NVIDIA_API_KEY")
    orr = os.environ.get("OPENROUTER_API_KEY")
    if not (nv or orr):
        return None
    return DynamicScanner(
        store,
        probe_factory=make_openai_probe_factory(),
        nvidia_key=nv,
        openrouter_key=orr,
        on_tick_complete=_make_rebuild_callback(cascade, store),
    )

# S2.8c 探活目标:真 provider(排 Mock——mock 探活恒活无信号)。模块级算一次(import 期,
# 与 _cascade 同读一次 manifest/env,一致)。spec Req 1 ping 全部 fallback/paid key;
# Phase1 provider 少,全 ping(不取"前 2",YAGNI;key 多时再限)。
_probe_targets: list[tuple[str, Provider]] = [
    (name, provider) for name, provider, _key in build_adapters(load_manifest())
]


def _make_lifespan(
    cascade_resolver: "Callable[[], Cascade] | Cascade",
    probe_targets_resolver: "Callable[[], list[tuple[str, Provider]]] | list[tuple[str, Provider]]",
    *,
    interval_seconds: float = 300.0,
    probe_timeout_seconds: float = 10.0,
    scanner_factory_resolver: "Optional[Callable[[], Callable[[Cascade, ScannerStore], Optional[DynamicScanner]]]]" = None,
    scanner_interval_seconds: float = 3600.0,
):
    """S2.8c Face 1 + Phase B · B3.2:构造 FastAPI lifespan——startup 起探活循环 + 动态 Scanner
    run_loop,shutdown 停。

    抽成工厂(非模块级闭包)以便单测注入 tmp cascade/targets/scanner 确定性验证 task 生命周期
    (不依赖 TestClient 是否跑 lifespan)。startup:init 共享 health_store + create_task
    prober.run_loop(stop)(on_alive=cascade.feed_probe_success 喂 HALF_OPEN,Face 3);
    **仅当有探活目标才起 task**(无真 key → 空转无意义)。shutdown:stop_event.set + cancel
    task + store.close。

    Phase B · B3.2(动态 Scanner run_loop):scanner_factory_resolver 为 zero-arg callable,返回
    factory ``(cascade, store) -> DynamicScanner | None``;返 None(无 key/禁用)→ 不起 scanner
    task。startup:init ScannerStore → factory(cascade, store) → create_task(ds.run_loop(stop))。
    shutdown:stop_event.set(共享,probe+scanner 同一 stop)+ await scanner task + store.close。
    同 health-probe 模式(不 task.cancel,await 优雅退出)。scanner_interval_seconds 透传 run_loop interval。

    S1.0 修复(2026-06-19,caveat 2 闭合):接 callable resolver(或裸 cascade/list 兼容旧测试)
    替代闭包硬绑;production 仍传 ``lambda: _cascade`` 行为不变(每次 startup 解析当前 attr),
    测试 monkeypatch ``_cascade = test_cascade`` 后 lifespan 解析到 test_cascade,不再污染
    production data/health.db。原 caveat:`_cascade = _build_cascade()` import 期构造,
    `lifespan=_make_lifespan(_cascade, ...)` 闭包硬绑该实例,测试 `with TestClient(app)`
    会触发 startup 调 `cascade.health_store.init()` 写 production health.db。
    """

    def _resolve_cascade() -> "Cascade":
        return cascade_resolver() if callable(cascade_resolver) else cascade_resolver

    def _resolve_targets() -> "list[tuple[str, Provider]]":
        return (
            probe_targets_resolver()
            if callable(probe_targets_resolver)
            else probe_targets_resolver
        )

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        cascade = _resolve_cascade()
        probe_targets = _resolve_targets()
        store = cascade.health_store
        if store is not None:
            await store.init()
        stop_event = asyncio.Event()
        task = None
        if probe_targets:
            prober = HealthProber(
                store,
                probe_targets,
                interval_seconds=interval_seconds,
                probe_timeout_seconds=probe_timeout_seconds,
                on_alive=cascade.feed_probe_success,
            )
            task = asyncio.create_task(prober.run_loop(stop_event))
        app.state.probe_stop = stop_event
        app.state.probe_task = task
        # Phase B · B3.2:动态 Scanner run_loop(tick → 面试入池 → on_tick_complete 重建候选池)。
        scanner_task = None
        scanner_store: Optional[ScannerStore] = None
        if scanner_factory_resolver is not None:
            factory = scanner_factory_resolver()
            if factory is not None:
                scanner_store = ScannerStore(_SCANNER_DB)
                await scanner_store.init()
                scanner = factory(cascade, scanner_store)
                if scanner is not None:
                    scanner_task = asyncio.create_task(
                        scanner.run_loop(stop_event, interval=scanner_interval_seconds)
                    )
        app.state.scanner_task = scanner_task
        app.state.scanner_store = scanner_store
        try:
            yield
        finally:
            stop_event.set()
            if task is not None:
                # 优雅退出:stop_event 让 run_loop 在下个循环检查点退出(probe.py #3 设计)。
                # **不 task.cancel()**——避免在 record_probe 的 DB 写中途注入 CancelledError
                # (probe.py #3 对抗审结论)。await task 等其退出:sleep 期被 wait_for(stop_event.wait())
                # 即时唤醒,最坏等完一个 in-flight tick(≤ probe_timeout × providers,Phase1 秒级)。
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if scanner_task is not None:
                # 同 probe 优雅退出:stop_event 唤醒 run_loop sleep,await 等其退出。
                # DynamicScanner.run_loop tick 异常自处理(不崩循环),await 不会抛。
                try:
                    await scanner_task
                except asyncio.CancelledError:
                    pass
            if scanner_store is not None:
                await scanner_store.close()
            if store is not None:
                await store.close()

    return _lifespan


app = FastAPI(
    title="llm-router",
    version="0.0.1",
    # S1.0 caveat 2 修复:传 callable 解析当前 module attr(每次 startup 解析),
    # 测试 monkeypatch.setattr(app_mod, "_cascade", tmp) 后 lifespan 跑用 tmp,不污染 production data。
    # Phase B · B3.2:scanner_factory_resolver 传 _production_scanner_factory(无 key → 返 None 不起 task)。
    lifespan=_make_lifespan(
        lambda: _cascade,
        lambda: _probe_targets,
        scanner_factory_resolver=lambda: _production_scanner_factory,
    ),
)


class _Message(BaseModel):
    role: str = "user"
    content: str | list | None = None


class _OpenAIRequest(BaseModel):
    model: str = "mock"
    messages: list[_Message] = Field(default_factory=list)
    # stream/tools/temperature 等:S2.x 接真 provider 时处理


class _AnthropicRequest(BaseModel):
    model: str = "mock"
    messages: list[_Message] = Field(default_factory=list)
    max_tokens: int | None = None


def _extract_prompt(messages: list[_Message]) -> str:
    """拍平 messages 成一个 prompt 串给 Cascade。"""
    parts = []
    for m in messages:
        c = m.content if isinstance(m.content, str) else str(m.content)
        parts.append(f"{m.role}: {c}")
    return "\n".join(parts) or "ping"


def _extract_session_id(request: Request) -> str | None:
    """S4.1:从请求派生 session_id(D9 灰度切 agent,design line 25/128)。

    优先级:X-Session-Id header(显式)> Authorization Bearer key 派生 > None。
    api_key 派生 = blake2b(key) → 同 key 同桶 = 天然按 agent 灰度(三 agent 各自 key 不同桶)。
    空串等同缺失。两者皆无 → None(Cascade 视为不参与灰度判定,不 log)。
    """
    explicit = request.headers.get("x-session-id") or None
    auth = request.headers.get("authorization", "")
    api_key: str | None = None
    # Bearer 解析:split(None) 容忍 SP/HTAB 分隔(RFC 用 SP,实践有 tab;OpenCode LOW#2)。
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        api_key = parts[1].strip() or None
    return derive_session_id(api_key, explicit)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """就绪探针。S0.0:返 200。readiness 切片补三库可写+policy加载+CB恢复。"""
    from .readiness import check_ready

    ok, detail = check_ready()
    return JSONResponse(
        {"status": "ok" if ok else "not_ready", "detail": detail},
        status_code=200 if ok else 503,
    )


@app.post("/v1/chat/completions")
async def openai_chat(req: _OpenAIRequest, request: Request) -> dict:
    """OpenAI 协议入口。经 Cascade(④)回退编排打到候选 provider 链。Roo/Codex 走这个。

    S2.1b:接 Cascade(prompt → strategy.plan 链 → 逐跳 complete + 熔断/完整性/幂等/hop)。
    S4.1:从 X-Session-Id / Authorization 派生 session_id 传 Cascade(灰度判定,可观测)。
    """
    result = await _cascade.run(
        _extract_prompt(req.messages),
        correlation_id=uuid.uuid4().hex,
        session_id=_extract_session_id(request),
    )
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": result.final_model or "mock",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": result.final_text or ""}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/messages")
async def anthropic_messages(req: _AnthropicRequest, request: Request) -> dict:
    """Anthropic 协议入口。经 Cascade(④)回退编排。CC 走这个。

    S2.1b:接 Cascade。S4.1:从 X-Session-Id / Authorization 派生 session_id 传 Cascade。
    """
    result = await _cascade.run(
        _extract_prompt(req.messages),
        correlation_id=uuid.uuid4().hex,
        session_id=_extract_session_id(request),
    )
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": result.final_model or "mock",
        "content": [{"type": "text", "text": result.final_text or ""}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }



# ── S4.3 · 应急回滚端点 ─────────────────────────────────────────────────────


class _RollbackRequest(BaseModel):
    """S4.3:/admin/rollback body。policy_version 必须 == policy().policy_version(灰度一致 guard)。

    字段含义:操作方已 revert policy.yaml 到目标版本;端点收到后比 body 的 version 与
    当前 policy().policy_version,一致才执行(防"操作方以为回了但 yaml 还没生效"的隐式不一致)。
    """

    policy_version: str = Field(..., description="回滚目标版本号(必须 == 当前 policy().policy_version)")


def _admin_guard() -> None:
    """S4.3 占位鉴权(D7 TODO:真 admin token / RBAC)。fail-closed:任何调用一律 403。

    OpenCode 节点 1 [HIGH] 决策:不留空 body,直接把端点锁住,防团队忘记加鉴权就让
    /admin/rollback 上线(D7 才会替换此 guard)。
    """
    raise HTTPException(
        status_code=403,
        detail="admin endpoint disabled (D7 TODO: real auth — token/RBAC)",
    )


@app.post("/admin/rollback")
def admin_rollback(req: _RollbackRequest, _admin: None = Depends(_admin_guard)) -> dict:
    """S4.3:policy 回滚状态同步端点。需 admin 鉴权(D7 TODO)。

    流程(应用层编排,职责分离——cascade 只管 CB+candidate,strategy/cost_gate/enforcer
    各自 refresh):
      1. 鉴权:_admin_guard 直接 403(fail-closed 占位)
      2. 灰度一致 guard:body.policy_version 必须 == policy().policy_version
      3. 重新读 manifest + policy → 构造新 candidates 与 entries 字典
      4. 同步刷新 strategy.refresh_entries / cost_gate.update_quotas / enforcer.rebuild
      5. cascade.apply_policy(new_candidates, policy_version)

    Returns:
        {"applied": bool, "policy_version": str, "candidates": list[str]}
    """
    # ② 灰度一致 guard(OpenCode 节点 1 [MED])
    pol = policy()
    if req.policy_version != pol.policy_version:
        raise HTTPException(
            status_code=400,
            detail=(
                f"policy_version mismatch: body={req.policy_version} "
                f"policy()={pol.policy_version} (revert policy.yaml 后再调)"
            ),
        )
    # ③ 重新构造候选(同 _build_cascade 同形,Phase B 三层:静态真→动态→mock)
    manifest_entries = load_manifest()
    entries, candidates = _build_three_layer_candidates(pol, manifest_entries)
    # ④+⑤ 同步刷新 strategy/cost_gate/enforcer + apply_policy(单一刷新源 _refresh_and_apply)
    applied = _refresh_and_apply(_cascade, entries, candidates, req.policy_version)
    return {
        "applied": applied,
        "policy_version": req.policy_version,
        "candidates": [n for n, _p, _k in candidates],
    }
