"""S2.7 · 合规门卫 policy_enforcer(design 门卫层 ①,请求管线最先)。

跨 provider 聚合 OK,同 provider 多账号薅羊毛 ❌(修 workflow2 BUG-policy-01)。

两职责(compliance-gate spec):
  1. **provider 别名归一化**(Req 1)——维护 name→entity 归一化表,由 scanner/manifest 生成
     (ProviderEntry.entity;None→name 自身)。运行时 normalize(name)→canonical entity。
     多别名映射同一 entity(同账号多模型/档位)→ 视为同一 provider 实体,**合规**。
  2. **同 provider 多账号拦截**(Req 2)——group by entity,某 entity 出现 ≥2 个不同 api_key_env
     (账号 key 名)= 同 provider 多账号薅羊毛 = **违规**。check() 拒绝路由 + 写合规日志。

判定规则(只看账号,不看模型):同一 canonical entity 下,不同 api_key_env 的数量 ≥ 2 即违规。
无 api_key_env 的 entry(mock / 未配 key)无账号,不参与多账号检测——它们不算薅羊毛。

接线:Cascade(policy_enforcer=...) 在 run() 最先 check();违规 → ComplianceError 被捕获
→ CascadeResult(last_reason="compliance_blocked")早返(不 init store、不 plan、不调 provider)。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from ..config import ProviderEntry

# 独立合规 logger:运维一眼定位「同 provider 多账号」违规(与路由/熔断日志分离)。
_COMPLIANCE_LOG = logging.getLogger("llm_router.compliance")


@dataclass(frozen=True)
class ComplianceViolation:
    """单条合规违规:同一 canonical entity 下挂了 ≥2 个不同账号 key。

    entity:违规的归一化 provider 实体(normalize 后)。
    accounts:该 entity 下出现的不同 api_key_env(账号 key **名**,非 secret 本身,守 security.md)。
    entries:涉事 ProviderEntry.name 列表(可观测/诊断,已去重排序)。
    """

    entity: str
    accounts: tuple[str, ...]
    entries: tuple[str, ...]


class ComplianceError(RuntimeError):
    """配置非合规(同 provider 多账号)。Cascade 捕获 → compliance_blocked,不上抛给 HTTP 层。

    携带 violations 供上层/日志结构化诊断。
    """

    def __init__(self, violations: tuple[ComplianceViolation, ...]):
        self.violations = violations
        super().__init__(
            "compliance violation: same-provider multi-account detected — "
            + "; ".join(
                f"{v.entity} accounts={list(v.accounts)}" for v in violations
            )
        )


class PolicyEnforcer:
    """合规门卫:别名归一化 + 同 provider 多账号检测。

    由候选 entries(policy mock + manifest 真 provider 合并)构造。__init__ 纯逻辑(无副作用,
    便于测试)。check() 是「拒绝 + 记日志」入口:违规 → 写合规日志(once) + raise ComplianceError;
    合规 → 静默返回 None。

    无 api_key_env 的 entry 不参与多账号检测(无账号≠薅羊毛),但仍进别名归一化表。
    """

    def __init__(self, entries: Iterable[ProviderEntry]) -> None:
        # 共享初始化路径:__init__ 和 S4.3 rebuild() 都走 __init_rebuild(防逻辑分叉)。
        self.__init_rebuild(entries)
        self._logged = False

    def rebuild(self, entries: "Iterable[ProviderEntry]") -> None:
        """S4.3:apply_policy 重建别名表 + 违规表(防 entity 映射 stale)。

        ponytail:重跑 __init__ 同款逻辑;不引新路径——统一用 __init__ 入口的
        __init_rebuild 私有方法(下面抽出来);对外只露 rebuild。
        """
        self.__init_rebuild(entries)

    def __init_rebuild(self, entries: "Iterable[ProviderEntry]") -> None:
        self._alias_table = {}
        per_entity: dict = defaultdict(lambda: defaultdict(set))
        for e in entries:
            entity = e.entity or e.name
            self._alias_table[e.name] = entity
            if e.api_key_env:
                per_entity[entity][e.api_key_env].add(e.name)
        self._violations = tuple(
            ComplianceViolation(
                entity=entity,
                accounts=tuple(sorted(accounts.keys())),
                entries=tuple(sorted(n for names in accounts.values() for n in names)),
            )
            for entity, accounts in sorted(per_entity.items())
            if len(accounts) >= 2
        )

    @property
    def alias_table(self) -> dict[str, str]:
        """name → canonical entity 归一化表(scanner 生成)。暴露供可观测/诊断。"""
        return dict(self._alias_table)

    @property
    def violations(self) -> tuple[ComplianceViolation, ...]:
        """检测到的全部违规(同 provider 多账号)。空 = 合规。"""
        return self._violations

    def normalize(self, name: str) -> str:
        """别名归一化:name → canonical entity。未登记的 name 回退自身(防御性,不抛)。"""
        return self._alias_table.get(name, name)

    def is_compliant(self) -> bool:
        """是否合规(无违规)。廉价 bool,供不抛异常的查询。"""
        return not self._violations

    def check(self) -> None:
        """门卫入口(spec「拒绝路由 + 记合规日志」):违规 → 记日志(once)+ raise ComplianceError。

        合规 → 静默返回 None。once-guard:配置静态,同一实例每请求 check() 一次,只在首次违规
        记日志(防 ERROR 刷屏);后续 check() 仍 raise(每次请求都被拒),但不重复记日志。

        线程安全:假设单线程 asyncio 事件循环(FastAPI 默认;check() 纯 sync 无 await 点,不被
        事件循环抢占)。线程池并发下 `_logged` 存在 TOCTOU(最多多记几次日志,不会漏记),可接受。
        """
        if not self._violations:
            return
        if not self._logged:
            for v in self._violations:
                _COMPLIANCE_LOG.error(
                    "compliance violation: provider '%s' 同实体多账号薅羊毛 "
                    "accounts=%s entries=%s(跨 provider 聚合合规;同 provider 多账号拒绝路由)",
                    v.entity,
                    list(v.accounts),
                    list(v.entries),
                )
            self._logged = True
        raise ComplianceError(self._violations)
