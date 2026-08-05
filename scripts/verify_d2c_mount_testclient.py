#!/usr/bin/env python3
"""D2-C mount 真验 (TestClient 版, 不需启 server)

按 2026-07-28 11:32 Hermes 主会话, 用 FastAPI TestClient 模拟 HTTP 客户端.

验证 6 步:
  1. mount /admin 真在 app.routes
  2. /healthz 通
  3. /admin/api/admin/users 无 token -> 401 (middleware 拦)
  4. /admin/admin/auth/login admin/admin -> 200 (X1 strip mount_prefix 已落地)
  5. /admin/api/admin/users 带 token -> 200 (S1 双 secret 同源化后 mount 端到端通)
  6. /docs -> 200 (FastAPI 文档)

已知真阻塞 (2026-07-28 11:42, S1 2026-08-04 已解):
  AuthMiddleware (admin/auth.py L33-34) 白名单硬编码 "/admin/auth/login",
  在 mount /admin 后实际路径变 "/admin/admin/auth/login", 白名单失效.
  2026-07-28 X1 fix: auth.py 加 strip mount_prefix → Step 4 200.
  2026-08-04 S1 fix: auth_enhanced.py SECRET_KEY 跟 auth.py 同源 → Step 5 200.

退出码:
  0 = mount 路径 6/6 PASS (含 Step 4 200 + Step 5 200)
  1 = FAIL
"""
import sys
import os
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute, Mount


def collect_admin_paths(app):
    paths = []
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/admin"):
            paths.append(r.path)
        elif isinstance(r, Mount):
            for sr in r.routes:
                if isinstance(sr, APIRoute):
                    full = (r.path.rstrip("/") + "/" + sr.path.lstrip("/")).replace("//", "/")
                    if full.startswith("/admin"):
                        paths.append(full)
    return paths


def main():
    try:
        from llm_router.app import app
    except ImportError as e:
        print(f"FAIL Step 0: import app 失败: {e}")
        return 1

    client = TestClient(app)

    # Step 1: 验证 mount 真生效 (admin_paths 非空)
    admin_paths = collect_admin_paths(app)
    if not admin_paths:
        print(f"FAIL Step 1: /admin/* 未 mount (admin_subapp 可能是 None)")
        return 1
    print(f"OK Step 1: /admin/* mount 真在 app.routes (发现 {len(admin_paths)} admin 端点)")

    # Step 2: /healthz 通
    r = client.get("/healthz")
    if r.status_code != 200:
        print(f"FAIL Step 2: GET /healthz 预期 200, 实 {r.status_code}: {r.text[:200]}")
        return 1
    print(f"OK Step 2: GET /healthz -> {r.status_code}")

    # Step 3: /admin/api/admin/users 无 token -> 401 (middleware 拦)
    r = client.get("/admin/api/admin/users")
    if r.status_code != 401:
        print(f"FAIL Step 3: GET /admin/api/admin/users 无 token 预期 401, 实 {r.status_code}: {r.text[:200]}")
        return 1
    print(f"OK Step 3: GET /admin/api/admin/users 无 token -> {r.status_code} (middleware 拦)")

    # Step 4: login admin/admin 拿 token — X1 fix (2026-07-28 三方共识): strip mount prefix
    r = client.post("/admin/admin/auth/login", json={"username": "admin", "password": "admin"})
    if r.status_code == 200:
        print(f"OK Step 4: POST /admin/admin/auth/login -> {r.status_code} (X1 strip mount_prefix 已落地, 白名单命中)")
        try:
            token_data = r.json()
            print(f"             token 字段: {list(token_data.keys()) if isinstance(token_data, dict) else token_data}")
        except Exception:
            pass
    elif r.status_code == 401 and "Missing Bearer token" in r.text:
        print(f"FAIL Step 4: POST /admin/admin/auth/login -> 401 Missing Bearer token (X1 未生效)")
        print(f"             根因: AuthMiddleware 白名单 \"/admin/auth/login\" 在 mount 后仍失效")
        return 1
    else:
        print(f"FAIL Step 4: POST /admin/admin/auth/login 预期 200, 实 {r.status_code}: {r.text[:200]}")
        return 1

    # Step 5: /admin/api/admin/users 带 token (mount 端到端鉴权)
    # S1 (2026-08-04): EnhancedAuthManager SECRET_KEY 已跟 auth.py AuthMiddleware 同源
    # (都读 ADMIN_SECRET_KEY env, 默认 "dev-secret-key"). mount 端到端鉴权现在真 200.
    token = r.json().get("token", "") if r.status_code == 200 else ""
    r = client.get("/admin/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        print(f"OK Step 5: GET /admin/api/admin/users 带 token -> {r.status_code} (mount 端到端通)")
    elif r.status_code == 401:
        # S1 fix 后不应再 WARN Step 5; 401 = 新 regression, 必须 FAIL
        print(f"FAIL Step 5: GET /admin/api/admin/users 带 token -> 401 (S1 secret 同源化已落地, 401 不应再出现)")
        print(f"             回归排查: 检查 auth.py auth_enhanced.py SECRET_KEY 是否同源")
        print(f"             env: ADMIN_SECRET_KEY={'set' if os.environ.get('ADMIN_SECRET_KEY') else 'unset (default dev-secret-key)'}")
        return 1
    else:
        print(f"FAIL Step 5: GET /admin/api/admin/users 带 token 预期 200, 实 {r.status_code}")
        return 1

    # Step 6: /docs -> 200
    r = client.get("/docs")
    if r.status_code != 200:
        print(f"FAIL Step 6: GET /docs 预期 200, 实 {r.status_code}")
        return 1
    print(f"OK Step 6: GET /docs -> {r.status_code} (FastAPI 文档)")

    print()
    print("=" * 60)
    print("D2-C mount 真验 (X1 strip mount_prefix fix + S1 双 secret 同源化):")
    print("  Step 1 OK: /admin/* mount 真在 app.routes")
    print("  Step 2 OK: /healthz 通")
    print("  Step 3 OK: middleware 拦无 token (401)")
    print("  Step 4 OK: login POST 真 200 (X1 strip mount_prefix 让白名单命中)")
    print("  Step 5 OK: 带 token GET /admin/api/admin/users 端到端真 200 (S1 双 secret 同源化)")
    print("  Step 6 OK: /docs 通")
    print("=" * 60)
    print("总体: PASS (6/6 OK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())