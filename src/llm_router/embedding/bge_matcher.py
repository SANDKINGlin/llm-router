"""S2.9 子片 0.1 · BgeMatcher 能力匹配器(② 匹配层,Phase2)。

spec: capability-matching/spec.md。design 约束#3:字典序非加权和(spec 旧"w4 免费对口加成"
加权只能近似、不能保证对口相当时免费必胜 → 实施改字典序;BgeMatcher 只产 capability bool
进首槽,is_free/cost 由 EpsilonGreedy._rank 第二三槽处理,免费优先不靠 bge 加权)。

守 TierMatcher 接口 `matches(tier, task_type) -> bool`(design docstring「同 matches 接口」,
surgical:零改 epsilon_greedy 调用点 + test_matcher)。tier→能力描述文本,task_type→任务描述
文本,encoder 编码后 cosine > threshold → 对口。None/未知 task_type → 全对口(向后兼容 S2.1a)。

真 bge 编码器(sentence-transformers 130MB 懒加载)defer 到模型就绪子片;本切片 HashEncoder
作确定性占位,BgeMatcher 吃 Encoder 协议,真 bge 接入只换 encoder 实例不改匹配逻辑。

capability_match 槽位:S2.1a 时 TierMatcher 置常量(粗档位),S2.9 换 BgeMatcher(向量细匹配),
排序键 (capability_match DESC, is_free DESC, 倍率 ASC) 逻辑不动(epsilon_greedy._rank)。
"""
from __future__ import annotations

from .encoder import Encoder, cosine

# tier → 能力描述文本(model card 代理;真 per-model 能力文本由 S2.10 scanner 抓 model card 填,
# 届时 BgeMatcher 可换吃 provider-name→描述 map,本切片 tier 粒度足够验证架构)。
_TIER_CAPABILITY_TEXT: dict[str, str] = {
    "strong": "strong deep reasoning complex math analysis coding long context hard problems",
    "medium": "medium general chat conversation translate summarize balanced tasks",
    "fast": "fast quick simple short lightweight code completion light tasks",
}

# task_type 关键词 → 任务描述文本(cosine 比对用,归一小写)。未知/None → 全对口。
_TASK_TEXT: dict[str, str] = {
    "reasoning": "deep reasoning complex math analysis hard problems",
    "math": "complex math analysis hard problems",
    "code": "code completion lightweight simple tasks",
    "coding": "code completion lightweight simple tasks",
    "general": "general chat conversation balanced tasks",
    "chat": "general chat conversation balanced tasks",
}


class BgeMatcher:
    """向量能力匹配器(Phase2,守 matches(tier, task_type) 接口)。

    matches(tier, task_type) = cosine(encode(tier_capability_text), encode(task_text)) > threshold。
    None/空/未知 task_type → 全对口 True(向后兼容 S2.1a 空 ctx 顺序零变化,与 TierMatcher 同)。

    threshold 调高更严(更多任务判不对口落 fallback 软尾);调低更松。Golden Set 校准
    (spec Req 3)调 threshold,defer 子片 0.2(用 S3.4 golden_set/wilson)。
    """

    def __init__(
        self,
        encoder: Encoder,
        *,
        threshold: float = 0.5,
        capability_text: dict[str, str] | None = None,
    ) -> None:
        self._encoder = encoder
        self._threshold = threshold
        self._capability_text = capability_text or _TIER_CAPABILITY_TEXT

    def matches(self, tier: str, task_type: str | None) -> bool:
        """provider tier 能力是否对口 task_type → bool(进排序键首槽)。

        - task_type 为 None/空/未知 → True(全对口,向后兼容)。
        - 已知 task_type → cosine(tier 能力描述, 任务描述) > threshold。
        - 未知 tier → 当 fast(fail-open,与 TierMatcher 同;policy Literal 已校验合法 tier)。

        实现复用 score()(S2.9-0.2):score None 即全对口情况 → True;否则 score>threshold。
        """
        s = self.score(tier, task_type)
        return True if s is None else s > self._threshold

    def score(self, tier: str, task_type: str | None) -> float | None:
        """tier 能力对 task_type 的 cosine 分(S2.9-0.2,供 Golden Set 校准)。

        - task_type 为 None/空/未知 → None(全对口情况,校准时排除;matches 据此返 True)。
        - 已知 task_type → cosine(tier 能力描述, 任务描述) ∈ [-1.0, 1.0]。
        - 未知 tier → 当 fast(fail-open,与 matches 同)。

        校准(embedding/calibration.py)收集 score 非 None 的配对,算 Pearson 预测力 +
        遍历候选 threshold;校准结果注入 __init__ threshold。score 不进排序键(只 matches 的
        bool 进首槽),守 routing-priority-principle。
        """
        if not task_type:
            return None
        norm = task_type.strip().lower()
        if not norm:
            return None
        task_text = self._task_text(norm)
        if task_text is None:
            return None  # 未知 task_type → None(matches 据此返 True 全对口)
        cap_text = self._capability_text.get(tier, _TIER_CAPABILITY_TEXT["fast"])
        return cosine(self._encoder.encode(cap_text), self._encoder.encode(task_text))

    @staticmethod
    def _task_text(norm: str) -> str | None:
        """归一 task_type → 任务描述文本;未知 → None(调用方判全对口)。"""
        for key, text in _TASK_TEXT.items():
            if key in norm:
                return text
        return None
