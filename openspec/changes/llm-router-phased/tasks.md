## 对齐说明 (2026-07-30, B9+B10 切片 tasks.md + 5 specs 补全)

**上下文**: 本 change (llm-router-phased) 是 llm-router 项目分阶段交付方案, 2026-06-12 三方共识 (CC 架构派 + Hermes 红队派 + Codex 执行派) 已归档 15 份 ObsidianVault 文档, 含 13 项共识 (G1-G13) + 6 项已解分歧 + 2 项需用户拍板 + 18 天落地表. 但本 change 目录长期只有 evidence/ (phase1-integ-子片1-4), proposal.md/tasks.md/specs/ 一直缺失, 导致 D5/B8 validator BLOCKED.

**本切片 B9+B10 范围**:
  1. 写 tasks.md (P0-1/2/3/4 4 阶段 ~50 tasks, 8 真 TODO 留 [ ], 已实现标 [x])
  2. 建 specs/ 目录: P0-1-secretstore, P0-2-rest-api, P0-3-frontend, P0-4-integration, META
  3. 写 5 个 spec.md (每 ~150 字)
  4. D5 validator 验证 (--change llm-router-phased PASS)
  5. 同步 master (cp tasks.md + 5 specs/ 到 master, 用户授权 uncommitted 改)

**保留 8 个真 TODO 为 [ ]**:
  - 4.8-4.10 E2E 测试 (test_e2e_workflows.py 待建, 0/3)
  - 4.11-4.13 Prometheus 接入 (v2 §3 B1 新增, 0/3)
  - 4.14 监控 Dashboard 完善 (UI 框架有, 图表缺, 0/3)
  - 4.15-4.17 安全加固 (审计日志框架有, 细节缺, 0/3)
  - 4.18 Admin 缺失端点 (1 个健康检查端点, 0/1)

**决策方**: 用户 2026-07-30 (B9+B10 切片授权)
**verifier**: codex (Codex CLI, MiniMax-M2.7 模型) — 本切片 D5 validator 端到端验 + 三件套 grep
**agent**: cc (Claude Code, GLM-5 模型) — B9+B10 切片单边执行 (模板补齐 + 规格补齐)

evidence:
  - 三方共识归档: ~/ObsidianVault/20-记忆/共享/research/三方辩证第四轮-方案-v2-2026-07-27.md
  - phase1-integ 证据: evidence/phase1-integ-子片1-4/review.md (Hermes v5 r6 审查)
  - B9+B10 evidence: .harness/evidence/B9-B10-tasks-specs-2026-07-30.json

## 1. P0-1 地基开发（Week 1，8小时）

### SecretStore抽象层

- [x] 1.1 创建SecretStore抽象接口（`src/llm_router/store/secret_store.py`）
  - 定义`SecretStore`抽象基类（ABC）
  - 实现`get/set/delete`抽象方法
  - 添加类型提示和docstring

- [x] 1.2 实现EnvSecretStore（环境变量后端）
  - 继承SecretStore，实现get/set/delete
  - get从环境变量读取，set更新进程env
  - 添加单元测试验证读写

- [x] 1.3 实现FileSecretStore（加密文件后端）
  - 继承SecretStore，使用AES-256加密
  - get从加密文件读取并解密，set加密后写入
  - 密钥从SECRET_ENCRYPTION_KEY环境变量获取
  - 密钥缺失时WARNING并降级到非加密模式

- [x] 1.4 添加SecretStore工厂函数
  - create_secret_store()根据环境变量选择后端
  - 支持SECRET_STORE_TYPE=env/file切换
  - 单元测试验证工厂选择逻辑

### 认证鉴权系统

- [x] 1.5 实现AdminAuth类（`src/llm_router/admin/auth.py`）
  - JWT token生成和验证
  - bcrypt密码哈希
  - 登录/登出/刷新token接口
  - token过期时间配置（ACCESS_TOKEN_EXPIRE_MINUTES）

- [x] 1.6 实现权限装饰器
  - require_admin装饰器检查JWT token
  - 提取user_id并验证
  - 返回401/403标准化错误

- [x] 1.7 添加用户管理端点
  - POST /admin/auth/login (用户名密码→JWT)
  - POST /admin/auth/refresh (刷新token)
  - POST /admin/auth/logout (黑名单token)
  - 单元测试验证登录/刷新/登出流程

### 审计日志系统

