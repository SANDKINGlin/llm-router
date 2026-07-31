## 继承自:

本 change 是 [智能路由层 v3 三方共识 (2026-06-12)](~/ObsidianVault/20-记忆/共享/2026-06-12-智能路由层三方共识.md) 的延续:
- 智能路由层 (93% 完成, 27/29 任务) 已交付路由核心能力
- Admin WebUI 是下一阶段必经: 密钥管理 / 配置热重载 / 监控 Dashboard / 备份恢复
- 本 change 不修改路由行为, 纯新增管理界面

参考决策依据:
- [三方共识完整流程硬规则 (2026-07-25)](~/ObsidianVault/20-记忆/共享/research/三方辩证完整流程硬规则刻入-AGENTS.md-(2026-07-25-实测违反修复-2026-07-25.md) — OpenSpec 继承链 + 三方辩证流程
- [2026-07-28 三方真审 r6 报告](~/ObsidianVault/20-记忆/共享/research/r6-integration-report.md) — D5 OpenSpec CI 接 CI 闭环
- D5 切片 (commit 10d03ad) — scripts/openspec_validate.py 实现 4 项校验, 本段对应第 1 项 (继承自段存在 + 链接有效)

## Why

当前智能路由池已93%完成（27/29任务），核心路由功能完整，但管理界面完全缺失（0/18任务）。

运维痛点：
- 密钥管理需改.env文件+重启容器，效率低且不安全
- 系统状态不可见（熔断器状态、429统计、健康检查全在SQLite表里）
- 配置参数调整需重启，无法热重载
- 缺乏审计日志和备份恢复机制

**为什么现在做**：核心功能完成，正是补管理界面时机。命令行管理已制约日常使用效率。

## What Changes

新增Admin WebUI系统，提供三方面能力：

**管理界面**：
- 密钥管理面板（掩码显示、增删改、轮换）
- 设置面板（灰度%、熔断阈值、token预算）
- 备份管理（导出/导入配置）

**监控Dashboard**：
- 熔断状态可视化（实时状态热图）
- 429限流统计（频率图表）
- Provider健康监控（探活状态）
- trace查询（按correlation_id链路追踪）

**配置管理**：
- REST API(:8790)提供管理接口
- 热重载机制（SIGHUP+配置watch）
- 认证鉴权系统（localhost暴露+token）
- 操作审计日志

**技术选型**：
- 前端：Jinja2+HTMX（轻量，同FastAPI技术栈）
- 监控：自建Dashboard（集成在Admin UI，统一入口）
- 部署：单容器（无需独立前端服务）

## Capabilities

### New Capabilities

- `admin-auth`: Admin WebUI认证鉴权系统（localhost默认暴露、token中间件、操作审计）
- `key-management`: 密钥管理（SecretStore抽象、加密存储、CRUD接口、轮换功能）
- `admin-dashboard`: 监控Dashboard（熔断状态、429统计、健康监控、trace查询）
- `config-reload`: 配置热重载（SIGHUP信号、inotify watch、无缝切换）
- `backup-restore`: 备份恢复（data目录导出/导入、库大小监控、迁移支持）

### Modified Capabilities

无修改。本change纯新增功能，不改变现有路由行为。

## Impact

**新增代码**：
- `src/llm_router/admin/` - Admin REST API模块
- `src/llm_router/ui/` - Jinja2+HTMX前端模板
- `src/llm_router/store/secret_store.py` - 密钥存储抽象

**新增依赖**：
- `jinja2>=3.1` - 模板引擎
- `python-multipart` - 表单数据解析
- `cryptography` - 密钥加密（可选，基础版用环境变量隔离）

**API变化**：
- 新增`:8790`端口 - Admin REST API（与业务API:8789分离）
- 新增`/admin/*`路由 - 认证后的管理接口

**部署变化**：
- 单容器内运行FastAPI + Jinja2服务
- 无需额外前端容器或数据库（复用现有SQLite）
- 密钥从环境变量迁移到SecretStore（可加密）

**安全考虑**：
- 认证鉴权：localhost默认暴露，远程访问需token
- 密钥加密：SecretStore抽象支持加密后端
- 审计日志：所有管理操作记录可追溯
- 备份加密：导出时可选择是否包含敏感数据

**测试要求**（用户强调）：
- 拟人验证：模拟真实运维场景，验证界面易用性
- 集成测试：在完整智能路由池环境验证Admin UI操作生效
- 端到端测试：UI改key→路由生效、UI调灰度→生效、导出导入恢复全流程
