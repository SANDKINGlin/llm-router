"""就绪探针逻辑(真实检查,修审计 F1)。

检查:① policy 加载且有 provider ② data 目录可写(三库 trace/ledger/circuit 可 init 的前提)。
未就绪 → /healthz 503,编排层(Docker HEALTHCHECK)据此判健康(不再恒 ready 假绿)。

CB 恢复:CircuitBreaker 在 app import 期 _init_db+_load_state 已跑;app 起来即恢复完成,
不在 healthz 再查(若 CB init 失败,app 根本起不来,在 startup 暴露而非运行期 healthz)。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .config import Policy, policy

_DATA_DIR = Path(
    os.environ.get(
        "LLM_ROUTER_DATA_DIR",
        str(Path(__file__).resolve().parents[2] / "data"),
    )
)
_PROBE_FILE = ".readiness_probe"


def check_ready(
    *,
    data_dir: str | Path | None = None,
    policy_fn: Callable[[], Policy] | None = None,
) -> tuple[bool, dict[str, str]]:
    """返回 (是否就绪, 各项检查详情)。

    Args(可注入,测试用;默认查真 data 目录 + 真 policy):
      data_dir: 数据目录(三库所在);默认 <repo>/data。
      policy_fn: 返回 Policy 的可调用;默认 config.policy。
    """
    ddir = Path(data_dir) if data_dir is not None else _DATA_DIR
    _policy = policy_fn or policy
    checks: dict[str, str] = {}

    # ① policy 加载 + 至少一个 provider(否则路由无候选)。
    try:
        pol = _policy()
        checks["policy"] = "ok" if pol.providers else "no_providers"
    except Exception as e:
        checks["policy"] = f"error:{type(e).__name__}"

    # ② data 目录可写:探针写删(比 os.access 诚实,能抓满盘/权限/挂载问题)。
    try:
        if ddir.is_symlink():
            ddir.unlink()
        ddir.mkdir(parents=True, exist_ok=True)
        probe = ddir / _PROBE_FILE
        probe.write_text("ok")
        probe.unlink()
        checks["data_writable"] = "ok"
    except Exception as e:
        checks["data_writable"] = f"error:{type(e).__name__}"

    ok = all(v == "ok" for v in checks.values())
    return ok, checks