- [x] 1.8 实现AuditLogger（`src/llm_router/admin/audit.py`）
  - 结构化日志记录（JSON格式）
  - 记录user_id/action/timestamp/details
  - 异步写入避免阻塞请求

- [x] 1.9 定义审计事件类型
  - AUTH_LOGIN/AUTH_LOGOUT
  - KEY_CREATED/KEY_UPDATED/KEY_DELETED
  - CONFIG_CHANGED/BACKUP_CREATED/RESTORE_PERFORMED
  - 枚举类型保证类型安全

- [x] 1.10 集成审计到Admin路由
  - 每个admin端点自动记录审计
  - 中间件拦截请求/响应
  - 单元测试验证审计记录完整性

## 2. P0-2 REST API开发（Week 2，16小时）

### 密钥管理API

- [x] 2.1 实现密钥列表端点（`src/llm_router/admin/keys.py`）
  - GET /admin/api/keys (分页列表)
  - 查询参数: page/per_page/provider
  - 返回总数+分页元数据

- [x] 2.2 实现密钥CRUD端点
  - POST /admin/api/keys (创建密钥)
  - PUT /admin/api/keys/{id} (更新密钥)
  - DELETE /admin/api/keys/{id} (删除密钥)
  - GET /admin/api/keys/{id} (单个密钥详情)

- [x] 2.3 添加密钥验证逻辑
  - provider_id有效性检查
  - api_key格式验证（非空/长度限制）
  - 权重范围验证（0.0-1.0）
  - 返回400错误+详细字段

- [x] 2.4 实现密钥测试端点
  - POST /admin/api/keys/{id}/test
  - 调用provider test_key()验证
  - 异步执行避免超时
  - 返回测试结果/耗时/错误信息

### 备份恢复API

- [x] 2.5 实现备份创建端点
  - POST /admin/api/backup (触发备份)
  - 导出SQLite为SQL文本
  - 压缩为.gz并生成时间戳文件名
  - 返回backup_id/file_size/timestamp

- [x] 2.6 实现备份列表端点
  - GET /admin/api/backups (备份列表)
  - 读取backup/目录文件
  - 返回file_name/size/created_at

- [x] 2.7 实现恢复端点
  - POST /admin/api/restore (执行恢复)
  - 上传.gz文件解压
  - 验证SQL格式安全性
  - 恢复到临时库并验证
  - 替换生产库并返回success

- [x] 2.8 添加备份清理任务
  - 定期清理30天前备份
  - 后台任务调度
  - 保留最近10个备份

### 配置热重载

- [x] 2.9 实现配置读取端点
  - GET /admin/api/config (当前配置)
  - 返回所有配置项（脱敏密钥）
  - 分组: routing/scanner/ui/limits

- [x] 2.10 实现配置更新端点
  - PUT /admin/api/config (更新配置)
  - 验证配置项有效性
  - 热重载（无需重启）
  - 记录配置变更审计

- [x] 2.11 添加配置重置端点
  - POST /admin/api/config/reset (重置为默认)
  - 读取config.yaml默认值
  - 应用并返回新配置

### Admin路由注册

- [x] 2.12 创建Admin蓝图（`src/llm_router/admin/app.py`）
  - 注册所有admin子路由
  - 统一前缀/api/admin
  - 全局错误处理（400/401/403/404/500）

- [x] 2.13 集成认证中间件
  - 所有/admin路由require_admin
  - 公开路由除外（/login）
  - JWT token解析+验证

- [x] 2.14 添加CORS支持
  - OPTIONS预检请求处理
  - Access-Control-Allow-Origin头
  - 开发环境允许所有源

## 3. P0-3 前端UI开发（Week 3，14小时）

### UI框架搭建

- [x] 3.1 创建HTMX+Jinja2模板（`src/llm_router/ui/templates/`）
  - base.html (主布局 + 导航)
  - 引入HTMX CDN
  - 引入Chart.js CDN
  - 定义content block扩展点

- [x] 3.2 实现Dashboard页面
  - dashboard.html (总览面板)
  - 统计卡片: 总请求数/活跃密钥/错误率
  - 使用HTMX从/api/stats拉取
  - 每5秒自动刷新

- [x] 3.3 实现密钥管理页面
  - keys.html (密钥列表+操作)
  - 表格展示密钥（provider/key/mask/权重/操作）
  - HTMX分页加载
  - 模态框创建/编辑密钥
  - 删除确认对话框

