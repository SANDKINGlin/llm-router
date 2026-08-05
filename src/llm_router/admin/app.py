"""Admin REST API (was :8790 standalone, v2 D2-C mount 进 :8789 /admin/* via SharedASGIMiddleware)。密钥管理、监控、配置热重载。"""
from __future__ import annotations

import os
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, status, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .auth import AuthMiddleware, generate_token, get_audit_logger, SecurityHeadersMiddleware
from .secrets import SecretStore, create_secret_store
from ..config import policy
from .providers import provider_manager, ProviderCreate, ProviderUpdate, ProviderResponse, ProviderListResponse
from .keys import key_manager, require_permission
from .auth_enhanced import (
    enhanced_auth_manager,
    require_enhanced_permission,
    require_enhanced_auth,
    get_current_user_enhanced,
    get_current_user_with_permission,
    get_current_user_auth
)
from .policy_sync import RollbackRequest, refresh_policy_state


# 创建SecretStore实例（默认环境变量后端）
secret_store: SecretStore = create_secret_store(backend="env")

# 创建Admin FastAPI应用
admin_app = FastAPI(title="LLM Router Admin API", version="0.1.0")

# 添加认证中间件
admin_app.add_middleware(AuthMiddleware)
admin_app.add_middleware(SecurityHeadersMiddleware)

# 配置模板引擎
templates = Jinja2Templates(directory="/home/lin/projects/llm-router/src/llm_router/ui/templates")

# 挂载静态文件服务
admin_app.mount("/static", StaticFiles(directory="/home/lin/projects/llm-router/src/llm_router/ui/static"), name="static")


# Pydantic模型
class KeyCreate(BaseModel):
    provider: str
    key: str


class KeyUpdate(BaseModel):
    key: str


class KeyRotate(BaseModel):
    new_key: str


class LoginRequest(BaseModel):
    username: str
    password: str


# ===== 认证接口 =====

@admin_app.post("/admin/auth/login")
async def login(req: LoginRequest) -> dict:
    """登录接口，生成Bearer Token。

    S1 (2026-08-04): 改用 enhanced_auth_manager.create_token() 签 3 段 token
    (不是 auth.py:generate_token() 2 段简化 token), 跟 /api/admin/users 等
    require_enhanced_permission 装饰器期望的 token 格式对齐.
    否则 mount 端到端鉴权 (Step 5) 会因 token 格式不兼容而 401.
    """
    # 简化版：用户名密码验证（生产环境用真实数据库）
    if req.username == "admin" and req.password == "admin":
        # S1: 跟 /api/admin/auth/login 用同一签发路径, 保持 token 格式一致
        user = enhanced_auth_manager.authenticate_user(req.username, req.password) or {
            "id": 1, "username": req.username, "role": "admin", "permissions": ["admin", "view", "manage_keys"]
        }
        token = enhanced_auth_manager.create_token(user)
        get_audit_logger().log("admin", "LOGIN", {"username": req.username}, result="SUCCESS")
        return {"token": token, "expires_in": 86400}
    else:
        get_audit_logger().log("admin", "LOGIN_FAILED", {"username": req.username}, result="FAILURE")
        raise HTTPException(status_code=401, detail="Invalid credentials")


# ===== 认证和用户管理接口 =====

@admin_app.post("/api/admin/auth/login")
async def login(request: Request):
    """用户登录 - 返回JWT token。"""
    try:
        # 从请求体获取凭据
        import json
        body = await request.body()
        credentials = json.loads(body)
        username = credentials.get("username")
        password = credentials.get("password")

        if not username or not password:
            raise HTTPException(status_code=400, detail="缺少用户名或密码")

        # 验证用户
        user = enhanced_auth_manager.authenticate_user(username, password)
        if not user:
            raise HTTPException(status_code=401, detail="无效的用户名或密码")

        # 创建token
        token = enhanced_auth_manager.create_token(user)

        # 记录登录事件
        enhanced_auth_manager.log_auth_event(user["id"], "LOGIN", f"User {username} logged in")

        return {
            "access_token": token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "permissions": user["permissions"]
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"登录失败: {str(e)}")


