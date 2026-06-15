"""S1.6 · 内容完整性判定(Phase 1 基础版)。

判定 provider 响应是否"完整"。残缺响应(字段缺失/格式损坏/空)按软失败计入熔断器
(record_failure(reason=SOFT_CONTENT),3 软 = 1 硬),避免 LiteLLM 损坏响应被静默当成功
(修 spec FR-05 / STORM 场景)。

Phase 1 契约:基于 MockProvider `complete(prompt) → (text, model)` 的两元组——
两者均非空即完整。真实 OpenAI/Anthropic dict 字段校验(finish_reason 异常 / JSON 损坏 /
choices 缺失 等)defer S2.x(接真 provider 时,由 caller 在适配层判定后标 SOFT_CONTENT)。

接线:S2.1 Cascade 在 wrap provider 调用后,调 `is_complete(text, model)`:
  - True  → record_success
  - False → record_failure(reason=SOFT_CONTENT)(软失败,不立即 trip)
本切片只建判定函数 + 单测,不接 app.py(守 surgical)。
"""
from __future__ import annotations

from typing import Optional


def is_complete(text: Optional[str], model: Optional[str]) -> bool:
    """Phase 1:响应文本与模型名均非空(去空白后)即完整。

    Args:
        text: provider 返回的响应文本(MockProvider 契约第一元)。
        model: provider 返回的模型名(MockProvider 契约第二元)。

    Returns:
        True 表示完整(成功);False 表示残缺(软失败,计入熔断器)。

    Note:
        真实 provider 的 dict 响应校验(choices/finish_reason/JSON 完整性)defer S2.x。
    """
    if text is None or model is None:
        return False
    if not isinstance(text, str) or not isinstance(model, str):
        return False
    return text.strip() != "" and model.strip() != ""
