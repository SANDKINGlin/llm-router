"""S2.7 · 合规门卫 policy_enforcer(design 门卫层 ①,请求管线最先)。

修 workflow2 BUG-policy-01:跨 provider 聚合 OK,同 provider 多账号薅羊毛 ❌。

两职责(compliance-gate spec):
  1. provider 别名归一化——name→entity 归一化表(scanner/manifest 经 ProviderEntry.entity 生成);
     多别名映射同一 entity(同账号多模型/档位)→ 视为同一 provider 实体,合规。
  2. 同 provider 多账号拦截——group by entity,某 entity 出现 ≥2 个不同 api_key_env(账号)= 违规;
     check() 拒绝路由 + 写合规日志(once-guard 防每请求刷屏)。

TDD:本套件先 RED——PolicyEnforcer / entity 字段未实现时 import 即失败。
测试模式沿用 test_cascade/test_probe:同步 def + 必要处 asyncio.run;caplog 验日志。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from llm_router.api.cascade import Cascade
from llm_router.api.policy_enforcer import (
    ComplianceError,
    ComplianceViolation,
    PolicyEnforcer,
)
from llm_router.api.strategy import RoutingStrategy
from llm_router.config import ProviderEntry
from llm_router.providers.base import Provider
from llm_router.resilience.circuit_breaker import CircuitBreaker
from llm_router.store.trace import TraceStore


def _entry(
    name: str,
    *,
    entity: str | None = None,
    api_key_env: str | None = None,
    tier: str = "fast",
    is_free: bool = True,
    cost_multiplier: float = 0.0,
) -> ProviderEntry:
    """最小 ProviderEntry 构造器(只填合规检测关心的字段 entity / api_key_env)。"""
    return ProviderEntry(
        name=name,
        entity=entity,
        tier=tier,
        quota=1,
        cooldown_s=1,
        is_free=is_free,
        cost_multiplier=cost_multiplier,
        api_key_env=api_key_env,
    )


# ── Req 1:provider 别名归一化 ──────────────────────────────────────────────


def test_alias_table_maps_alias_to_entity():
    """显式 entity 的 entry → alias_table 把 name 映射到 canonical entity。"""

    enf = PolicyEnforcer([
        _entry("openrouter-gptoss", entity="openrouter", api_key_env="K1"),
        _entry("openrouter-qwen", entity="openrouter", api_key_env="K1"),
    ])
    assert enf.alias_table == {
        "openrouter-gptoss": "openrouter",
        "openrouter-qwen": "openrouter",
    }


def test_normalize_maps_alias_to_entity():
    """normalize(alias) → canonical entity(Req 1 场景:多别名识别为同一 provider 实体)。"""

    enf = PolicyEnforcer([
        _entry("a1", entity="openrouter", api_key_env="K1"),
        _entry("a2", entity="openrouter", api_key_env="K1"),
    ])
    assert enf.normalize("a1") == "openrouter"
    assert enf.normalize("a2") == "openrouter"


def test_normalize_defaults_to_name_without_entity():
    """无 entity 的 entry → entity 默认 = name 自身(向后兼容;每个 name 自成一实体)。"""

    enf = PolicyEnforcer([_entry("groq", api_key_env="GROQ_API_KEY")])
    assert enf.normalize("groq") == "groq"


def test_normalize_unknown_name_returns_name():
    """未登记的 name → 回退自身(不抛 KeyError,防御性)。"""

    enf = PolicyEnforcer([_entry("a", entity="openrouter", api_key_env="K1")])
    assert enf.normalize("not-in-table") == "not-in-table"


def test_same_entity_same_key_is_compliant():
    """同一 entity + 同一账号 key + 多别名 = 合规(同账号多模型/档位,非薅羊毛)。"""

    enf = PolicyEnforcer([
        _entry("openrouter-gptoss", entity="openrouter", api_key_env="K1"),
        _entry("openrouter-qwen", entity="openrouter", api_key_env="K1"),
    ])
    assert enf.violations == ()
    assert enf.is_compliant() is True


# ── Req 2:同 provider 多账号拦截 ────────────────────────────────────────────


def test_same_entity_two_keys_is_violation():
    """同 entity + 2 个不同账号 key = 违规(Req 2 场景 1:同 provider 多账号薅羊毛)。"""

    enf = PolicyEnforcer([
        _entry("openrouter-a", entity="openrouter", api_key_env="K1"),
        _entry("openrouter-b", entity="openrouter", api_key_env="K2"),
    ])
    assert len(enf.violations) == 1
    v = enf.violations[0]
    assert v.entity == "openrouter"
    assert set(v.accounts) == {"K1", "K2"}
    assert enf.is_compliant() is False


def test_violation_carries_entity_accounts_and_entries():
    """ComplianceViolation 暴露 entity / accounts(不同 key)/ entries(涉事 entry 名),供诊断。"""

    enf = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="openrouter", api_key_env="K2"),
    ])
    v = enf.violations[0]
    assert v.entity == "openrouter"
    assert set(v.accounts) == {"K1", "K2"}
    assert set(v.entries) == {"a", "b"}


def test_cross_provider_aggregation_is_compliant():
    """不同 entity 各 1 个账号 key = 合规(Req 2 场景 2:跨 provider 聚合放行)。"""

    enf = PolicyEnforcer([
        _entry("openrouter", entity="openrouter", api_key_env="OPENROUTER_API_KEY"),
        _entry("groq", entity="groq", api_key_env="GROQ_API_KEY"),
        _entry("nvidia", entity="nvidia", api_key_env="NVIDIA_API_KEY"),
    ])
    assert enf.violations == ()
    assert enf.is_compliant() is True


def test_entity_defaulting_to_name_can_still_violate():
    """显式 entity 与「name 即 entity」混合时仍能归一:entry b 无 entity 但 name=openrouter,
    与 entry a(entity=openrouter)归一为同一实体 → 若账号不同即违规。"""

    enf = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("openrouter", api_key_env="K2"),  # entity 默认 name=openrouter
    ])
    assert len(enf.violations) == 1
    assert enf.violations[0].entity == "openrouter"


# ── 边界:unkeyed / 空 / check 行为 / 日志 ────────────────────────────────────


def test_unkeyed_entries_excluded_from_multi_account_check():
    """无 api_key_env 的 entry(mock / 未配 key)不参与多账号检测——它们没有账号,不算薅羊毛。"""

    enf = PolicyEnforcer([
        _entry("mock-a", entity="mock"),  # 无 key
        _entry("mock-b", entity="mock"),  # 无 key,同 entity 但都无账号 → 合规
    ])
    assert enf.violations == ()
    assert enf.is_compliant() is True


def test_unkeyed_alias_alongside_keyed_is_not_violation():
    """同 entity 下 1 个有 key + 1 个无 key → 仅 1 个账号 → 合规(无 key 的只是未配置别名)。"""

    enf = PolicyEnforcer([
        _entry("openrouter", entity="openrouter", api_key_env="K1"),
        _entry("openrouter-fallback", entity="openrouter"),  # 无 key
    ])
    assert enf.is_compliant() is True


def test_empty_entries_compliant():
    """空配置 → 合规,无违规。"""

    enf = PolicyEnforcer([])
    assert enf.violations == ()
    assert enf.is_compliant() is True


def test_check_raises_compliance_error_when_violation():
    """check() 在违规时 raise ComplianceError(携带 violations)。"""

    enf = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="openrouter", api_key_env="K2"),
    ])
    with pytest.raises(ComplianceError) as exc_info:
        enf.check()
    assert exc_info.value.violations == enf.violations


def test_check_passes_silently_when_compliant():
    """合规时 check() 返回 None(不抛)。"""

    enf = PolicyEnforcer([_entry("groq", entity="groq", api_key_env="GROQ_API_KEY")])
    assert enf.check() is None  # 合规 → 静默放行


def test_check_logs_violation_once(caplog):
    """check() 违规时写合规日志(ERROR),且同一实例只记一次(防每请求刷屏)。"""

    enf = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="openrouter", api_key_env="K2"),
    ])
    with caplog.at_level(logging.ERROR, logger="llm_router.compliance"):
        with pytest.raises(ComplianceError):
            enf.check()
        with pytest.raises(ComplianceError):
            enf.check()  # 第二次:仍 raise,但不重复记日志
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "llm_router.compliance"
    ]
    assert len(errors) == 1, "合规违规日志只记一次(once-guard 防刷屏)"
    msg = errors[0].getMessage()
    assert "openrouter" in msg and "K1" in msg and "K2" in msg


def test_check_compliant_does_not_log(caplog):
    """合规 check() 不写任何合规日志(不污染日志)。"""

    enf = PolicyEnforcer([_entry("groq", entity="groq", api_key_env="GROQ_API_KEY")])
    with caplog.at_level(logging.DEBUG, logger="llm_router.compliance"):
        enf.check()
    assert all(r.name != "llm_router.compliance" for r in caplog.records)


# ── 边界盲区(OpenCode 审查 #1-#4)──────────────────────────────────────────


def test_entity_empty_string_falls_back_to_name():
    """entity='' 空串同 None → 回退 name 自身(与 config docstring 一致;OpenCode #1)。"""

    enf = PolicyEnforcer([_entry("groq", entity="", api_key_env="K1")])
    assert enf.normalize("groq") == "groq"
    assert enf.is_compliant() is True


def test_api_key_env_empty_string_excluded_from_check():
    """api_key_env='' 空串 = 无账号 → 不参与多账号检测(同 None;OpenCode #2)。"""

    enf = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env=""),
        _entry("b", entity="openrouter", api_key_env=""),
    ])
    assert enf.is_compliant() is True  # 两个空 key = 无账号 = 合规