@admin_app.post("/api/admin/auth/logout")
async def logout(request: Request, current_user: dict = Depends(get_current_user_auth)):
    """用户登出。"""
    try:
        username = current_user.get("username", "unknown")
        user_id = current_user.get("user_id")

        # 记录登出事件
        enhanced_auth_manager.log_auth_event(user_id, "LOGOUT", f"User {username} logged out")

        return {"message": "登出成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"登出失败: {str(e)}")


@admin_app.get("/api/admin/users")
@require_enhanced_permission("admin")
async def list_users(request: Request, current_user: dict = None):
    """列出所有用户（需要admin权限）。"""
    try:
        users = enhanced_auth_manager.list_users()
        return {"users": users, "total": len(users)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取用户列表失败: {str(e)}")


@admin_app.post("/api/admin/users")
@require_enhanced_permission("admin")
async def create_user(request: Request, current_user: dict = None):
    """创建新用户（需要admin权限）。"""
    try:
        import json
        body = await request.body()
        user_data = json.loads(body)

        username = user_data.get("username")
        password = user_data.get("password")
        role = user_data.get("role", "view")
        permissions = user_data.get("permissions", [])

        if not username or not password:
            raise HTTPException(status_code=400, detail="缺少用户名或密码")

        if role not in ["view", "operate", "admin"]:
            raise HTTPException(status_code=400, detail="无效的角色")

        user_id = enhanced_auth_manager.create_user(username, password, role, permissions)

        return {
            "message": "用户创建成功",
            "user_id": user_id,
            "username": username,
            "role": role
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建用户失败: {str(e)}")


@admin_app.put("/api/admin/users/{username}/role")
@require_enhanced_permission("admin")
async def update_user_role(username: str, request: Request, current_user: dict = None):
    """更新用户角色（需要admin权限）。"""
    try:
        import json
        body = await request.body()
        update_data = json.loads(body)

        new_role = update_data.get("role")
        permissions = update_data.get("permissions")

        if not new_role:
            raise HTTPException(status_code=400, detail="缺少新角色")

        if new_role not in ["view", "operate", "admin"]:
            raise HTTPException(status_code=400, detail="无效的角色")

        success = enhanced_auth_manager.update_user_role(username, new_role, permissions)
        if not success:
            raise HTTPException(status_code=404, detail=f"用户不存在: {username}")

        return {"message": f"用户 {username} 角色已更新为 {new_role}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新用户角色失败: {str(e)}")


@admin_app.delete("/api/admin/users/{username}")
@require_enhanced_permission("admin")
async def delete_user(username: str, request: Request, current_user: dict = None):
    """删除用户（需要admin权限）。"""
    try:
        if username == current_user.get("username"):
            raise HTTPException(status_code=400, detail="不能删除自己的账号")

        success = enhanced_auth_manager.delete_user(username)
        if not success:
            raise HTTPException(status_code=404, detail=f"用户不存在: {username}")

        return {"message": f"用户 {username} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除用户失败: {str(e)}")


# ===== 密钥管理接口 =====

@admin_app.get("/api/admin/keys")
async def list_keys(request: Request, current_user: dict = Depends(get_current_user_auth)):
    """获取所有provider密钥（key字段掩码显示）。"""
    providers = [p.name for p in policy().providers]
    keys = []
    for provider in providers:
        key = await secret_store.get(provider)
        if key:
            # 使用key_manager进行掩码处理
            masked = key_manager.mask_key(key)
            keys.append({
                "provider": provider,
                "key": masked,
                "has_key": True,
                "key_length": len(key)  # 返回密钥长度供前端参考
            })
        else:
            keys.append({
                "provider": provider,
                "key": None,
                "has_key": False,
                "key_length": 0
            })

    return {"keys": keys}


@admin_app.get("/api/admin/keys/{provider}/reveal")
async def reveal_key(
    provider: str,
    request: Request,
    current_user: dict = Depends(get_current_user_with_permission("admin"))
):
    """查看指定provider的完整API密钥（需要admin权限）。"""
    try:
        # 验证provider存在
        if provider not in [p.name for p in policy().providers]:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 获取完整密钥
        key = await secret_store.get(provider)
        if not key:
            raise HTTPException(status_code=404, detail=f"No key found for provider: {provider}")

        # 记录密钥查看操作到审计日志
        key_manager.log_key_access(user_id=None, provider=provider, action="KEY_REVEAL", details="Full key viewed")

        # 返回完整密钥
        return {
            "provider": provider,
            "key": key,
            "revealed_at": datetime.now().isoformat(),
            "warning": "这是一个一次性操作。请妥善保管密钥。"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reveal key: {str(e)}")


@admin_app.get("/api/admin/keys/{provider}/history")
async def get_key_history(provider: str, limit: int = 5, *, request: Request, current_user: dict = Depends(get_current_user_auth)):
    """获取指定provider的密钥操作历史。"""
    try:
        # 验证provider存在
        if provider not in [p.name for p in policy().providers]:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 获取历史记录
        history = key_manager.get_key_history(provider, limit)

        return {
            "provider": provider,
            "history": history,
            "total": len(history)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get key history: {str(e)}")


@admin_app.get("/api/admin/keys/{provider}/expiration")
async def get_key_expiration(provider: str, request: Request, current_user: dict = Depends(get_current_user_auth)):
    """获取指定provider的密钥过期信息。"""
    try:
        # 验证provider存在
        if provider not in [p.name for p in policy().providers]:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 获取密钥数据
        key_data = {}
        warning = key_manager.get_key_expiration_warning(provider, key_data)

        return {
            "provider": provider,
            "has_key": True,
            "expiration_warning": warning,
            "status": "warning" if warning else "ok"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get expiration info: {str(e)}")


@admin_app.get("/admin/keys/form", response_class=HTMLResponse)
async def keys_form_page(request: Request):
    """显示添加密钥表单 - 提供provider选择和密钥输入。"""
    try:
        providers = [p.name for p in policy().providers]

        # 生成provider下拉选项
        provider_options = "\n".join([
            f'<option value="{p}">{p}</option>'
            for p in providers
        ])

        form_html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>添加密钥 - LLM Router Admin</title>
    <link rel="stylesheet" href="/static/css/admin.css">
</head>
<body>
    <div class="container">
        <div class="keys-container">
            <h2>添加API密钥</h2>
            <form id="key-form" onsubmit="handleSubmit(event)">
                <div class="form-group">
                    <label for="provider">选择Provider</label>
                    <select id="provider" name="provider" required>
                        <option value="">-- 请选择 --</option>
                        {provider_options}
                    </select>
                </div>
                <div class="form-group">
                    <label for="key">API密钥</label>
                    <input type="password" id="key" name="key" required placeholder="请输入API密钥">
                </div>
                <div class="button-group">
                    <button type="submit" class="btn btn-primary">提交</button>
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">取消</button>
                </div>
            </form>
        </div>
    </div>
    <script>
        async function handleSubmit(e) {{
            e.preventDefault();
            const provider = document.getElementById('provider').value;
            const key = document.getElementById('key').value;

            if (!provider || !key) {{
                alert('请填写完整信息');
                return;
            }}

            try {{
                const response = await fetch('/admin/keys', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{provider, key}})
                }});

                const result = await response.json();

                if (response.ok) {{
                    alert('密钥添加成功！');
                    window.location.href = '/admin/keys';
                }} else {{
                    alert('添加失败: ' + (result.detail || '未知错误'));
                }}
            }} catch (error) {{
                alert('请求错误: ' + error.message);
            }}
        }}

        function closeModal() {{
            window.location.href = '/admin/keys';
        }}
    </script>
    <style>
        .keys-container {{
            max-width: 500px;
            margin: 50px auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }}
        .form-group select,
        .form-group input {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .button-group {{
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }}
        .btn-primary {{
            background: #007bff;
            color: white;
        }}
        .btn-secondary {{
            background: #6c757d;
            color: white;
        }}
    </style>
</body>
</html>
        """

        return HTMLResponse(content=form_html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"表单生成失败: {str(e)}")


@admin_app.get("/admin/keys/{provider}")
async def get_key(provider: str):
    """获取单个provider密钥详情（key完全隐藏）。"""
    key = await secret_store.get(provider)
    if key is None:
        raise HTTPException(status_code=404, detail=f"Key not found for provider: {provider}")

    return {
        "provider": provider,
        "has_key": True,
        "key_hidden": True  # 不返回明文
    }


@admin_app.post("/admin/keys")
async def create_key(req: KeyCreate):
    """创建provider密钥。"""
    # 验证provider存在
    providers = [p.name for p in policy().providers]
    if req.provider not in providers:
        raise HTTPException(status_code=404, detail=f"Provider not found: {req.provider}")

    # 检查是否已存在
    existing = await secret_store.get(req.provider)
    if existing:
        raise HTTPException(status_code=409, detail=f"Key already exists for provider: {req.provider}")

    # 创建密钥
    await secret_store.set(req.provider, req.key)
    get_audit_logger().log("admin", "CREATE_KEY", {"provider": req.provider})

    return {"status": "created", "provider": req.provider}


@admin_app.put("/admin/keys/{provider}")
async def update_key(provider: str, req: KeyUpdate):
    """更新provider密钥。"""
    # 验证provider存在
    providers = [p.name for p in policy().providers]
    if provider not in providers:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

    # 更新密钥
    await secret_store.set(provider, req.key)
    get_audit_logger().log("admin", "UPDATE_KEY", {"provider": provider})

    return {"status": "updated", "provider": provider}


@admin_app.delete("/admin/keys/{provider}")
async def delete_key(provider: str):
    """删除provider密钥。"""
    # 验证provider存在
    providers = [p.name for p in policy().providers]
    if provider not in providers:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

    # 删除密钥
    await secret_store.delete(provider)
    get_audit_logger().log("admin", "DELETE_KEY", {"provider": provider})

    return {"status": "deleted", "provider": provider}


@admin_app.post("/admin/keys/{provider}/rotate")
async def rotate_key(provider: str, req: KeyRotate):
    """轮换provider密钥（原子性替换，失败回滚）。"""
    # 验证provider存在
    providers = [p.name for p in policy().providers]
    if provider not in providers:
        raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

    # 保存旧密钥（用于回滚）
    old_key = await secret_store.get(provider)

    try:
        # 设置新密钥
        await secret_store.set(provider, req.new_key)

        # TODO: 触发熔断器回滚（需要Cascade实例引用）
        # cascade.apply_policy(...)

        get_audit_logger().log("admin", "ROTATE_KEY", {"provider": provider})
        return {"status": "rotated", "provider": provider}

    except Exception as e:
        # 失败回滚
        if old_key:
            await secret_store.set(provider, old_key)

        get_audit_logger().log("admin", "ROTATE_KEY_FAILED", {"provider": provider, "error": str(e)})


# ===== Provider Management API =====

@admin_app.post("/api/admin/providers")
async def create_provider(
    req: ProviderCreate,
    request: Request,
    current_user: dict = Depends(get_current_user_with_permission("operate"))
):
    """创建新provider。"""
    try:
        provider_id = provider_manager.create_provider(req)
        get_audit_logger().log("admin", "CREATE_PROVIDER", {"provider": req.name, "id": provider_id})
        return {"status": "created", "provider": req.name, "id": provider_id}
    except HTTPException:
        raise
    except Exception as e:
        get_audit_logger().log("admin", "CREATE_PROVIDER_FAILED", {"provider": req.name, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Provider creation failed: {str(e)}")


@admin_app.get("/api/admin/providers")
async def list_providers(request: Request, current_user: dict = Depends(get_current_user_auth)):
    """获取所有provider列表。"""
    try:
        providers = provider_manager.list_providers()
        return ProviderListResponse(
            providers=[ProviderResponse(**p) for p in providers],
            total=len(providers)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list providers: {str(e)}")


@admin_app.get("/admin/providers/list", response_class=HTMLResponse)
async def providers_list_html(request: Request, current_user: dict = Depends(get_current_user_auth)):
    """返回HTML格式的provider列表（用于HTMX）。"""
    try:
        providers = provider_manager.list_providers()

        if not providers:
            return '<tr><td colspan="6" style="text-align:center;color:#999;">暂无Provider</td></tr>'

        rows_html = ""
        for p in providers:
            name = p.get('name', 'unknown')
            tier = p.get('tier', 'unknown')
            base_url = p.get('base_url', '')
            is_active = p.get('is_active', 1)
            models = p.get('models', [])

            # 状态标签
            status_class = "enabled" if is_active else "disabled"
            status_text = "可用" if is_active else "不可用"

            # Tier标签
            tier_class = f"tier-{tier}" if tier in ['strong', 'medium', 'fast'] else ''
            tier_label = tier.upper() if tier else 'UNKNOWN'

            rows_html += f"""
            <tr>
                <td><strong>{name}</strong></td>
                <td><span class="provider-tier {tier_class}">{tier_label}</span></td>
                <td style="font-family:monospace;font-size:12px;color:#666;">{base_url[:50]}{'...' if len(base_url) > 50 else ''}</td>
                <td><span class="provider-status {status_class}">{status_text}</span></td>
                <td>{len(models) if isinstance(models, list) else 0}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="editProvider('{name}')">编辑</button>
                    <button class="btn btn-sm btn-danger" onclick="confirmDeleteProvider('{name}')">删除</button>
                    <button class="btn btn-sm btn-secondary" onclick="toggleProviderStatus('{name}', {1 if is_active else 0})">
                        {'禁用' if is_active else '启用'}
                    </button>
                </td>
            </tr>
            """

        return rows_html
    except Exception as e:
        return f'<tr><td colspan="6" style="text-align:center;color:red;">加载失败: {str(e)}</td></tr>'


@admin_app.get("/api/admin/providers/{provider}/config")
@require_enhanced_auth
async def get_provider_config(provider: str, request: Request, current_user: dict = Depends(get_current_user_auth)):
    """获取单个provider配置详情。"""
    try:
        provider = provider_manager.get_provider(provider)
        if not provider:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")
        return provider
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get provider config: {str(e)}")


@admin_app.put("/api/admin/providers/{provider}/config")
async def update_provider_config(
    provider: str,
    req: ProviderUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user_with_permission("operate"))
):
    """更新provider配置。"""
    try:
        # 验证provider存在
        existing = provider_manager.get_provider(provider)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 更新配置
        updated = provider_manager.update_provider(provider, req)
        if updated:
            get_audit_logger().log("admin", "UPDATE_PROVIDER_CONFIG", {"provider": provider})
            return {"status": "updated", "provider": provider}
        else:
            return {"status": "unchanged", "provider": provider}
    except HTTPException:
        raise
    except Exception as e:
        get_audit_logger().log("admin", "UPDATE_CONFIG_FAILED", {"provider": provider, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Config update failed: {str(e)}")


@admin_app.delete("/api/admin/providers/{provider}")
async def delete_provider(
    provider: str,
    request: Request,
    current_user: dict = Depends(get_current_user_with_permission("admin"))
):
    """删除provider（软删除，设置is_active=0）。"""
    try:
        deleted = provider_manager.delete_provider(provider)
        if deleted:
            get_audit_logger().log("admin", "DELETE_PROVIDER", {"provider": provider})
            return {"status": "deleted", "provider": provider}
        else:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")
    except HTTPException:
        raise
    except Exception as e:
        get_audit_logger().log("admin", "DELETE_PROVIDER_FAILED", {"provider": provider, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Provider deletion failed: {str(e)}")


@admin_app.get("/api/admin/providers/{provider}/config/history")
async def get_provider_config_history(provider: str, request: Request, current_user: dict = Depends(get_current_user_auth)):
    """获取provider配置变更历史。"""
    try:
        # 验证provider存在
        existing = provider_manager.get_provider(provider)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 获取配置历史
        with provider_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM config_history
                WHERE provider_id = ?
                ORDER BY changed_at DESC
                LIMIT 10
            """, (existing['id'],))
            rows = cursor.fetchall()
            history = [dict(row) for row in rows]
            return {"provider": provider, "history": history}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get config history: {str(e)}")


@admin_app.post("/api/admin/providers/{provider}/config/rollback/{version}")
async def rollback_provider_config(
    provider: str,
    version: int,
    request: Request,
    current_user: dict = Depends(get_current_user_with_permission("admin"))
):
    """回滚provider配置到指定版本。"""
    try:
        # 验证provider存在
        existing = provider_manager.get_provider(provider)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 执行回滚
        success = provider_manager.rollback_provider_config(provider, version)
        if success:
            get_audit_logger().log("admin", "ROLLBACK_PROVIDER_SUCCESS", {"provider": provider, "version": version})
            return {"status": "rolled_back", "provider": provider, "version": version}
        else:
            raise HTTPException(status_code=400, detail=f"Rollback failed for provider: {provider}")
    except HTTPException:
        raise
    except Exception as e:
        get_audit_logger().log("admin", "ROLLBACK_PROVIDER_FAILED", {"provider": provider, "version": version, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")


# ===== HTML页面路由 =====

@admin_app.get("/admin/", response_class=HTMLResponse)
async def admin_home(request: Request):
    """主页路由 - 渲染base.html主框架。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "base.html"
        html_content = template_path.read_text()
        # 清理Jinja2语法，浏览器会忽略这些标签
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页路由 - 渲染login.html。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "login.html"
        html_content = template_path.read_text()
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/keys/form", response_class=HTMLResponse)
async def keys_form_page(request: Request):
    """显示添加密钥表单 - 提供provider选择和密钥输入。"""
    try:
        providers = [p.name for p in policy().providers]

        # 生成provider下拉选项
        provider_options = "\n".join([
            f'<option value="{p}">{p}</option>'
            for p in providers
        ])

        form_html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>添加密钥 - LLM Router Admin</title>
    <link rel="stylesheet" href="/static/css/admin.css">
</head>
<body>
    <div class="container">
        <div class="keys-container">
            <h2>添加API密钥</h2>
            <form id="key-form" onsubmit="handleSubmit(event)">
                <div class="form-group">
                    <label for="provider">选择Provider</label>
                    <select id="provider" name="provider" required>
                        <option value="">-- 请选择 --</option>
                        {provider_options}
                    </select>
                </div>
                <div class="form-group">
                    <label for="key">API密钥</label>
                    <input type="password" id="key" name="key" required placeholder="请输入API密钥">
                </div>
                <div class="button-group">
                    <button type="submit" class="btn btn-primary">提交</button>
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">取消</button>
                </div>
            </form>
        </div>
    </div>
    <script>
        async function handleSubmit(e) {{
            e.preventDefault();
            const provider = document.getElementById('provider').value;
            const key = document.getElementById('key').value;

            if (!provider || !key) {{
                alert('请填写完整信息');
                return;
            }}

            try {{
                const response = await fetch('/admin/keys', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{provider, key}})
                }});

                const result = await response.json();

                if (response.ok) {{
                    alert('密钥添加成功！');
                    window.location.href = '/admin/keys';
                }} else {{
                    alert('添加失败: ' + (result.detail || '未知错误'));
                }}
            }} catch (error) {{
                alert('请求错误: ' + error.message);
            }}
        }}

        function closeModal() {{
            window.location.href = '/admin/keys';
        }}
    </script>
    <style>
        .keys-container {{
            max-width: 500px;
            margin: 50px auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }}
        .form-group select,
        .form-group input {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .button-group {{
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }}
        .btn-primary {{
            background: #007bff;
            color: white;
        }}
        .btn-secondary {{
            background: #6c757d;
            color: white;
        }}
    </style>
</body>
</html>
        """

        return HTMLResponse(content=form_html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"表单生成失败: {str(e)}")


@admin_app.get("/admin/keys", response_class=HTMLResponse)
async def keys_page(request: Request):
    """密钥管理页路由 - 渲染keys.html。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "keys.html"
        html_content = template_path.read_text()
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/health", response_class=HTMLResponse)
async def health_page(request: Request):
    """健康监控页面 (3.9)。"""
    template_path = (
        Path(__file__).resolve().parent.parent.parent
        / "llm_router" / "ui" / "templates" / "health.html"
    )
    return HTMLResponse(template_path.read_text(encoding="utf-8"))


@admin_app.get("/admin/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    """监控面板路由 - 渲染monitoring.html。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "monitoring.html"
        html_content = template_path.read_text()
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """设置面板路由 - 渲染settings.html。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "settings.html"
        html_content = template_path.read_text()
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    """备份管理页路由 - 渲染backup.html。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "backup.html"
        html_content = template_path.read_text()
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    """Provider管理页路由 - 渲染providers.html。"""
    try:
        template_path = Path(__file__).resolve().parent.parent.parent / "llm_router" / "ui" / "templates" / "providers.html"
        html_content = template_path.read_text()
        html_content = html_content.replace("{%", "<!-- ").replace("%}", " -->")
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板加载失败: {str(e)}")


@admin_app.get("/admin/providers/form", response_class=HTMLResponse)
async def providers_form_page(request: Request):
    """显示添加provider表单。"""
    try:
        providers = provider_manager.list_providers()

        # 生成tier下拉选项
        tier_options = """
        <option value="strong">strong (高性能)</option>
        <option value="medium">medium (标准)</option>
        <option value="fast">fast (快速响应)</option>
        """

        form_html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>添加Provider - LLM Router Admin</title>
    <script src="/static/js/htmx.min.js"></script>
    <link rel="stylesheet" href="/static/css/admin.css">
</head>
<body>
    <div class="container">
        <div class="keys-container">
            <h2>添加新Provider</h2>
            <form id="provider-form" onsubmit="handleSubmit(event)">
                <div class="form-group">
                    <label for="provider_name">Provider名称</label>
                    <input type="text" id="provider_name" name="name" required placeholder="例如: openai, anthropic, groq">
                </div>
                <div class="form-group">
                    <label for="tier">性能层级</label>
                    <select id="tier" name="tier" required>
                        <option value="">-- 请选择 --</option>
                        {tier_options}
                    </select>
                </div>
                <div class="form-group">
                    <label for="base_url">API基础URL (可选)</label>
                    <input type="url" id="base_url" name="base_url" placeholder="例如: https://api.openai.com/v1">
                </div>
                <div class="form-group">
                    <label for="quota">请求配额 (可选)</label>
                    <input type="number" id="quota" name="quota" placeholder="默认1000000" min="1">
                </div>
                <div class="form-group">
                    <label for="default_model">默认模型 (可选)</label>
                    <input type="text" id="default_model" name="default_model" placeholder="例如: gpt-4">
                </div>
                <div class="button-group">
                    <button type="submit" class="btn btn-primary">创建Provider</button>
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">取消</button>
                </div>
            </form>
        </div>
    </div>
    <script>
        async function handleSubmit(e) {{
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = {{
                name: formData.get('name'),
                tier: formData.get('tier'),
                base_url: formData.get('base_url') || null,
                quota: formData.get('quota') || null,
                default_model: formData.get('default_model') || null
            }};

            if (!data.name || !data.tier) {{
                alert('请填写必填字段 (Provider名称和性能层级)');
                return;
            }}

            try {{
                const response = await fetch('/api/admin/providers', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(data)
                }});

                const result = await response.json();

                if (response.ok) {{
                    alert('Provider创建成功！');
                    window.location.href = '/admin/providers';
                }} else {{
                    alert('创建失败: ' + (result.detail || '未知错误'));
                }}
            }} catch (error) {{
                alert('请求错误: ' + error.message);
            }}
        }}

        function closeModal() {{
            window.location.href = '/admin/providers';
        }}
    </script>
    <style>
        .keys-container {{
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }}
        .form-group input {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .form-group select {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .button-group {{
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }}
        .btn-primary {{
            background: #007bff;
            color: white;
        }}
        .btn-secondary {{
            background: #6c757d;
            color: white;
        }}
    </style>
</body>
</html>
        """

        return HTMLResponse(content=form_html)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"表单生成失败: {str(e)}")


@admin_app.get("/admin/providers/{provider}/edit", response_class=HTMLResponse)
async def provider_edit_page(provider: str, request: Request):
    """显示编辑provider表单（预填充现有数据）。"""
    try:
        # 获取provider现有配置
        existing = provider_manager.get_provider(provider)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Provider not found: {provider}")

        # 生成tier下拉选项
        tier_options = f"""
        <option value="strong" {'selected' if existing.get('tier') == 'strong' else ''}>strong (高性能)</option>
        <option value="medium" {'selected' if existing.get('tier') == 'medium' else ''}>medium (标准)</option>
        <option value="fast" {'selected' if existing.get('tier') == 'fast' else ''}>fast (快速响应)</option>
        """

        form_html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>编辑Provider - LLM Router Admin</title>
    <script src="/static/js/htmx.min.js"></script>
    <link rel="stylesheet" href="/static/css/admin.css">
</head>
<body>
    <div class="container">
        <div class="keys-container">
            <h2>编辑Provider: {provider}</h2>
            <form id="provider-edit-form" onsubmit="handleSubmit(event)">
                <div class="form-group">
                    <label for="provider_name">Provider名称</label>
                    <input type="text" id="provider_name" name="name" value="{provider}" readonly disabled style="background-color: #f5f5f5;">
                    <small style="color: #666;">Provider名称不可修改</small>
                </div>
                <div class="form-group">
                    <label for="tier">性能层级</label>
                    <select id="tier" name="tier" required>
                        <option value="">-- 请选择 --</option>
                        {tier_options}
                    </select>
                </div>
                <div class="form-group">
                    <label for="base_url">API基础URL</label>
                    <input type="url" id="base_url" name="base_url" value="{existing.get('base_url', '') or ''}" placeholder="例如: https://api.openai.com/v1">
                </div>
                <div class="form-group">
                    <label for="quota">请求配额</label>
                    <input type="number" id="quota" name="quota" value="{existing.get('quota', '') or ''}" placeholder="默认1000000" min="1">
                </div>
                <div class="form-group">
                    <label for="default_model">默认模型</label>
                    <input type="text" id="default_model" name="default_model" value="{existing.get('default_model', '') or ''}" placeholder="例如: gpt-4">
                </div>
                <div class="button-group">
                    <button type="submit" class="btn btn-primary">保存修改</button>
                    <button type="button" class="btn btn-secondary" onclick="closeModal()">取消</button>
                </div>
            </form>
        </div>
    </div>
    <script>
        async function handleSubmit(e) {{
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = {{
                tier: formData.get('tier'),
                base_url: formData.get('base_url') || null,
                quota: formData.get('quota') || null,
                default_model: formData.get('default_model') || null
            }};

            if (!data.tier) {{
                alert('请选择性能层级');
                return;
            }}

            try {{
                const response = await fetch('/api/admin/providers/{provider}/config', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(data)
                }});

                const result = await response.json();

                if (response.ok) {{
                    alert('Provider配置更新成功！');
                    window.location.href = '/admin/providers';
                }} else {{
                    alert('更新失败: ' + (result.detail || '未知错误'));
                }}
            }} catch (error) {{
                alert('请求错误: ' + error.message);
            }}
        }}

        function closeModal() {{
            window.location.href = '/admin/providers';
        }}
    </script>
    <style>
        .keys-container {{
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }}
        .form-group input {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .form-group select {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .form-group small {{
            display: block;
            margin-top: 5px;
        }}
        .button-group {{
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }}
        .btn-primary {{
            background: #007bff;
            color: white;
        }}
        .btn-secondary {{
            background: #6c757d;
            color: white;
        }}
    </style>
</body>
</html>
        """

        return HTMLResponse(content=form_html)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"表单生成失败: {str(e)}")


# ===== 健康检查 =====

@admin_app.get("/healthz")
async def healthz():
    """K8s liveness probe。"""
    return {"status": "healthy"}


@admin_app.get("/metrics")
async def metrics():
    """Prometheus /metrics 端点 (纯 stdlib, 零外部依赖).

    暴露:
      llm_router_requests_total{provider}     Counter 总请求
      llm_router_errors_total{provider}       Counter 总错误
      llm_router_active_keys                   Gauge 活跃密钥数
      llm_router_db_size_bytes{db}             Gauge 数据库文件大小
    """
    import os as _os
    from pathlib import Path as _Path

    data_dir = _Path(__file__).resolve().parents[3] / "data"
    lines = [
        "# HELP llm_router_requests_total Total requests per provider.",
        "# TYPE llm_router_requests_total counter",
        "# HELP llm_router_errors_total Total errors per provider.",
        "# TYPE llm_router_errors_total counter",
        "# HELP llm_router_active_keys Number of active API keys.",
        "# TYPE llm_router_active_keys gauge",
        "# HELP llm_router_db_size_bytes Database file sizes in bytes.",
        "# TYPE llm_router_db_size_bytes gauge",
        "",
    ]

    # DB sizes
    for db_file in ["trace.db", "health.db", "scanner.db", "ledger.db"]:
        db_path = data_dir / db_file
        size = db_path.stat().st_size if db_path.exists() else 0
        lines.append(
            f'llm_router_db_size_bytes{{db="{db_file}"}} {size}'
        )

    # Active keys count (from policy providers)
    try:
        from llm_router.config import policy as _policy
        active = len([p for p in _policy().providers if p.name != "mock"])
        lines.append(f"llm_router_active_keys {active}")
    except Exception:
        lines.append("llm_router_active_keys 0")

    # Per-provider metrics placeholder (real impl needs Cascade access)
    lines.append("")
    return "\n".join(lines) + "\n"


# ===== 备份导出导入 =====

class BackupExportRequest(BaseModel):
    include_secrets: bool = True


class BackupImportRequest(BaseModel):
    confirm: bool = False  # 防止误操作


@admin_app.post("/admin/backup/export")
async def export_backup(req: BackupExportRequest):
    """导出data/目录为tar.gz。"""
    data_dir = Path(__file__).resolve().parents[3] / "data"

    if not data_dir.exists():
        raise HTTPException(status_code=404, detail="data directory not found")

    # 生成临时tar.gz文件
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(tmp_fd)

    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(data_dir, arcname="data")

        # 计算文件大小
        file_size = os.path.getsize(tmp_path)

        # 如果不包含secret，需要掩码处理（简化版：暂不实现）
        if not req.include_secrets:
            get_audit_logger().log("admin", "BACKUP_EXPORT", {"masked": True})
        else:
            get_audit_logger().log("admin", "BACKUP_EXPORT", {"masked": False})

        # 流式返回文件
        def iterfile():
            with open(tmp_path, "rb") as f:
                while chunk := f.read(8192):
                    yield chunk
            # 发送完后删除临时文件
            os.unlink(tmp_path)

        return StreamingResponse(
            iterfile(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="backup-{datetime.now().strftime("%Y%m%d-%H%M%S")}.tar.gz"',
                "X-Backup-Size": str(file_size),
            }
        )

    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@admin_app.post("/admin/backup/import")
async def import_backup(
    backup_file: UploadFile = File(..., description="备份 tar.gz 文件 (export 端点输出格式, 前端字段名 backup_file)"),
    confirm: bool = Form(False, description="必须勾选确认 (防止误操作)"),
    _user: dict = Depends(get_current_user_with_permission("admin")),
):
    """导入备份 tar.gz 并替换 data/ 目录 (R19 实施 — 三方共识 2026-08-06,
    R20 增强: R1 max upload size, F1 store reconnect, F3 symlink, R3 audit log, R4 atomic).

    流程:
      1. RBAC: admin 权限 (D7 防御深度)
      2. 确认门: confirm=true 才执行
      3. R1: 上传大小预检 + 流式截断 (ENV LLM_ROUTER_BACKUP_MAX_UPLOAD_MB, 默认 500MB)
      4. 流式读上传 → 临时 tar.gz
      5. F3: 验证 tarfile + 路径穿越 + symlink 检查
      6. 自动备份当前 data/ → data/.backup/{ts}/
      7. F1: 关闭 app 级持久 store 连接 (防 os.replace 后数据漂移)
      8. R4 Phase 1: 全部 db 先 integrity_check, 收集 valid_dbs
      9. R3: checkpoint 当前 db 静默吞错 → audit log
      10. R4 Phase 2: 全过才批量 replace, 任一 os.replace 失败回滚 (从 backup tar)
      11. audit log BACKUP_IMPORT (filename/size/sha256/restored/integrity_errors)
      12. 不重启进程, store 下次请求自动 reconnect

    Returns:
        {"status": "ok"|"partial", "restored_files": int, "backup_id": str, "sha256": str}
    """
    import hashlib
    import shutil
    import sqlite3
    import tempfile

    # R20 R1: max upload size (DoS 防护, ENV 可覆盖, 默认 500MB — Codex 共识)
    _MAX_UPLOAD_MB = int(os.environ.get("LLM_ROUTER_BACKUP_MAX_UPLOAD_MB", "500"))
    _MAX_UPLOAD_BYTES = _MAX_UPLOAD_MB * 1024 * 1024

    if not confirm:
        raise HTTPException(status_code=403, detail="Import requires confirmation (set confirm=true)")

    # R20 R1: Content-Length 预检 (如客户端提供)
    cl = backup_file.headers.get("content-length") if hasattr(backup_file, "headers") else None
    if cl is not None:
        try:
            if int(cl) > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload too large: {int(cl)} bytes exceeds max {_MAX_UPLOAD_MB} MB",
                )
        except (ValueError, TypeError):
            pass  # 非数字 Content-Length 忽略, 走流式计数

    # 1. 流式读上传到临时 tar.gz
    data_dir = Path(__file__).resolve().parents[3] / "data"
    # R20 F2: ENV 覆盖 data_dir (测试注入用, 默认 None = 用默认路径)
    _env_data_dir = os.environ.get("LLM_ROUTER_BACKUP_DATA_DIR")
    if _env_data_dir:
        data_dir = Path(_env_data_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz", prefix="r19-import-")
    os.close(tmp_fd)

    sha256 = hashlib.sha256()
    file_size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await backup_file.read(8192):
                file_size += len(chunk)
                # R20 R1: 流式截断兜底
                if file_size > _MAX_UPLOAD_BYTES:
                    out.close()
                    os.unlink(tmp_path)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds max size ({_MAX_UPLOAD_MB} MB)",
                    )
                out.write(chunk)
                sha256.update(chunk)
        sha256_hex = sha256.hexdigest()

        # 2. F3 + R19: 验证 tarfile + 路径穿越 + symlink
        try:
            with tarfile.open(tmp_path, "r:gz") as tar:
                members = tar.getmembers()
                for m in members:
                    # F3 R20: symlink 检查 — 防 admin 用 symlink 提权读外部文件
                    if m.issym() or m.islnk():
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unsafe symlink in tar: {m.name} (refuses symlinks for safety)",
                        )
                    # R19: 路径穿越: 不能含 .. 或绝对路径
                    if ".." in m.name or m.name.startswith("/") or "\\" in m.name:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unsafe path in tar: {m.name} (contains .. or absolute)",
                        )
                # 解压到临时目录
                extract_dir = tempfile.mkdtemp(prefix="r19-extract-")
                tar.extractall(extract_dir)
        except tarfile.ReadError as e:
            raise HTTPException(status_code=400, detail=f"Invalid tar.gz: {e}")

        # 3. 自动备份当前 data/ → data/.backup/{ts}/
        backup_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = data_dir / ".backup" / backup_id
        backup_root.mkdir(parents=True, exist_ok=True)
        if data_dir.exists() and any(data_dir.iterdir()):
            backup_tar = backup_root / "data.tar.gz"
            with tarfile.open(backup_tar, "w:gz") as tar:
                tar.add(data_dir, arcname="data")
            backup_saved = str(backup_tar.relative_to(data_dir.parent))
        else:
            backup_saved = "(empty data dir, no backup)"

        # 4. R20 F1: 关闭 app 级持久 store 连接 (防 os.replace 后数据漂移)
        # admin 路由内的 _get_health_store 等是 per-request 短生命周期, 不需要处理
        # 这里处理主 app module-level TraceStore/HealthStore/LedgerStore (在 app.py _store_instances 注册)
        try:
            from llm_router.app import _store_instances
            for _store_ref in _store_instances:
                _s = _store_ref()
                if _s is not None and hasattr(_s, "reconnect"):
                    try:
                        await _s.reconnect()
                    except Exception:
                        pass  # reconnect 失败不阻断 (e.g. store 未 init)
        except ImportError:
            pass  # 非 app 上下文 (测试) 跳过

        # 5. R20 R4 Phase 1: 全部 db 先 integrity_check, 收集 valid_dbs
        # R20 R3: checkpoint 静默吞错 → audit log
        db_files = ["trace.db", "health.db", "scanner.db", "ledger.db", "circuit.db", "keys.db"]
        restored_count = 0
        integrity_errors = []
        checkpoint_failures = []  # R3: 记录 checkpoint 失败的 db
        valid_dbs: list[tuple[str, Path, Path]] = []
        for db_name in db_files:
            target_db = data_dir / db_name
            source_db = Path(extract_dir) / "data" / db_name
            if not source_db.exists():
                continue
            # R3: checkpoint 当前 db (损坏 db 不阻断恢复, 但记录到 audit log)
            if target_db.exists():
                try:
                    conn = sqlite3.connect(str(target_db))
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.close()
                except sqlite3.DatabaseError as e:
                    checkpoint_failures.append(f"{db_name}: {e}")
            # integrity_check 源 db
            try:
                chk_conn = sqlite3.connect(str(source_db))
                result = chk_conn.execute("PRAGMA integrity_check").fetchone()
                chk_conn.close()
                if result and result[0] != "ok":
                    integrity_errors.append(f"{db_name}: {result[0]}")
                    continue
                valid_dbs.append((db_name, source_db, target_db))
            except sqlite3.DatabaseError as e:
                integrity_errors.append(f"{db_name}: {e}")
                continue

        # 6. R20 R4 Phase 2: 全过才批量替换, 任一失败回滚
        replaced_in_this_run: list[Path] = []
        if not integrity_errors:
            for db_name, source_db, target_db in valid_dbs:
                try:
                    os.replace(str(source_db), str(target_db))
                    replaced_in_this_run.append(target_db)
                    # 清残留 -wal/-shm (新 db 重建)
                    for suf in ("-wal", "-shm"):
                        wal_or_shm = target_db.with_name(target_db.name + suf)
                        if wal_or_shm.exists():
                            wal_or_shm.unlink()
                    restored_count += 1
                except OSError as e:
                    # R20 R4: os.replace 失败 → 回滚已替换的 db (从 backup tar)
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.error("import_backup: os.replace failed for %s: %s — rolling back", db_name, e)
                    _restore_from_backup(replaced_in_this_run, backup_root / "data.tar.gz", data_dir)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Atomic replace failed for {db_name}: {e}. "
                               f"Rolled back {len(replaced_in_this_run)} files from backup.",
                    )

        # 7. audit log (R19 加 filename/size/sha256, R20 加 checkpoint_failures)
        get_audit_logger().log("admin", "BACKUP_IMPORT", {
            "confirmed": True,
            "filename": backup_file.filename or "<unknown>",
            "size_bytes": file_size,
            "sha256": sha256_hex,
            "restored_db_count": restored_count,
            "backup_id": backup_id,
            "integrity_errors": integrity_errors,
            "checkpoint_failures": checkpoint_failures,  # R20 R3
        })

        # 8. 清理临时目录
        shutil.rmtree(extract_dir, ignore_errors=True)
        os.unlink(tmp_path)

        # 9. 不重启进程: store 层下次请求自动 reconnect (F1)
        # admin 路由 store 是 per-request, 主 app store 已显式 reconnect 上方

        if integrity_errors:
            return {
                "status": "partial",
                "restored_files": restored_count,
                "backup_id": backup_id,
                "sha256": sha256_hex,
                "backup_saved_to": backup_saved,
                "integrity_errors": integrity_errors,
                "checkpoint_failures": checkpoint_failures,  # R20 R3
            }
        return {
            "status": "ok",
            "restored_files": restored_count,
            "backup_id": backup_id,
            "sha256": sha256_hex,
            "backup_saved_to": backup_saved,
            "checkpoint_failures": checkpoint_failures,  # R20 R3
        }

    except HTTPException:
        raise
    except Exception as e:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        get_audit_logger().log("admin", "BACKUP_IMPORT_FAILED", {
            "filename": backup_file.filename or "<unknown>",
            "error": str(e),
        })
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")


