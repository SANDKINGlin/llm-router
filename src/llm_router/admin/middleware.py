"""Admin sub-app ASGI middleware · D7 透明重写

D7 fix (CC 复核关切): 单前缀 /admin/rollback 带认证时 404, 因 Starlette mount
剥 prefix 后 admin_subapp 收到 /rollback (跟 /admin/rollback 全路径注册不匹配).
加 D7SinglePrefixRewriteMiddleware 在 main app 上做透明重写:

  - 单前缀 /admin/rollback → scope["path"] 改写成 /admin/admin/rollback
  - middleware 装在 mount 之前, mount 接 scope 时已是双前缀路径
  - admin_subapp 仍按双前缀匹配 (保持 D7 已落地的双前缀注册)
  - 客户端契约不变 (单前缀 /admin/rollback 即可命中)

非 D7 admin 子路径 (e.g. /admin/keys, /admin/auth/login) 已经在 admin_subapp 用
全路径注册, mount 剥 prefix 后变成 /keys, /auth/login → 也都 404 (D2-C 已知
bug, 不属 D7 范围). 本 middleware 只针对 D7 admin_rollback 单前缀做重写,
其他路径保持原行为, 不做 scope creep.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# D7 endpoint 在 admin_subapp 用全路径 /admin/rollback 注册. 客户端期望单前缀契约.
# 重写列表可扩展 (未来 /admin/xxx 单前缀契约补齐).
REWRITE_MAP = {
    "/admin/rollback": "/admin/admin/rollback",
}


class D7SinglePrefixRewriteMiddleware:
    """ASGI middleware: 单前缀 → 双前缀透明重写 (D7 切片专用).

    行为:
      - 请求 path 在 REWRITE_MAP 中 → 改写 scope["path"]
      - 同时改写 raw_path (raw_path 是 path 的 bytes 形式, Starlette route 用 path 匹配)
      - query string, headers, body 全透传 (不破坏请求语义)
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in REWRITE_MAP:
            new_path = REWRITE_MAP[path]
            scope["path"] = new_path
            # raw_path 是 path 的 bytes 形式 (RFC 3986), route 匹配有时用 raw_path
            raw_path = scope.get("raw_path")
            if raw_path is not None:
                scope["raw_path"] = new_path.encode("latin-1")

        await self.app(scope, receive, send)