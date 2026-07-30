"""设置注册表：动态管理可调参数。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Setting:
    """可调参数定义。"""
    name: str
    value: Any
    type: type
    min: float | int | None = None
    max: float | int | None = None
    description: str = ""
    category: str = "general"
    on_change: Callable | None = None  # 回调函数


class SettingsRegistry:
    """设置注册表。"""

    def __init__(self):
        self._settings: dict[str, Setting] = {}
        self._lock = asyncio.Lock()

    def register(self, setting: Setting) -> None:
        """注册可调参数。"""
        self._settings[setting.name] = setting

    async def get_all(self) -> dict[str, dict]:
        """获取所有设置。"""
        async with self._lock:
            return {
                name: {
                    "value": setting.value,
                    "type": setting.type.__name__,
                    "min": setting.min,
                    "max": setting.max,
                    "description": setting.description,
                    "category": setting.category,
                }
                for name, setting in self._settings.items()
            }

    async def get(self, name: str) -> Any:
        """获取单个设置值。"""
        async with self._lock:
            if name not in self._settings:
                raise ValueError(f"Setting not found: {name}")
            return self._settings[name].value

    async def update(self, name: str, value: Any) -> None:
        """更新设置值。"""
        async with self._lock:
            if name not in self._settings:
                raise ValueError(f"Setting not found: {name}")

            setting = self._settings[name]

            # 类型验证
            try:
                value = setting.type(value)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid type for {name}, expected {setting.type.__name__}")

            # 范围验证
            if setting.min is not None and value < setting.min:
                raise ValueError(f"{name} must be >= {setting.min}")
            if setting.max is not None and value > setting.max:
                raise ValueError(f"{name} must be <= {setting.max}")

            old_value = setting.value
            setting.value = value

            # 触发回调
            if setting.on_change:
                await setting.on_change(old_value, value)


# 全局注册表实例
_registry = SettingsRegistry()


def get_registry() -> SettingsRegistry:
    """获取全局注册表。"""
    return _registry


def register_core_settings(policy):
    """注册核心设置（从现有policy对象）。"""
    registry = get_registry()

    # 灰度百分比
    registry.register(Setting(
        name="gray_percent",
        value=policy.gray_percent,
        type=int,
        min=0,
        max=100,
        description="灰度发布百分比（0-100）",
        category="gray_release",
    ))

    # ε值
    registry.register(Setting(
        name="epsilon",
        value=0.1,  # 默认值，需要从实际配置读取
        type=float,
        min=0.0,
        max=1.0,
        description="ε-greedy探索率",
        category="routing",
    ))

    # 熔断阈值
    registry.register(Setting(
        name="circuit_threshold",
        value=5,  # 默认值
        type=int,
        min=1,
        max=100,
        description="熔断触发失败次数阈值",
        category="resilience",
    ))

    # 退避冷却时间（秒）
    registry.register(Setting(
        name="cooldown_seconds",
        value=60,  # 默认值
        type=int,
        min=10,
        max=3600,
        description="熔断后冷却时间（秒）",
        category="resilience",
    ))

    # token预算
    registry.register(Setting(
        name="token_budget",
        value=1000000,  # 默认值
        type=int,
        min=0,
        description="token用量预算（软限制）",
        category="cost",
    ))
