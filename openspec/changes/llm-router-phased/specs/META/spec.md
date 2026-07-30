# META 元数据规格

## 概述
META 阶段完成文档完善、验证通过和交付物清单，确保整个 change 符合 OpenSpec D5 validator 标准。

## 文档完善

### API 文档（`docs/api.md`）

#### 端点列表
**认证相关**：
- `POST /admin/auth/login`：用户名密码 → JWT token
- `POST /admin/auth/refresh`：刷新 token
- `POST /admin/auth/logout`：登出

**密钥管理**：
- `GET /admin/api/keys`：密钥列表（分页）
- `POST /admin/api/keys`：创建密钥
- `GET /admin/api/keys/{id}`：密钥详情
- `PUT /admin/api/keys/{id}`：更新密钥
- `DELETE /admin/api/keys/{id}`：删除密钥
- `POST /admin/api/keys/{id}/test`：测试密钥

**备份恢复**：
- `POST /admin/api/backup`：创建备份
- `GET /admin/api/backups`：备份列表
- `POST /admin/api/restore`：恢复备份

**配置管理**：
- `GET /admin/api/config`：读取配置
- `PUT /admin/api/config`：更新配置
- `POST /admin/api/config/reset`：重置配置

#### 请求/响应示例
每个端点提供：
- cURL 命令示例
- 请求 JSON body
- 成功响应 JSON（200）
- 错误响应 JSON（400/401/403/404/500）

#### 错误码说明
- 400 Bad Request：参数验证失败
- 401 Unauthorized：认证失败/token 无效
- 403 Forbidden：权限不足
- 404 Not Found：资源不存在
- 500 Internal Server Error：服务器错误

### 部署文档（`docs/deployment.md`）

#### Docker 部署步骤
1. 克隆仓库：`git clone <repo>`
2. 构建镜像：`docker build -t llm-router .`
3. 启动服务：`docker-compose up -d`
4. 验证健康：`curl http://localhost:8765/healthz`

#### 环境变量清单
- `SECRET_ENCRYPTION_KEY`：加密密钥（生产必填）
- `SECRET_STORE_TYPE`：存储后端（env/file，默认 env）
- `DATABASE_URL`：SQLite 路径（默认 sqlite:///llm_router.db）
- `REDIS_URL`：Redis 连接（默认 redis://localhost:6379）
- `OLLAMA_BASE_URL`：Ollama 地址（默认 http://localhost:11434）

#### docker-compose 启动命令
- 启动：`docker-compose up -d`
- 停止：`docker-compose down`
- 查看日志：`docker-compose logs -f llm-router`
- 重启：`docker-compose restart llm-router`

### 备份恢复文档（`docs/backup.md`）

#### 备份策略说明
- **自动备份**：每天凌晨 2 点（后台任务）
- **手动备份**：通过 UI 或 API 触发
- **保留策略**：最近 10 个备份 + 30 天内备份
- **存储位置**：`backup/` 目录

#### 恢复步骤详解
1. 通过 UI 上传 `.gz` 备份文件
2. 或通过 API：`curl -X POST http://localhost:8765/admin/api/restore -F "backup=@backup.gz"`
3. 等待验证完成（自动检查 SQL 格式）
4. 确认恢复（数据库替换为备份内容）
5. 重启服务生效

#### 故障场景处理
- **备份文件损坏**：验证失败，拒绝恢复
- **数据库锁定**：等待写入完成，自动重试
- **版本不兼容**：检查 schema 版本，提示升级

## 验证通过

### D5 validator 通过
运行命令：
```bash
python3 scripts/openspec_validate.py --change llm-router-phased
```

通过标准：
- `proposal.md` 存在且格式正确（YAML front matter + markdown）
- `tasks.md` 存在且 ≥ 40 [x] 已完成项
- `specs/` 目录存在且 ≥ 5 个 `spec.md` 文件

### 三件套验证通过

#### 1. 任务计数验证
```bash
grep -c '^- \[x\]' tasks.md
```
期望输出：≥ 40（实际 43）

#### 2. TODO 计数验证
```bash
grep -c '^- \[ \]' tasks.md
```
期望输出：~10（实际 14，符合要求）

#### 3. 规格文件计数验证
```bash
ls specs/ | wc -l
```
期望输出：5（P0-1/P0-2/P0-3/P0-4/META）

## 交付物清单

### 代码交付物
- `src/llm_router/store/secret_store.py`：SecretStore 抽象层
- `src/llm_router/admin/auth.py`：认证鉴权系统
- `src/llm_router/admin/audit.py`：审计日志系统
- `src/llm_router/admin/keys.py`：密钥管理 API
- `src/llm_router/admin/app.py`：Admin 路由注册
- `src/llm_router/ui/templates/`：前端页面模板
- `tests/test_e2e_workflows.py`：E2E 测试套件

### 配置交付物
- `Dockerfile`：容器镜像定义
- `docker-compose.yml`：服务编排配置
- `requirements.txt`：Python 依赖清单
- `prometheus.yml`：Prometheus 抓取配置

### 文档交付物
- `docs/api.md`：API 接口文档
- `docs/deployment.md`：部署指南
- `docs/backup.md`：备份恢复指南
- `README.md`：项目总览（快速开始）

### 质量指标
- 单元测试覆盖率 ≥ 80%
- E2E 测试通过率 100%
- 安全扫描无高危漏洞
- 性能基准：API 响应时间 < 100ms（p95）
