"""S2.9 子片 0.1 · 能力匹配 embedding 编码器 + 纯 Python cosine(② 匹配层,Phase2)。

spec: capability-matching/spec.md(Req: bge-small 懒加载,编码后卸载)。
design 约束#1(防卡死):bge-small 130MB 懒加载+卸载,峰值<250MB,稳态<80MB。
design R1:bge-small 实测,必要时改更小 embedding 或纯 API embedding。

**本切片范围**:Encoder 协议 + HashEncoder(确定性,无 torch)+ 纯 Python cosine。
真 BgeEncoder(sentence-transformers + torch + 130MB 模型懒加载/卸载)**defer 到模型就绪子片**
(诚实边界:venv 无 numpy/torch/ST,1Gi free;装 2GB torch + 下模型风险高,design R1 授权替代)。
HashEncoder 用 hashlib 驱动(确定性,跨运行稳定,无 Math.random),作测试 + Phase1 占位;
真 bge 通过 Encoder 协议槽位接入(BgeMatcher 吃 Encoder,不绑死实现)。

纯 Python cosine:dot / (|a|·|b|),无 numpy 依赖(同 S3.4 Wilson 用 stdlib 的纪律)。
fail-loud:维度不匹配 / 零向量 → ValueError(不静默返 0/NaN,守 gotchas「失败要响亮」)。
"""
from __future__ import annotations

import hashlib
import math
from typing import Protocol, Sequence

Vector = Sequence[float]


class Encoder(Protocol):
    """文本 → 固定维度向量(BgeMatcher 吃此协议,不绑死 bge/HashEncoder)。

    确定性契约:同文本同向量(跨运行稳定,供缓存 + 测试)。
    """

    def encode(self, text: str) -> Vector:
        ...


def cosine(a: Vector, b: Vector) -> float:
    """余弦相似度 = a·b / (|a|·|b|),纯 Python(stdlib,无 numpy)。

    fail-loud:
      - 维度不一致 → ValueError(防隐 bug,不静默截断/补零)。
      - 零向量(|a|=0 或 |b|=0)→ ValueError(除零无定义,不返 NaN)。
    返回 [-1.0, 1.0]。
    """
    if len(a) != len(b):
        raise ValueError(
            f"cosine 维度不一致: len(a)={len(a)} len(b)={len(b)}(不静默截断)"
        )
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        raise ValueError("cosine 零向量无定义(|a|=0 或 |b|=0),不返 NaN 静默")
    return dot / (math.sqrt(na) * math.sqrt(nb))


class HashEncoder:
    """确定性哈希编码器(hashlib 驱动,无 torch/bge)。

    文本 → dim 维归一化向量。用字符 n-gram(共享子串提升相似文本 cosine,作语义代理)
    + sha256 哈希散列到 dim 个桶,累加后 L2 归一化(单位向量,cosine 算内积即可)。

    **确定性**:同文本同向量(无 Math.random;hashlib 跨运行/跨进程稳定)。
    **语义代理**:共享 n-gram 的文本 cosine 高于无关文本(供测试可验;真 bge 接入后更准)。

    非 bge:不捕获深层语义,仅 n-gram 重叠。Phase1 占位 + 测试确定性用;生产真匹配
    待 BgeEncoder(sentence-transformers 懒加载)接入 Encoder 协议槽位。
    """

    def __init__(self, dim: int = 64, ngram: int = 3) -> None:
        if dim <= 0:
            raise ValueError(f"HashEncoder dim 须 >0;实际 {dim}")
        if ngram <= 0:
            raise ValueError(f"HashEncoder ngram 须 >0;实际 {ngram}")
        self._dim = dim
        self._ngram = ngram

    def encode(self, text: str) -> list[float]:
        """文本 → dim 维 L2 归一化向量(n-gram 哈希散列)。"""
        vec = [0.0] * self._dim
        norm = text.strip().lower()
        if not norm:
            return vec  # 空文本 → 零向量(cosine 会 fail-loud,调用方应避免)
        # 字符 n-gram(含整体 token 的 word n-gram 作语义代理)。
        tokens = self._tokens(norm)
        grams = self._ngrams(norm) + tokens
        for g in grams:
            h = int(hashlib.sha256(g.encode("utf-8")).hexdigest(), 16)
            vec[h % self._dim] += 1.0
        # L2 归一化(单位向量)。
        mag = math.sqrt(sum(v * v for v in vec))
        if mag == 0.0:
            return vec
        return [v / mag for v in vec]

    @staticmethod
    def _tokens(norm: str) -> list[str]:
        """分词(空格分隔),返各 token + token 自身作 gram(共享 token 提升相似性)。"""
        return [t for t in norm.split() if t]

    def _ngrams(self, norm: str) -> list[str]:
        """字符 n-gram(跨 token 边界,捕获拼写相似性)。"""
        if len(norm) < self._ngram:
            return [norm] if norm else []
        return [norm[i : i + self._ngram] for i in range(len(norm) - self._ngram + 1)]
