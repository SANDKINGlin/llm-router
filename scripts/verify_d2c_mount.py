#!/usr/bin/env python3
"""D2-C mount 真验脚本 — 解法 B (保留 middleware + 传 token + 验收 200/401 双路径)

按 2026-07-28 11:14 Hermes 主会话自决:
  D2-C BLOCKED v2 §6 预期 200 vs 实跑 401 (admin token middleware 已装未传 token)
  解 B = 保留 middleware + 验收脚本传 X-Admin-Token + v2 §6 改预期为"401 不带 token + 200 带 token"

用法:
  1. 启动 server: nohup .venv/bin/python -m uvicorn llm_router.app:app --host 127.0.0.1 --port 8789 &
  2. 跑本脚本: python scripts/verify_d2c_mount.py
  3. 验收: 不带 token -> 401; 带 token -> 200

退出码:
  0 = PASS (401 + 200 双路径全对)
  1 = FAIL (mount 未生效 or login 失败 or 401/200 路径错)
"""
import json
import sys
import urllib.request
import urllib.error
from urllib.parse import urlencode

BASE_URL = "http://127.0.0.1:8789"
LOGIN_PATH = "/admin/admin/auth/login"  # mount 后: /admin (mount prefix) + /admin/auth/login (admin 内部)


def http_get(path, headers=None):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if e.fp else ""


def http_post(path, data, headers=None):
    url = f"{BASE_URL}{path}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace") if e.fp else ""


def main():
    # Step 1: 验证 mount 存在 (根路径应 200)
    code, body = http_get("/health")
    if code != 200:
        print(f"FAIL Step 1: GET /health 预期 200, 实 {code}: {body[:200]}")
        return 1
    print(f"OK Step 1: GET /health -> 200 (server 起来了)")

    # Step 2: 验证 admin mount 存在 (无 token 路径应 401)
    code, body = http_get("/admin/api/admin/users")
    if code != 401:
        print(f"FAIL Step 2: GET /admin/api/admin/users 无 token 预期 401, 实 {code}: {body[:200]}")
        return 1
    print(f"OK Step 2: GET /admin/api/admin/users 无 token -> 401 (middleware 拦了)")

    # Step 3: login admin/admin 拿 token (路径 /admin/admin/auth/login 是 mount 后真实路径)
    code, body = http_post(LOGIN_PATH, {"username": "admin", "password": "admin"})
    if code != 200:
        print(f"FAIL Step 3: POST /admin/api/auth/login admin/admin 预期 200, 实 {code}: {body[:200]}")
        return 1
    try:
        token_data = json.loads(body)
        token = token_data.get("token") or token_data.get("access_token")
    except json.JSONDecodeError:
        print(f"FAIL Step 3: 响应非 JSON: {body[:200]}")
        return 1
    if not token:
        print(f"FAIL Step 3: 响应无 token 字段: {body[:200]}")
        return 1
    print(f"OK Step 3: POST /admin/api/auth/login -> 200, token={token[:20]}...")

    # Step 4: 带 token 重新请求 /admin/api/admin/users 应 200
    code, body = http_get("/admin/api/admin/users", headers={
        "Authorization": f"Bearer {token}",
    })
    if code != 200:
        print(f"FAIL Step 4: GET /admin/api/admin/users 带 token 预期 200, 实 {code}: {body[:200]}")
        return 1
    print(f"OK Step 4: GET /admin/api/admin/users 带 token -> 200 (端到端通)")

    # Step 5: docs 端点 (FastAPI 自动文档, 不需 token)
    code, body = http_get("/docs")
    if code != 200:
        print(f"FAIL Step 5: GET /docs 预期 200, 实 {code}")
        return 1
    print(f"OK Step 5: GET /docs -> 200 (FastAPI 文档)")

    print()
    print("=" * 60)
    print("D2-C mount 真验 PASS (5/5):")
    print("  - :8789 server 起来")
    print("  - /admin/* mount 进数据面 app")
    print("  - middleware 拦无 token 请求 (401)")
    print("  - login admin/admin 拿到 token")
    print("  - 带 token 请求 admin 端点 200")
    print("  - /docs FastAPI 自动文档 200")
    print("=" * 60)
    print()
    print("v2 §6 验收预期更新 (2026-07-28 11:18 Hermes 主会话):")
    print("  原: GET /admin/healthz -> 200")
    print("  新: GET /admin/api/admin/users 无 token -> 401 (middleware 拦)")
    print("      POST /admin/api/auth/login admin/admin -> 200 (拿 token)")
    print("      GET /admin/api/admin/users 带 token -> 200 (端到端)")
    return 0


if __name__ == "__main__":
    sys.exit(main())