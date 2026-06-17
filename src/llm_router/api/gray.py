"""S4.1 · 灰度分流键(policy 灰度发布机制,D9 design.md line 138/152)。

Phase1 范围 A(机制就位):灰度判定纯函数 + session_id 派生。

⚠ Phase1 行为边界(诚实):路由层内只有一策略(EpsilonGreedy)+ 一 policy_version
("0.1.0"),D7 热更新(多版本共存)未做。故 gray_release 判定后"灰度内/外"在 Phase1 走
**同一策略**——路由层内无可观测行为差异。灰度的真实行为差异在**接入层**(cc-switch 决定
agent 流量是否发到路由层,D9,本仓外)。本模块提供**机制**(判定函数 + session 派生),供
D7/Phase2 接多 policy 版本时直接消费。不假装"新旧切换行为"(Phase1 无第二策略)。

设计:
  - gray_release:hash(session_id) % 100 < gray_percent → 在灰度桶内。
    用 **blake2b**(hashlib)取稳定整数,**非** Python 内置 hash()——后者受 PYTHONHASHSEED
    随机化,跨进程/重启不稳定,会破坏 session 钉定。
  - derive_session_id:explicit(X-Session-Id header)> api_key(Authorization Bearer)派生 > None。
    api_key 派生 = 同 key 同桶 = 天然"按 agent 灰度"(design line 25/128)。

不进 strategy.plan 排序键——灰度是"层外"机制(决定桶成员),不影响字典序排序
(routing-priority-principle:capability→is_free→倍率,不被破坏)。
"""
from __future__ import annotations

import hashlib


def _stable_int(session_id: str) -> int:
    """稳定整数哈希:blake2b 8 字节 → int(大端无符号)。

    确定性跨进程/重启(blake2b 是密码学哈希,输出固定,不受 PYTHONHASHSEED 影响)。
    Python 内置 hash(str) 不可用——每个进程随机化种子,同一串不同进程不同值。
    """
    digest = hashlib.blake2b(session_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def gray_release(session_id: str, gray_percent: int) -> bool:
    """灰度桶判定:hash(session_id) % 100 < gray_percent → 在灰度桶内(True)。

    Args:
        session_id: 请求会话标识(由 derive_session_id 从 X-Session-Id / api_key 派生)。
        gray_percent: 灰度比例 [0,100],超出则夹紧(0→永不灰度,100→全量;>100/<0 安全)。

    Returns:
        True = 该 session 在灰度桶内(走新策略);False = 桶外(走旧策略)。
        Phase1 两者走同一策略(机制就位,行为差异 defer D7/Phase2)。

    确定性:同 session_id + 同 percent → 同结果(跨进程/重启稳定,供 session 钉定)。
    """
    pct = max(0, min(100, gray_percent))
    return _stable_int(session_id) % 100 < pct


def derive_session_id(
    api_key: str | None, explicit: str | None = None
) -> str | None:
    """从请求派生 session_id(D9 灰度切 agent,design line 25/128)。

    优先级:explicit(X-Session-Id header)> api_key(Authorization Bearer)派生 > None。
    api_key 派生 = blake2b(key) hex → 同 key 同串 = 同 agent 同桶(天然按 agent 灰度)。
    空串等同缺失(回退下一优先级)。两者皆无/空 → None(调用方视为不参与灰度判定)。

    安全:派生串是 key 的哈希,**不含**原始 key(防 key 泄漏到 log/trace)。
    """
    if explicit:
        # 防御:清洗换行(X-Session-Id 用户可控,防 log 注入;OpenCode LOW#1)。
        return explicit.replace("\r", "").replace("\n", "")
    if api_key:
        return hashlib.blake2b(api_key.encode("utf-8"), digest_size=16).hexdigest()
    return None
