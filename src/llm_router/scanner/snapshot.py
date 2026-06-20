"""S2.10-0.1 · 动态 Scanner 快照 + diff(纯函数,零网络零 I/O)。

验收(specs/free-model-scanner Req「动态 diff 抓新免费模型」):每 h 轮询
NVIDIA/OpenRouter,与上次快照 diff,新增免费模型 → 自动面试 → 入库贴标 → 过期清退。
本拆片只做**纯函数数据层**——快照建模 + diff 算子;网络抓取(0.2)/存储(0.3)/
面试(0.4)/编排循环(0.5)留后续子片。

设计:
- `DiscoveredModel`:单个被发现的免费模型(source/name/id/tier 推断)。frozen dataclass,
  可哈希(进 set/dict),immutable(守 coding-style)。
- `Snapshot`:某次轮询某 source 的快照(`frozenset[DiscoveredModel]` + taken_at + source)。
  frozenset 天然去重 + 集合运算(diff 用 set 差集)。
- `DiffResult`:diff(prev, curr) → added/removed/unchanged 三组 frozenset。
  added = curr - prev(新上架);removed = prev - curr(下架/到期);unchanged = 交集。

红线(守 routing-priority-principle):diff 只是**发现信号**——added 模型还要过面试(0.4)
才贴标入库;不直接进路由排序键加权。本片零副作用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class ScannerSource(str, Enum):
    """动态轮询的免费模型来源。spec 钉死 NVIDIA + OpenRouter 两个。"""

    NVIDIA = "nvidia"
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class DiscoveredModel:
    """单个被发现的免费模型(轮询 /models 端点解析出的一条)。

    `model_id`:provider 侧的模型 id(调 OpenAI 兼容 API 用,如 `nvidia/llama-3.1-nemotron-70b-instruct`
        或 `openai/gpt-oss-120b:free`)。这是跨快照 diff 的**稳定主键**(同 id = 同模型)。
    `display_name`:人类可读名(可选,默认 = model_id)。
    `tier`:从模型名关键词推断的档位(strong/medium/fast,None = 未推断(面试 0.4 复核)。
        Phase1 关键词粗匹配,同 design S2.2;真语义能力 defer S2.9 bge(动态模型面试不加载本地模型)。
    `is_free`:是否免费档(OpenRouter `:free` 后缀 / NVIDIA NIM 免费档)。默认 True(动态
        scanner 只抓免费;非免费在 poller 0.2 过滤掉,不进快照)。
    """

    source: ScannerSource
    model_id: str
    display_name: str | None = None
    tier: str | None = None
    is_free: bool = True

    @property
    def name(self) -> str:
        """display_name 优先,回退 model_id(供日志/贴标人类可读)。"""
        return self.display_name or self.model_id


@dataclass(frozen=True)
class Snapshot:
    """某次轮询某 source 的快照:frozenset[DiscoveredModel] + taken_at + source。

    frozenset 天然去重(同 model_id 的 DiscoveredModel 等值——dataclass(frozen=True) + 全字段
    参与 hash;**注意**:display_name 差异的同 model_id 会被视为不同条目。poller 0.2 解析时
    应对同 model_id 归一,本片不强制)。
    """

    source: ScannerSource
    models: frozenset[DiscoveredModel]
    taken_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def empty(cls, source: ScannerSource) -> "Snapshot":
        """空快照(首次轮询 prev 用,或 source 无可用模型时降级)。"""
        return cls(source=source, models=frozenset())

    def __len__(self) -> int:
        return len(self.models)

    def model_ids(self) -> frozenset[str]:
        """快照内所有 model_id 集合(供按 id diff,跨 display_name 抖动稳定)。"""
        return frozenset(m.model_id for m in self.models)


@dataclass(frozen=True)
class DiffResult:
    """两次快照的 diff 结果(按 model_id 主键)。

    - added:curr 有 prev 无 → 新上架免费模型(过面试 0.4 后入库贴标)。
    - removed:prev 有 curr 无 → 下架/到期(过期清退,0.5 秏 pool)。
    - unchanged:两者都有(稳定在池,不动)。

    按 model_id diff(非整对象),跨 display_name/tier 抖动稳定——同模型两次轮询 display_name
    变了不算 added/removed。added/removed 返回完整 DiscoveredModel(added 取 curr 的,removed
    取 prev 的),供下游面试/清退用全字段。
    """

    source: ScannerSource
    added: frozenset[DiscoveredModel]
    removed: frozenset[DiscoveredModel]
    unchanged: frozenset[DiscoveredModel]


def diff_snapshots(prev: Snapshot, curr: Snapshot) -> DiffResult:
    """两次同 source 快照 diff → DiffResult(按 model_id 主键)。

    不同 source → ValueError(fail-loud,不静默跨源比较;守 surgical fail-fast 纪律)。
    首次轮询(prev=empty)→ added=curr 全部,removed=空,unchanged=空。

    按 model_id 主键 diff:同 id 视为同模型(display_name/tier 抖动不触发 added/removed)。
    added 取 curr 的 DiscoveredModel,removed 取 prev 的,unchanged 取 curr 的(curr 最新字段)。
    """
    if prev.source is not curr.source and prev.source != curr.source:
        raise ValueError(
            f"diff_snapshots 跨 source 比较(fail-loud):prev={prev.source.value} "
            f"curr={curr.source.value}——同 source 才能比"
        )

    prev_by_id = {m.model_id: m for m in prev.models}
    curr_by_id = {m.model_id: m for m in curr.models}
    prev_ids = set(prev_by_id)
    curr_ids = set(curr_by_id)

    added_ids = curr_ids - prev_ids
    removed_ids = prev_ids - curr_ids
    unchanged_ids = curr_ids & prev_ids

    return DiffResult(
        source=curr.source,
        added=frozenset(curr_by_id[i] for i in added_ids),
        removed=frozenset(prev_by_id[i] for i in removed_ids),
        unchanged=frozenset(curr_by_id[i] for i in unchanged_ids),
    )


def merge_snapshots(snapshots: Iterable[Snapshot]) -> dict[ScannerSource, frozenset[DiscoveredModel]]:
    """多 source 快照 → 按 source 分组的 model 集合(供 0.5 编排聚合同轮询批次)。

    同 source 多次出现 → 后者覆盖(最新轮询为准,latest-wins,同 health-probe 新鲜度语义)。
    返回 dict[source → frozenset[DiscoveredModel]],空输入 → {}。
    """
    out: dict[ScannerSource, frozenset[DiscoveredModel]] = {}
    for snap in snapshots:
        out[snap.source] = snap.models
    return out