def _restore_from_backup(replaced: list[Path], backup_tar: Path, data_dir: Path) -> None:
    """R20 R4: 从备份 tar.gz 恢复已替换的 db 文件 (os.replace 失败回滚).

    只恢复 .db 文件;非 .db (e.g. -wal/-shm) 不恢复 (新 db 会重建).
    """
    import logging
    logger = logging.getLogger(__name__)
    if not backup_tar.exists():
        logger.error("_restore_from_backup: backup tar not found at %s", backup_tar)
        return
    try:
        with tarfile.open(backup_tar, "r:gz") as tar:
            for member in tar.getmembers():
                target = data_dir / Path(member.name).name
                if target in replaced and member.name.endswith(".db"):
                    try:
                        tar.extract(member, data_dir, set_attrs=False)
                        logger.info("_restore_from_backup: restored %s", target.name)
                    except Exception as e:
                        logger.error("_restore_from_backup: failed to restore %s: %s", target.name, e)
    except Exception as e:
        logger.error("_restore_from_backup: failed to open backup tar %s: %s", backup_tar, e)


@admin_app.get("/api/admin/backup/db-sizes")
async def get_db_sizes():
    """获取数据库文件大小 + 监控告警 (2.6).

    当任一 db 超过 WARN_THRESHOLD(1GB) 时,返回 warnings 段。
    当 trace_hot 表超过 100 万行时,返回 migration_needed 标志。
    """
    import sqlite3
    data_dir = Path(__file__).resolve().parents[3] / "data"
    WARN_THRESHOLD = 1_073_741_824  # 1GB
    TRACE_HOT_MIGRATE = 1_000_000    # 100万行

    db_files = [
        ("trace.db", "trace_hot"),
        ("health.db", None),
        ("scanner.db", None),
        ("ledger.db", None),
    ]

    sizes = {}
    warnings = []
    for db_file, hot_table in db_files:
        db_path = data_dir / db_file
        if db_path.exists():
            size = os.path.getsize(db_path)
            sizes[db_file] = size
            if size > WARN_THRESHOLD:
                warnings.append(f"{db_file}: {size / 1e9:.1f}GB (超过 1GB 告警阈值)")
        else:
            sizes[db_file] = 0

    # trace_hot 超 100 万行 → 迁移触发
    migration_needed = False
    try:
        trace_path = data_dir / "trace.db"
        if trace_path.exists():
            conn = sqlite3.connect(str(trace_path))
            cur = conn.execute("SELECT COUNT(*) FROM trace_hot")
            row_count = cur.fetchone()[0]
            conn.close()
            if row_count > TRACE_HOT_MIGRATE:
                migration_needed = True
                warnings.append(
                    f"trace_hot: {row_count:,} 行 (超过 {TRACE_HOT_MIGRATE:,} "
                    f"迁移阈值, 建议迁移到 trace_cold)"
                )
    except Exception:
        pass  # trace_hot 表不存在或连接失败 → 不阻塞

    return {
        "sizes": sizes,
        "warnings": warnings,
        "migration_needed": migration_needed,
    }


