# P0-1 SecretStore 抽象层规格

## 概述
P0-1 阶段实现 SecretStore 抽象层和认证鉴权系统，为整个 admin 系统提供安全基础设施。本阶段预计 8 小时完成。

## SecretStore 抽象层

### 核心接口
SecretStore 抽象基类定义三个核心方法：
- `get(key: str) -> Optional[str]`：检索密钥值
- `set(key: str, value: str) -> None`：存储密钥值
- `delete(key: str) -> None`：删除密钥

### 后端实现
两个后端实现：
1. **EnvSecretStore**：从环境变量读写，适用于开发环境
2. **FileSecretStore**：AES-256 加密文件存储，适用于生产环境

### 工厂函数
`create_secret_store()` 根据 `SECRET_STORE_TYPE` 环境变量选择后端，默认为 env。

## 认证鉴权系统

### JWT Token 机制
- 登录成功返回 access_token（默认 30 分钟过期）
- 使用 bcrypt 哈希密码（salt rounds=12）
- 支持刷新 token（`/admin/auth/refresh`）

### 权限控制
- `require_admin` 装饰器保护所有 admin 端点
- 从 HTTP Authorization 头提取 token
- 验证失败返回 401 Unauthorized

### 端点
- `POST /admin/auth/login`：用户名密码 → JWT token
- `POST /admin/auth/refresh`：刷新 token
- `POST /admin/auth/logout`：登出（可选黑名单）

## 审计日志系统

### 事件类型
结构化记录以下事件：
- AUTH_LOGIN/AUTH_LOGOUT
- KEY_CREATED/KEY_UPDATED/KEY_DELETED
- CONFIG_CHANGED/BACKUP_CREATED/RESTORE_PERFORMED

### 日志格式
JSON 格式日志，包含：
- user_id：操作用户 ID
- action：事件类型
- timestamp：ISO 8601 时间戳
- details：额外上下文（字典）

### 集成方式
中间件自动记录所有 admin 端点的审计事件，异步写入避免阻塞请求。

## 交付物
- `src/llm_router/store/secret_store.py`：SecretStore 抽象层
- `src/llm_router/admin/auth.py`：认证鉴权系统
- `src/llm_router/admin/audit.py`：审计日志系统
- 单元测试覆盖率 ≥ 80%