def test_three_keys_same_entity_is_violation():
    """同 entity ≥3 个不同 key 仍是违规(泛化 ≥2;OpenCode #3)。"""

    enf = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="openrouter", api_key_env="K2"),
        _entry("c", entity="openrouter", api_key_env="K3"),
    ])
    assert len(enf.violations) == 1
    assert set(enf.violations[0].accounts) == {"K1", "K2", "K3"}
    assert set(enf.violations[0].entries) == {"a", "b", "c"}


def test_multiple_entities_violate_independently():
    """多实体各自违规 → violations 含多条(OpenCode #4)。"""

    enf = PolicyEnforcer([
        _entry("a1", entity="openrouter", api_key_env="K1"),
        _entry("a2", entity="openrouter", api_key_env="K2"),
        _entry("b1", entity="groq", api_key_env="G1"),
        _entry("b2", entity="groq", api_key_env="G2"),
    ])
    assert len(enf.violations) == 2
    entities = {v.entity for v in enf.violations}
    assert entities == {"openrouter", "groq"}


# ── Cascade 接线:合规门是请求管线 layer ①(最先,先于 health/plan) ─────────────


class _CountingProvider(Provider):
    """记录是否被调用(证明合规门阻断时 provider.complete 零调用)。"""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def complete(self, prompt: str) -> tuple[str, str, None]:
        self.calls += 1
        return f"from-{self.name}", f"m-{self.name}", None