# ===== 监控观测接口 =====

@admin_app.get("/api/admin/metrics/circuit-breakers")
async def get_circuit_breakers():
    """获取所有provider熔断状态。"""
    # TODO: 需要Cascade实例访问熔断器状态
    # 这里返回模拟数据
    providers = [p.name for p in policy().providers]
    return {
        "circuit_breakers": [
            {
                "provider": p,
                "state": "CLOSED",
                "remaining_cooldown": 0,
                "failure_count": 0,
            }
            for p in providers
        ]
    }


@admin_app.get("/api/admin/metrics/rate-limits")
async def get_rate_limits():
    """429 限流统计 (R13 实施 — 接 trace.db 真实数据).

    返回 24h 内 rate_limited 记录数, 按 provider 分组.
    SQL: `SELECT provider, COUNT(*) FROM trace_hot WHERE result = 'rate_limited' AND created_at >= ?`
    """
    data_dir = Path(__file__).resolve().parents[3] / "data"
    trace_path = data_dir / "trace.db"
    now = datetime.now()
    cutoff = (now - timedelta(hours=24)).isoformat()

    if not trace_path.exists():
        return {"total_429": 0, "providers": {}, "last_24h": [],
                "data_source": "empty"}

    try:
        conn = sqlite3.connect(str(trace_path))
        cur = conn.execute(
            "SELECT provider, COUNT(*) FROM trace_hot WHERE result = 'rate_limited' AND created_at >= ? GROUP BY provider",
            (cutoff,),
        )
        rows = cur.fetchall()
        conn.close()

        per_provider = {}
        total = 0
        for provider, count in rows:
            per_provider[provider] = count
            total += count

        return {
            "total_429": total,
            "providers": per_provider,
            "last_24h": [],
            "data_source": "trace.db" if total > 0 else "empty",
        }
    except Exception as e:
        return {"total_429": 0, "providers": {}, "last_24h": [],
                "data_source": "error", "error": str(e)}