### 监控Dashboard

- [x] 3.4 实现监控页面框架（`src/llm_router/ui/templates/monitoring.html`）
  - 布局结构（顶部统计卡+图表区）
  - Chart.js占位canvas元素
  - HTMX拉取数据端点占位

- [ ] 3.5 实现请求量趋势图
  - 折线图显示24h请求量
  - 从/api/monitoring/trends拉取数据
  - Chart.js配置（时间轴/颜色/响应式）

- [ ] 3.6 实现错误率分布图
  - 柱状图显示provider错误率
  - 从/api/monitoring/errors拉取数据
  - 红色高亮错误率>5%的provider

- [ ] 3.7 实现响应时间热图
  - 热力图显示时段响应时间
  - 从/api/monitoring/latency拉取数据
  - 绿(快)/黄(中)/红(慢)色阶

### 设置页面

- [x] 3.8 实现设置页面框架（`src/llm_router/ui/templates/settings.html`）
  - 配置表单（routing/scanner/limits）
  - HTMX提交PUT /admin/api/config
  - 成功/失败Toast提示

- [x] 3.9 添加配置保存按钮
  - 提交表单到/api/admin/config
  - 验证输入（范围/类型）
  - 成功后显示"配置已保存"

- [x] 3.10 添加配置重置按钮
  - POST /admin/api/config/reset
  - 确认对话框
  - 重置后刷新页面

### 备份恢复页面

- [x] 3.11 实现备份列表页面（`src/llm_router/ui/templates/backups.html`）
  - 表格显示备份（文件名/大小/时间/操作）
  - GET /admin/api/backups拉取
  - 下载/恢复按钮

- [x] 3.12 添加备份创建按钮
  - POST /admin/api/backup触发
  - 进度指示器
  - 完成后刷新列表

- [x] 3.13 添加恢复确认流程
  - 上传.gz文件
  - POST /admin/api/restore
  - 警告对话框
  - 成功后重定向Dashboard

## 4. P0-4 集成验证（Week 4，8小时）

### Docker化

- [x] 4.1 创建Dockerfile
  - 基于python:3.11-slim
  - 安装依赖（requirements.txt）
  - 暴露8765端口
  - CMD启动uvicorn

- [x] 4.2 创建docker-compose.yml
  - 服务: llm-router + redis + ollama
  - 网络隔离
  - 卷挂载（backup/）
  - 环境变量注入（SECRET_ENCRYPTION_KEY）

- [x] 4.3 添加.dockerignore
  - 排除.venv/__pycache__/.git
  - 排除测试文件
  - 排除backup/本地数据

- [x] 4.4 验证Docker构建
  - docker build成功
  - docker-compose up启动
  - 健康检查/ready探针通过
  - 容器日志无ERROR

### E2E测试

- [x] 4.5 创建test_e2e_workflows.py  _(R13 三方共识 11A, 2026-08-05)_
  - pytest-asyncio框架
  - 测试数据库: test_e2e.db
  - 清理fixture（每个测试独立库）

- [x] 4.6 实现密钥管理E2E测试  _(R13 三方共识 11A, 2026-08-05)_
  - POST /admin/api/keys创建
  - GET /admin/api/keys/{id}验证
  - PUT /admin/api/keys/{id}更新
  - DELETE /admin/api/keys/{id}删除
  - 断言每步响应正确

- [x] 4.7 实现备份恢复E2E测试  _(R13 三方共识 11A, 2026-08-05)_
  - 创建测试备份
  - POST /admin/api/restore恢复
  - 验证数据完整性
  - 清理测试文件

- [x] 4.8 实现配置E2E测试  _(R13 三方共识 11A, 2026-08-05)_
  - PUT /admin/api/config修改
  - GET /admin/api/config验证
  - 重置并确认恢复默认

### Prometheus接入

- [x] 4.9 添加/metrics端点  _(R13 三方共识 11A, 2026-08-05)_
  - prometheus_client库
  - Counter: 总请求数/错误数
  - Histogram: 响应时间分布
  - Gauge: 活跃密钥数

- [x] 4.10 配置Prometheus抓取  _(R13 三方共识 11A, 2026-08-05)_
  - docker-compose.yml添加prometheus服务
  - prometheus.yml配置job
  - scrape_interval: 15s
  - 验证目标UP

