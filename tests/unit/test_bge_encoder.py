"""S2.9 子片 0.3 · 真 BgeEncoder(sentence-transformers 懒加载/卸载)单测。

spec: capability-matching/spec.md(Req: bge-small 懒加载,编码后卸载,<80MB 稳态)。
design D2:bge-small 130MB 懒加载+卸载,峰值<250MB,稳态<80MB。约束#1 防卡死。

**测试策略**(守 gotchas「外部依赖先验证」+ 确定性):
- 单测用 DI(`model_factory` 注入 FakeModel,免真 bge 130MB 加载,快且确定性)。
- 真 bge 集成测试 gated:`BGE_INTEGRATION=1` 环境变量 + ST 已装才跑(需网络下模型,慢,
  不进默认 CI)。默认 skip——单测已覆盖懒加载/卸载/协议/确定性契约。

BgeEncoder 守 Encoder 协议(encode(text)->Vector),可注入 BgeMatcher 经 Encoder 槽替
HashEncoder(0.1/0.2 用 HashEncoder,0.3 真 bge 接入)。

TDD:先 RED——BgeEncoder 未实现时 import 即失败。
"""
from __future__ import annotations

import math
import os

import pytest

from llm_router.embedding.bge_matcher import BgeMatcher
from llm_router.embedding.encoder import BgeEncoder


class FakeModel:
    """DI fake:模拟 SentenceTransformer.encode(L2 归一化,确定性)。

    不加载真模型;同文本同向量(确定性,守 Encoder 协议契约)。
    """

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.encode_calls = 0

    def encode(self, texts, normalize_embeddings: bool = True):
        self.encode_calls += 1
        out = []
        for t in texts:
            # 确定性向量:文本 hash 散列(跨运行稳定,守协议确定性契约)。
            h = abs(hash(t))
            v = [float((h >> i) & 0xF) for i in range(self._dim)]
            mag = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / mag for x in v])
        return out


def _factory(dim: int = 16):
    return lambda: FakeModel(dim=dim)


# ── L1:懒加载 / 卸载 / 内存契约(design D2)──────────────────────────────────


class TestLazyLoadUnload:
    def test_not_loaded_until_first_encode(self):
        """★ 懒加载:构造后未加载,首次 encode 才 load(守 D2 不预占 130MB)。"""
        enc = BgeEncoder(model_factory=_factory())
        assert enc.is_loaded() is False

    def test_first_encode_triggers_load(self):
        enc = BgeEncoder(model_factory=_factory())
        enc.encode("hello")
        assert enc.is_loaded() is True

    def test_unload_releases_model(self):
        """★ 卸载:unload() 后 is_loaded() False(释放 130MB,稳态<80MB 契约)。"""
        enc = BgeEncoder(model_factory=_factory())
        enc.load()
        assert enc.is_loaded() is True
        enc.unload()
        assert enc.is_loaded() is False

    def test_load_idempotent(self):
        """load() 幂等:二次 load 不重复调 factory(不重建模型)。"""
        calls = [0]

        def f():
            calls[0] += 1
            return FakeModel()

        enc = BgeEncoder(model_factory=f)
        enc.load()
        enc.load()
        assert calls[0] == 1

    def test_factory_called_once_across_encodes(self):
        """多次 encode 复用同一模型(factory 只调一次,不每次重载——避免慢)。"""
        calls = [0]

        def f():
            calls[0] += 1
            return FakeModel()

        enc = BgeEncoder(model_factory=f)
        enc.encode("a")
        enc.encode("b")
        enc.encode("c")
        assert calls[0] == 1

    def test_unload_then_reencode_reloads(self):
        """unload 后再 encode 重新 load(卸载后能恢复,非一次性)。"""
        enc = BgeEncoder(model_factory=_factory())
        enc.encode("a")
        enc.unload()
        assert enc.is_loaded() is False
        v = enc.encode("b")  # 重新 load
        assert enc.is_loaded() is True
        assert len(v) > 0


# ── L2:Encoder 协议契约(确定性 + 归一化)─────────────────────────────────


