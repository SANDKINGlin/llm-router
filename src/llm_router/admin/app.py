"""Admin REST API (was :8790 standalone, v2 D2-C mount 进 :8789 /admin/* via SharedASGIMiddleware)。密钥管理、监控、配置热重载。"""
from __future__ import annotations

import os
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, status, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .auth import AuthMiddleware, generate_token, get_audit_logger
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


# 创建SecretStore实例（默认环境变量后端）
secret_store: SecretStore = create_secret_store(backend="env")

# 创建Admin FastAPI应用
admin_app = FastAPI(title="LLM Router Admin API", version="0.1.0")

# 添加认证中间件
admin_app.add_middleware(AuthMiddleware)

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
    """登录接口，生成Bearer Token。"""
    # 简化版：用户名密码验证（生产环境用真实数据库）
    if req.username == "admin" and req.password == "admin":
        token = generate_token()
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
    """健康检查端点（无认证）。"""
    return {"status": "healthy"}


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
async def import_backup(req: BackupImportRequest):
    """导入备份tar.gz文件。"""
    # 简化版：需要上传文件（实际需要FastAPI UploadFile）
    # 这里仅展示接口结构
    if not req.confirm:
        raise HTTPException(status_code=403, detail="Import requires confirmation (set confirm=true)")

    get_audit_logger().log("admin", "BACKUP_IMPORT", {"confirmed": True})

    # TODO: 实际实现需要：
    # 1. 接收上传文件
    # 2. 验证tar.gz格式
    # 3. 自动备份当前状态到data/.backup/
    # 4. 解压并替换data/目录
    # 5. 重启服务

    return {"status": "not_implemented", "message": "需要文件上传支持"}


@admin_app.get("/api/admin/backup/db-sizes")
async def get_db_sizes():
    """获取数据库文件大小。"""
    data_dir = Path(__file__).resolve().parents[3] / "data"

    sizes = {}
    for db_file in ["trace.db", "health.db", "scanner.db", "ledger.db"]:
        db_path = data_dir / db_file
        if db_path.exists():
            sizes[db_file] = os.path.getsize(db_path)
        else:
            sizes[db_file] = 0

    return sizes


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
    """获取429限流统计。"""
    # TODO: 需要从trace.db查询429记录
    # 这里返回模拟数据
    return {
        "total_429": 0,
        "providers": {},
        "last_24h": [],
    }


@admin_app.get("/api/admin/health/status")
async def get_health_status():
    """获取所有provider健康状态。"""
    # TODO: 需要从HealthStore查询
    providers = [p.name for p in policy().providers]
    return {
        "providers": [
            {
                "provider": p,
                "alive": True,
                "last_probe": None,
                "consecutive_failures": 0,
            }
            for p in providers
        ]
    }


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