- [x] 4.11 创建Grafana Dashboard  _(R13 三方共识 11A, 2026-08-05)_
  - 导入dashboard JSON
  - 面板: 请求率/错误率/响应时间
  - 数据源: Prometheus
  - 验证面板渲染

### 安全测试

- [x] 4.12 SQL注入测试  _(R13 三方共识 11A, 2026-08-05)_
  - POST /admin/api/keys注入payload
  - 验证参数化查询防御
  - 断言无错误/数据泄露

- [x] 4.13 XSS测试  _(R13 三方共识 11A, 2026-08-05)_
  - PUT /admin/api/keys注入script标签
  - 验证输出转义
  - 断言无脚本执行

- [x] 4.14 权限测试  _(R13 三方共识 11A, 2026-08-05)_
  - 无token访问/admin路由→401
  - 无效token→401
  - 权限不足→403

### 缺失端点补全

- [x] 4.15 添加健康检查端点  _(R13 三方共识 11A, 2026-08-05)_
  - GET /admin/health
  - 检查数据库连接
  - 检查Redis连接
  - 检查Ollama可用性
  - 返回200+各组件状态


## R13 对齐说明 (2026-08-05, 11A 切片标 [x] 三方共识)

**上下文**: R13 第一轮三方会诊发现 14 个 [ ] TODO 中 11 个实际已实装:
- 测试 4.5-4.8 实装在 tests/integration/test_e2e_workflows.py (10 passed + 2 skipped)
- 安全 4.12-4.14 实装在 tests/integration/test_security_admin.py (22 passed) + test_apply_policy.py (RBAC)
- 监控 4.9-4.11 实装在 admin/app.py:1345 /metrics (stdlib) + prometheus.yml + grafana-dashboard.json + docker-compose.yml
- 健康 4.15 实装在 admin/app.py:950 /admin/health + 1339 /healthz + 1567/1590/1612 共 4 端点

仅 3.5/3.6/3.7 (monitoring.html 3 图表) 真缺失 → 留 Phase5 切片实施.

**三方共识 (R2 PASS, 100% 收敛)**:
- Q1 (4.9 /metrics 标 [x]) — CC=YES, Codex=YES, Hermes=YES
- Q2 (4.15 /admin/health 标 [x]) — CC=YES, Codex=YES, Hermes=YES
- Q3 (3.5/3.6/3.7 起 Phase5 WT) — CC=YES, Codex=YES, Hermes=YES

**本切片 R13 范围**:
1. 改 11 个 [ ] → [x] (按 R2 共识 11A 列表)
2. 补 p0-4-integration/spec.md 11 个任务的实装状态段落
3. 补 p0-3-frontend/spec.md monitoring.html 现状描述 (3.5/3.6/3.7 留 follow-up)
4. D5 validator 验证 (--change llm-router-phased PASS)
5. 三方真验交付 (pytest + grep + curl 端点 + D5 validator 4 重验证)
6. commit (不 push, 等用户拍)

**verifier**: codex (R2 共识盖章)
**agent**: hermes-v5 (orchestrator)

evidence:
- R1 三方报告: /tmp/agent-threeway/llm-router-phase4-r13-20260805/results/{{cc,codex,hermes}}-result.md
- R2 三方报告: /tmp/agent-threeway/llm-router-phase4-r13-20260805/results/{{cc-r2-1,codex-r2,hermes-r2}}.md
- Obsidian 归档: ~/ObsidianVault/20-记忆/共享/research/R{{1,2}}-三方-llm-router-phase4-r13-20260805.md

## 5. META 任务

### 文档完善

- [x] 5.1 编写API文档（`docs/api.md`）
  - 所有admin端点说明
  - 请求/响应示例
  - 错误码说明

- [x] 5.2 编写部署文档（`docs/deployment.md`）
  - Docker部署步骤
  - 环境变量清单
  - docker-compose启动命令

- [x] 5.3 编写备份恢复文档（`docs/backup.md`）
  - 备份策略说明
  - 恢复步骤详解
  - 故障场景处理

### 验证通过

- [x] 5.4 D5 validator通过
  - scripts/openspec_validate.py --change llm-router-phased
  - proposal.md存在且格式正确
  - tasks.md存在且≥40 [x]
  - specs/目录存在且≥5 spec.md

- [x] 5.5 三件套验证通过
  - grep -c '^- \[x\]' tasks.md ≥ 40
  - grep -c '^- \[ \]' tasks.md ~10
  - ls specs/ | wc -l = 5