class TestEncoderProtocol:
    def test_deterministic_same_text_same_vector(self):
        """★ 协议契约:同文本同向量(跨调用稳定,供缓存 + 校准)。"""
        enc = BgeEncoder(model_factory=_factory(dim=16))
        assert enc.encode("hello") == enc.encode("hello")

    def test_different_text_different_vector(self):
        enc = BgeEncoder(model_factory=_factory(dim=16))
        assert enc.encode("hello") != enc.encode("world")

    def test_returns_normalized_unit_vector(self):
        """★ 归一化:|v|≈1(cosine 算内积即可,与 HashEncoder 一致契约)。"""
        enc = BgeEncoder(model_factory=_factory(dim=8))
        v = enc.encode("any text")
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6

    def test_returns_list_of_floats(self):
        enc = BgeEncoder(model_factory=_factory(dim=8))
        v = enc.encode("text")
        assert isinstance(v, list)
        assert all(isinstance(x, float) for x in v)


# ── L3:接入 BgeMatcher(经 Encoder 协议槽替 HashEncoder)─────────────────


class TestBgeMatcherIntegration:
    def test_bge_encoder_injectable_into_bge_matcher(self):
        """★ BgeEncoder 守 Encoder 协议 → 可注入 BgeMatcher(0.1/0.2 用 HashEncoder,
        0.3 真 bge 经同槽接入,surgical 零改 BgeMatcher)。"""
        enc = BgeEncoder(model_factory=_factory(dim=16))
        m = BgeMatcher(enc, threshold=0.5)
        assert isinstance(m.matches("strong", "reasoning"), bool)

    def test_backward_compat_none_task_type(self):
        """注入 BgeEncoder 后向后兼容:None task_type → 全对口 True(S2.1a 零变化)。"""
        enc = BgeEncoder(model_factory=_factory(dim=16))
        m = BgeMatcher(enc, threshold=0.5)
        assert m.matches("strong", None) is True
        assert m.matches("fast", "") is True

    def test_score_returns_float_with_bge_encoder(self):
        """BgeMatcher.score() 经 BgeEncoder 返 float(供 0.2 校准;0.3 真 bge 亦通)。"""
        enc = BgeEncoder(model_factory=_factory(dim=16))
        m = BgeMatcher(enc, threshold=0.5)
        s = m.score("strong", "reasoning")
        assert s is not None
        assert -1.0 <= s <= 1.0


# ── L4:真 bge 集成测试(gated,默认 skip)──────────────────────────────────
# 需 BGE_INTEGRATION=1 + sentence-transformers 已装;下载 130MB 模型,慢,不进默认 CI。
# 守 gotchas「外部依赖先验证」:真 bge 加载在此显式验证(懒加载/卸载/编码真语义向量)。

_HAS_ST = True
try:
    import sentence_transformers  # noqa: F401
except ImportError:
    _HAS_ST = False

_INTEGRATION = os.environ.get("BGE_INTEGRATION") == "1"


@pytest.mark.skipif(
    not (_HAS_ST and _INTEGRATION),
    reason="需 BGE_INTEGRATION=1 + sentence-transformers(下载 130MB 模型,慢)",
)
class TestRealBgeIntegration:
    def test_real_bge_small_loads_and_encodes(self):
        """真 bge-small-en-v1.5:懒加载 → 编码 → 归一化 → 卸载(守 D2)。"""
        enc = BgeEncoder()  # 默认真 factory:SentenceTransformer("BAAI/bge-small-en-v1.5")
        assert enc.is_loaded() is False
        v = enc.encode("deep reasoning complex math")
        assert len(v) > 0
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-4
        enc.unload()
        assert enc.is_loaded() is False

    def test_real_bge_semantic_similarity(self):
        """真 bge 语义:reasoning 与 math 相似度 > reasoning 与 cooking(语义分离,
        HashEncoder n-gram 代理做不到,0.1 已声明此为 0.3 bge 价值)。"""
        from llm_router.embedding.encoder import cosine

        enc = BgeEncoder()
        v_reason = enc.encode("deep reasoning complex math")
        v_math = enc.encode("mathematical problem solving")
        v_cook = enc.encode("cooking recipe ingredients")
        enc.unload()
        assert cosine(v_reason, v_math) > cosine(v_reason, v_cook)
