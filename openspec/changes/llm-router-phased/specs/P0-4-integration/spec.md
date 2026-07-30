# P0-4 集成验证规格

## 概述
P0-4 阶段完成 Docker 化、端到端测试、Prometheus 接入、安全测试和缺失端点补全，确保整个系统可部署、可监控、安全。本阶段预计 8 小时完成。

## Docker 化

### Dockerfile
基于 `python:3.11-slim`：
- 安装系统依赖（gcc/redis-tools）
- 复制 `requirements.txt` 并 pip 安装
- 复制源代码
- 暴露 8765 端口
- CMD 启动 uvicorn（`src.llm_router.app:app`）

### docker-compose.yml
三个服务：
1. **llm-router**：主服务
   - 环境变量注入（SECRET_ENCRYPTION_KEY/SECRET_STORE_TYPE）
   - 卷挂载（`./backup:/app/backup`）
   - 依赖 redis/ollama

2. **redis**：缓存/任务队列
   - image: redis:7-alpine
   - 持久化卷

3. **ollama**：本地 LLM（可选）
   - image: ollama/ollama
   - GPU 支持（可选）

### 验证步骤
- `docker build -t llm-router .`：构建镜像
- `docker-compose up -d`：启动服务
- `docker-compose logs llm-router`：检查日志（无 ERROR）
- `curl http://localhost:8765/healthz`：健康检查

## 端到端测试

### 测试框架
- **pytest-asyncio**：异步测试支持
- **test_e2e.db**：独立测试数据库
- **清理 fixture**：每个测试独立库

### 测试用例

#### 密钥管理 E2E
1. POST `/admin/api/keys` 创建密钥
2. GET `/admin/api/keys/{id}` 验证创建成功
3. PUT `/admin/api/keys/{id}` 更新权重
4. GET `/admin/api/keys/{id}` 验证更新成功
5. DELETE `/admin/api/keys/{id}` 删除密钥
6. GET `/admin/api/keys` 确认删除成功

#### 备份恢复 E2E
1. 创建测试数据（密钥/配置）
2. POST `/admin/api/backup` 创建备份
3. GET `/admin/api/backups` 确认备份存在
4. POST `/admin/api/restore` 上传备份恢复
5. 验证数据完整性（密钥/配置一致）
6. 清理测试文件

#### 配置 E2E
1. PUT `/admin/api/config` 修改配置
2. GET `/admin/api/config` 验证修改成功
3. POST `/admin/api/config/reset` 重置配置
4. GET `/admin/api/config` 确认恢复默认

## Prometheus 接入（待完善）

### /metrics 端点
使用 `prometheus_client` 库：
- **Counter**：总请求数/错误数（label: provider/status）
- **Histogram**：响应时间分布（buckets: 10ms/50ms/100ms/500ms/1s/5s）
- **Gauge**：活跃密钥数/队列长度

### Prometheus 配置
`prometheus.yml` 抓取配置：
```yaml
scrape_configs:
  - job_name: 'llm-router'
    static_configs:
      - targets: ['llm-router:8765']
    scrape_interval: 15s
```

### Grafana Dashboard（待完善）
导入 dashboard JSON：
- 面板 1：请求率（requests/sec）
- 面板 2：错误率（errors/total）
- 面板 3：响应时间（p50/p95/p99）
- 数据源：Prometheus

## 安全测试（待完善）

### SQL 注入测试
- POST `/admin/api/keys` 注入 payload（`'; DROP TABLE keys; --`）
- 验证参数化查询防御（无错误/数据泄露）
- 断言响应 400 错误（验证失败）

### XSS 测试
- PUT `/admin/api/keys` 注入 script 标签（`api_key=<script>alert(1)</script>`）
- GET `/admin/api/keys` 验证输出转义（无脚本执行）
- 断言响应 HTML 转义（`&lt;script&gt;`）

### 权限测试
- 无 token 访问 `/admin` 路由 → 401 Unauthorized
- 无效 token（签名错误）→ 401 Unauthorized
- 权限不足（非 admin）→ 403 Forbidden

## 缺失端点补全（待完善）

### 健康检查端点
`GET /admin/health` 检查各组件：
- 数据库连接（`ping` SQLite）
- Redis 连接（`ping` Redis）
- Ollama 可用性（`GET /api/tags`）
- 返回 JSON：`{"status": "ok", "checks": {"db": "ok", "redis": "ok", "ollama": "ok"}}`

## 交付物
- `Dockerfile` + `docker-compose.yml`
- `tests/test_e2e_workflows.py`：E2E 测试套件
- `prometheus.yml` + `grafana/`：监控配置
- 测试覆盖率 ≥ 80%
- 安全扫描无高危漏洞（Bandit/Safety）