class _FixedOrderStrategy(RoutingStrategy):
    def __init__(self, order):
        self._order = list(order)

    def plan(self, candidates, context):
        seen = set(candidates)
        return [c for c in self._order if c in seen]

    def select_provider(self, candidates, context):
        return self.plan(candidates, context)[0]


def _run(coro):
    return asyncio.run(coro)


def test_cascade_compliance_gate_blocks_routing(tmp_path):
    """Cascade 挂违规 enforcer → run() 最先被门卫拦:compliance_blocked,provider 零调用。"""

    violating = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="openrouter", api_key_env="K2"),
    ])
    pa = _CountingProvider("a")
    pb = _CountingProvider("b")
    store = TraceStore(tmp_path / "trace.db")
    cascade = Cascade(
        store,
        CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3),
        _FixedOrderStrategy(["a", "b"]),
        [("a", pa, "K1"), ("b", pb, "K2")],
        policy_enforcer=violating,
    )

    async def body():
        try:
            res = await cascade.run("ping", correlation_id="CID")
            return res
        finally:
            await store.close()

    res = _run(body())
    assert res.success is False
    assert res.last_reason == "compliance_blocked"
    assert res.hops_attempted == 0
    assert pa.calls == 0 and pb.calls == 0, "合规门阻断时 provider 必须零调用"


def test_cascade_compliant_enforcer_routes_normally(tmp_path):
    """Cascade 挂合规 enforcer → 门卫放行,正常路由(首跳成功)。"""

    compliant = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="groq", api_key_env="K2"),
    ])
    pa = _CountingProvider("a")
    store = TraceStore(tmp_path / "trace.db")
    cascade = Cascade(
        store,
        CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3),
        _FixedOrderStrategy(["a", "b"]),
        [("a", pa, "K1"), ("b", _CountingProvider("b"), "K2")],
        policy_enforcer=compliant,
    )

    async def body():
        try:
            return await cascade.run("ping", correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is True
    assert res.final_text == "from-a"
    assert pa.calls == 1


def test_cascade_without_enforcer_unaffected(tmp_path):
    """Cascade 未挂 enforcer(None)→ 行为零变化(向后兼容,已有 cascade 测试不挂 enforcer)。"""

    pa = _CountingProvider("a")
    store = TraceStore(tmp_path / "trace.db")
    cascade = Cascade(
        store,
        CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3),
        _FixedOrderStrategy(["a"]),
        [("a", pa, "K1")],
    )  # 不传 policy_enforcer

    async def body():
        try:
            return await cascade.run("ping", correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.success is True
    assert res.final_text == "from-a"


def test_compliance_gate_runs_before_health_filter(tmp_path):
    """合规门先于 health 过滤:compliance_blocked 时 health_store.latest_probe 零调用(OpenCode #5)。

    钉 layering 契约:① 合规 → ② health → ③ plan。违规即早返,不查 health.db。
    """

    class _SpyHealthStore:
        """记录 latest_probe 调用次数的 spy(duck-typed,仅实现 Cascade 用到的方法)。"""

        def __init__(self):
            self.queries = 0

        async def latest_probe(self, providers=None, *, alive_only=False):
            self.queries += 1
            return []  # 无探活记录 → 不过滤

    violating = PolicyEnforcer([
        _entry("a", entity="openrouter", api_key_env="K1"),
        _entry("b", entity="openrouter", api_key_env="K2"),
    ])
    spy = _SpyHealthStore()
    store = TraceStore(tmp_path / "trace.db")
    cascade = Cascade(
        store,
        CircuitBreaker(db_path=tmp_path / "circuit.db", key_hard_threshold=3),
        _FixedOrderStrategy(["a", "b"]),
        [("a", _CountingProvider("a"), "K1"), ("b", _CountingProvider("b"), "K2")],
        health_store=spy,
        policy_enforcer=violating,
    )

    async def body():
        try:
            return await cascade.run("ping", correlation_id="CID")
        finally:
            await store.close()

    res = _run(body())
    assert res.last_reason == "compliance_blocked"
    assert spy.queries == 0, "合规门阻断时 health_store 必须零查询(合规先于 health)"
