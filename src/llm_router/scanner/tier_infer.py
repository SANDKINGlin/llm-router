"""S2.10 · 模型档位关键词推断(Phase1 粗匹配,同 design S2.2)。

从 model_id 关键词推断 tier(strong/medium/fast)。动态 scanner 轮询到的免费模型无显式
档位元数据,用关键词粗匹配贴标;真语义能力匹配 defer S2.9 bge(动态面试不加载本地模型,
守 ollama-qwen3-rules)。

红线(守 routing-priority-principle):tier 只进**能力匹配首槽**(capability_match bool
判定 + 路由排序键 capability_match DESC),不进排序键加权 sum。排序键仍是字典序
`(capability_match DESC, is_free DESC, 倍率 ASC)`。

设计:纯函数,无 I/O 无副作用,易测。关键词大小写不敏感。多关键词命中取**最强档**
(strong > medium > fast);无命中 → medium(默认中档,不偏激)。
"""
from __future__ import annotations

from .snapshot import DiscoveredModel

# 关键词 → tier 映射(从强到弱排序,匹配时取最强命中)。
# 来源:常见免费/开权重模型命名约定(nemotron/70b/405b/ultra=strong;
# mini/flash/8b/7b/small=fast;其余 medium)。
_TIER_KEYWORDS: list[tuple[str, str]] = [
    # strong:大参数 / 旗舰 / 推理增强
    ("70b", "strong"),
    ("405b", "strong"),
    ("nemotron", "strong"),
    ("ultra", "strong"),
    ("opus", "strong"),
    ("pro", "strong"),
    ("max", "strong"),
    ("reasoning", "strong"),
    ("r1", "strong"),
    ("o1", "strong"),
    ("o3", "strong"),
    ("deepseek-r", "strong"),
    ("gpt-oss-120b", "strong"),
    # fast:小参数 / 轻量 / 快速
    ("mini", "fast"),
    ("flash", "fast"),
    ("20b", "fast"),
    ("14b", "fast"),
    ("12b", "fast"),
    ("8b", "fast"),
    ("7b", "fast"),
    ("3b", "fast"),
    ("1b", "fast"),
    ("small", "fast"),
    ("lite", "fast"),
    ("nano", "fast"),
    ("haiku", "fast"),
    ("gpt-oss-20b", "fast"),
]

_TIER_RANK = {"strong": 3, "medium": 2, "fast": 1}
DEFAULT_TIER = "medium"


def infer_tier(model_id: str) -> str:
    """从 model_id 关键词推断 tier(strong/medium/fast)。

    大小写不敏感;多关键词命中取**最强档**(strong > medium > fast);无命中 → medium。

    Bug 防护:best_rank 从 0 起步(非 medium 的 2),否则 fast(rank 1)永远 > 不了初始
    medium(rank 2)→ fast 关键词被吞。无命中时显式回退 DEFAULT_TIER(medium)。
    """
    lowered = model_id.lower()
    best: str | None = None
    best_rank = 0
    for kw, tier in _TIER_KEYWORDS:
        if kw in lowered:
            rank = _TIER_RANK[tier]
            if rank > best_rank:
                best = tier
                best_rank = rank
    return best if best is not None else DEFAULT_TIER


def label_tier(model: DiscoveredModel) -> DiscoveredModel:
    """对 DiscoveredModel 贴 tier 标(model.tier 为 None 时用关键词推断,已有则保留)。

    返回**新** DiscoveredModel(immutable,守 coding-style);原对象不动。
    已有 tier(非 None)→ 原样返回(面试 0.4 可覆写,本函数不覆盖已有判定)。
    """
    if model.tier is not None:
        return model
    return DiscoveredModel(
        source=model.source,
        model_id=model.model_id,
        display_name=model.display_name,
        tier=infer_tier(model.model_id),
        is_free=model.is_free,
    )