def _get_health_store():
    """懒初始化 HealthStore (避免 import 期副作用)."""
    from llm_router.store.health_store import HealthStore
    data_dir = Path(__file__).resolve().parents[3] / "data"
    return HealthStore(data_dir / "health.db")




# ===== 监控观测接口 — Phase5 (3.5/3.6/3.7 monitoring charts) =====

@admin_app.get("/api/admin/metrics/trends")
async def get_metrics_trends():
    """24h 请求量趋势 (3.5 monitoring.html 折线图数据源).

    返回 hourly buckets 24 个数据点 (按 trace.db trace_hot 表 ts 字段聚合).
    当前实现: 尝试从 trace.db 读最近 24h, 无数据时返回 24 个 0 占位 (前端空图).
    """
    from collections import Counter
    from datetime import datetime, timedelta
    import sqlite3

    data_dir = Path(__file__).resolve().parents[3] / "data"
    trace_path = data_dir / "trace.db"
    now = datetime.now()
    cutoff = (now - timedelta(hours=24)).isoformat()

    buckets = [{"hour": i, "count": 0} for i in range(24)]

    if not trace_path.exists():
        return {"trends": buckets, "total_24h": 0, "data_source": "empty"}

    try:
        conn = sqlite3.connect(str(trace_path))
        cur = conn.execute(
            "SELECT created_at FROM trace_hot WHERE created_at >= ?",
            (cutoff,),
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            return {"trends": buckets, "total_24h": 0, "data_source": "empty"}

        hour_counter = Counter()
        for (ts_str,) in rows:
            try:
                ts = datetime.fromisoformat(ts_str)
                hours_ago = int((now - ts).total_seconds() // 3600)
                if 0 <= hours_ago < 24:
                    hour_counter[23 - hours_ago] += 1
            except (ValueError, TypeError):
                continue

        for hour_idx, count in hour_counter.items():
            buckets[hour_idx]["count"] = count

        return {"trends": buckets, "total_24h": sum(hour_counter.values()),
                "data_source": "trace.db"}
    except Exception as e:
        return {"trends": buckets, "total_24h": 0,
                "data_source": "error", "error": str(e)}


@admin_app.get("/api/admin/metrics/errors")
async def get_metrics_errors():
    """Provider 错误率分布 (3.6 monitoring.html 柱状图数据源).

    返回每个 provider 24h 错误率 (0.0-1.0), 红色高亮 >5%.
    当前实现: 从 trace.db 读 status != 200 + 总数.
    """
    import sqlite3
    from datetime import datetime, timedelta

    data_dir = Path(__file__).resolve().parents[3] / "data"
    trace_path = data_dir / "trace.db"
    now = datetime.now()
    cutoff = (now - timedelta(hours=24)).isoformat()

    if not trace_path.exists():
        providers = [p.name for p in policy().providers]
        return {"errors": [{"provider": p, "error_rate": 0.0, "total": 0, "errors": 0}
                           for p in providers], "data_source": "empty"}

    try:
        conn = sqlite3.connect(str(trace_path))
        cur = conn.execute(
            "SELECT provider, result, COUNT(*) FROM trace_hot WHERE created_at >= ? GROUP BY provider, result",
            (cutoff,),
        )
        rows = cur.fetchall()
        conn.close()

        per_provider = {}
        for provider, result, count in rows:
            if provider not in per_provider:
                per_provider[provider] = {"total": 0, "errors": 0}
            per_provider[provider]["total"] += count
            # result 字段: 'success' / 'error' / 'rate_limited' 等字符串
            if result and result != "success":
                per_provider[provider]["errors"] += count

        errors = []
        for provider, stats in per_provider.items():
            total = stats["total"]
            err_count = stats["errors"]
            rate = err_count / total if total > 0 else 0.0
            errors.append({
                "provider": provider,
                "error_rate": round(rate, 4),
                "total": total,
                "errors": err_count,
            })

        return {"errors": errors, "data_source": "trace.db"}
    except Exception as e:
        providers = [p.name for p in policy().providers]
        return {"errors": [{"provider": p, "error_rate": 0.0, "total": 0, "errors": 0}
                           for p in providers], "data_source": "error", "error": str(e)}


@admin_app.get("/api/admin/metrics/latency")
async def get_metrics_latency():
    """响应时间热图 (3.7 monitoring.html 热力图数据源).

    返回 24h x 8 时段 = 192 数据点 (latency_ms p95).
    当前实现: 从 trace.db 读 latency_ms 按小时聚合 (8 时段 = 3h/段).
    """
    import sqlite3
    from datetime import datetime, timedelta

    data_dir = Path(__file__).resolve().parents[3] / "data"
    trace_path = data_dir / "trace.db"
    now = datetime.now()
    cutoff = (now - timedelta(hours=24)).isoformat()

    # 24 hours × 8 buckets per hour = 192 cells
    cells = []
    for h in range(24):
        for b in range(8):
            cells.append({"hour": h, "bucket": b, "p95_ms": 0})

    if not trace_path.exists():
        return {"latency": cells, "data_source": "empty"}

    try:
        conn = sqlite3.connect(str(trace_path))
        cur = conn.execute(
            "SELECT created_at, latency FROM trace_hot WHERE created_at >= ? AND latency IS NOT NULL",
            (cutoff,),
        )
        rows = cur.fetchall()
        conn.close()

        per_cell = {}
        for ts_str, lat in rows:
            try:
                ts = datetime.fromisoformat(ts_str)
                hours_ago = int((now - ts).total_seconds() // 3600)
                bucket = int(((now - ts).total_seconds() % 3600) // 450)  # 8 buckets × 450s
                if 0 <= hours_ago < 24 and 0 <= bucket < 8:
                    key = (23 - hours_ago, bucket)
                    if key not in per_cell:
                        per_cell[key] = []
                    per_cell[key].append(lat)
            except (ValueError, TypeError):
                continue

        # 计算 p95 per cell
        for key, lats in per_cell.items():
            lats_sorted = sorted(lats)
            p95_idx = int(len(lats_sorted) * 0.95)
            p95 = lats_sorted[p95_idx] if p95_idx < len(lats_sorted) else lats_sorted[-1]
            # find matching cell
            for cell in cells:
                if cell["hour"] == key[0] and cell["bucket"] == key[1]:
                    cell["p95_ms"] = int(p95)
                    break

        return {"latency": cells, "data_source": "trace.db"}
    except Exception as e:
        return {"latency": cells, "data_source": "error", "error": str(e)}


@admin_app.get("/api/admin/health/status")
async def get_health_status(current_user: dict = Depends(get_current_user_auth)):
    """获取所有provider健康状态 (2.9 — 接 HealthStore 真数据)."""
    store = _get_health_store()
    await store.init()
    try:
        rows = []
        async with store._db() as db:
            cursor = await db.execute(
                "SELECT provider, alive, last_probe_at, latency_ms FROM health ORDER BY provider"
            )
            async for row in cursor:
                rows.append({
                    "provider": row[0],
                    "alive": bool(row[1]),
                    "last_probe": row[2],
                    "latency_ms": row[3],
                })
        return {"providers": rows}
    finally:
        await store.close()


@admin_app.get("/api/admin/health/dead")
async def get_dead_providers(current_user: dict = Depends(get_current_user_auth)):
    """获取死亡 provider 列表 (2.9)."""
    store = _get_health_store()
    await store.init()
    try:
        rows = []
        async with store._db() as db:
            cursor = await db.execute(
                "SELECT provider, last_probe_at, latency_ms FROM health WHERE alive = 0 ORDER BY last_probe_at DESC"
            )
            async for row in cursor:
                rows.append({
                    "provider": row[0],
                    "last_probe": row[1],
                    "latency_ms": row[2],
                })
        return {"dead": rows, "count": len(rows)}
    finally:
        await store.close()


@admin_app.get("/api/admin/health/probe-history/{provider}")
async def get_probe_history(provider: str, current_user: dict = Depends(get_current_user_auth)):
    """获取单个 provider 24h 探活历史 (2.9)."""
    store = _get_health_store()
    await store.init()
    try:
        row = await store.get(provider)
        if row is None:
            return {"provider": provider, "found": False}
        return {
            "provider": provider,
            "alive": row.alive,
            "last_probe_at": row.last_probe_at,
            "latency_ms": row.latency_ms,
        }
    finally:
        await store.close()


@admin_app.get("/api/admin/traces/{correlation_id}")
async def get_trace(correlation_id: str):
    """按correlation_id查询trace链路。"""
    # TODO: 需要从TraceStore查询
    return {
        "correlation_id": correlation_id,
        "hops": [],
        "not_found": True,
    }


# ===== 配置管理接口 =====

class GrayPercentUpdate(BaseModel):
    percent: int


@admin_app.put("/admin/settings/gray_percent")
async def update_gray_percent(req: GrayPercentUpdate):
    """更新灰度百分比 (r9.6.2 后切片 #2: CC 约束 3 = try/except + 内存回滚 + 审计降级)."""
    if not (0 <= req.percent <= 100):
        raise HTTPException(status_code=400, detail="gray_percent must be between 0 and 100")

    current_policy = policy()
    old_percent = current_policy.gray_percent
    current_policy.gray_percent = req.percent

    # 保存配置 (D2-A stub 实装: Policy.save() → model_save() 写 override 路径)
    # 包 try/except OSError: IO 失败时三步 (CC 约束 3):
    #   (i) 内存回滚 gray_percent=old_percent (1494 已存旧值)
    #   (ii) audit_logger 写 GRANULAR_CHANGE_FAILED (result="FAILURE")
    #   (iii) raise HTTPException(500)
    try:
        current_policy.save()
    except OSError as e:
        # (i) 内存回滚 (避免内存/磁盘漂移)
        current_policy.gray_percent = old_percent
        # (ii) 审计降级 (不让 save 失败沉默)
        get_audit_logger().log(
            "admin",
            "UPDATE_GRAY_PERCENT_FAILED",
            {"old": old_percent, "new": req.percent, "error": str(e)},
            result="FAILURE",
        )
        # (iii) 返 500 (让前端显示更新失败)
        raise HTTPException(
            status_code=500,
            detail=f"failed to persist gray_percent: {e}",
        ) from e

    get_audit_logger().log("admin", "UPDATE_GRAY_PERCENT", {
        "old": old_percent,
        "new": req.percent
    })

    return {"status": "updated", "gray_percent": req.percent}


@admin_app.get("/api/admin/settings")
async def get_settings():
    """获取所有可调设置。"""
    from .settings_registry import get_registry
    registry = get_registry()
    return await registry.get_all()


@admin_app.put("/admin/settings/{setting_name}")
async def update_setting(setting_name: str, value: dict):
    """更新单个设置。"""
    from .settings_registry import get_registry
    registry = get_registry()

    try:
        new_value = value.get("value")
        await registry.update(setting_name, new_value)
        get_audit_logger().log("admin", "UPDATE_SETTING", {
            "setting": setting_name,
            "value": new_value
        })
        return {"status": "updated", "setting": setting_name, "value": new_value}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===== D7 · /admin/rollback 端点 (派 D: 路由搬 admin_subapp + Depends RBAC) =====

@admin_app.post("/admin/rollback")
async def admin_rollback(
    req: RollbackRequest,
    _user: dict = Depends(get_current_user_with_permission("admin")),
) -> dict:
    """D7:policy 回滚状态同步端点。需 admin RBAC (sub-app AuthMiddleware 已先验 Bearer)。

    双层 defense-in-depth:
      1. AuthMiddleware (admin/auth.py:24) 挡无/坏 token → 401
      2. Depends(get_current_user_with_permission("admin")) 挡权限不足 → 403

    流程(应用层编排,职责分离):
      ① 鉴权 (双层, 已上)
      ② 灰度一致 guard: body.policy_version 必须 == policy().policy_version
      ③ 重新读 manifest + policy → 构造新 candidates + entries
      ④ 同步刷新 strategy / cost_gate / enforcer + cascade.apply_policy
      ⑤ 审计日志 + 返回

    Returns:
        {"applied": bool, "policy_version": str, "candidates": list[str]}
    """
    # lazy import _cascade 单例 (避免循环: admin_subapp 实例化时 app.py 还在
    # loading, _cascade = _build_cascade() 是 module-level 副作用)
    from ..app import _cascade as _production_cascade

    # ② 灰度一致 guard (OpenCode 节点 1 [MED])
    pol = policy()
    if req.policy_version != pol.policy_version:
        raise HTTPException(
            status_code=400,
            detail=(
                f"policy_version mismatch: body={req.policy_version} "
                f"policy()={pol.policy_version} (revert policy.yaml 后再调)"
            ),
        )

    # ③+④ 重新构造候选 + 同步刷新 (单一刷新源 policy_sync.refresh_policy_state,
    #    跟 app.py._refresh_and_apply 同形 — DRY 防漂移)
    applied, candidate_names = refresh_policy_state(_production_cascade, req.policy_version)

    # ⑤ 审计日志
    get_audit_logger().log(
        _user.get("username", "admin"),
        "ADMIN_ROLLBACK",
        {"policy_version": req.policy_version, "applied": applied},
    )

    return {
        "applied": applied,
        "policy_version": req.policy_version,
        "candidates": candidate_names,
    }
