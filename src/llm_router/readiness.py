"""就绪探针逻辑。S0.0:恒就绪。
readiness 切片补:三库(trace/ledger/circuit)可写 + router-policy 加载 + CB 状态恢复。
"""
from __future__ import annotations


def check_ready() -> tuple[bool, str]:
    """返回 (是否就绪, 说明)。S0.0:恒 True。"""
    # TODO(readiness 切片): data/ 三库可写 + config.policy_loaded + CB 恢复
    return True, "s0.0 skeleton ready (real checks pending readiness slice)"
