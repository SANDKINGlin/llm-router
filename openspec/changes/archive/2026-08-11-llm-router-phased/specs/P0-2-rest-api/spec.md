# P0-2 REST API 规格

## 概述
P0-2 阶段实现完整的 REST API，包括密钥管理、备份恢复、配置热重载和 Admin 路由注册。本阶段预计 16 小时完成。

## 密钥管理 API

### CRUD 端点
- `GET /admin/api/keys`：分页列表（query: page/per_page/provider）
- `POST /admin/api/keys`：创建密钥（body: provider_id/api_key/weight）
- `GET /admin/api/keys/{id}`：单个密钥详情
- `PUT /admin/api/keys/{id}`：更新密钥（同创建字段）
- `DELETE /admin/api/keys/{id}`：删除密钥

### 验证逻辑
- provider_id 必须存在于已注册 providers
- api_key 非空且长度 ≤ 2048 字符
- weight 范围 0.0-1.0
- 失败返回 400 错误 + 详细字段错误信息

### 测试端点
- `POST /admin/api/keys/{id}/test`：调用 provider.test_key() 验证
- 异步执行避免超时（后台任务）
- 返回测试结果/耗时/错误信息

## 备份恢复 API

### 备份管理
- `POST /admin/api/backup`：触发备份，导出 SQLite 为 .gz
- 返回 backup_id/file_size/timestamp
- `GET /admin/api/backups`：备份列表（文件名/大小/时间）
- 后台任务定期清理 30 天前备份，保留最近 10 个

### 恢复流程
- `POST /admin/api/restore`：上传 .gz 文件执行恢复
- 验证 SQL 格式安全性（防注入）
- 恢复到临时库并验证完整性
- 替换生产库并返回 success

## 配置热重载

### 配置读取
- `GET /admin/api/config`：返回当前配置（脱敏密钥）
- 分组：routing/scanner/ui/limits
- 只读操作，无需认证

### 配置更新
- `PUT /admin/api/config`：更新配置项（无需重启）
- 验证配置项有效性（范围/类型）
- 记录配置变更审计日志
- 返回更新后配置

### 配置重置
- `POST /admin/api/config/reset`：重置为 config.yaml 默认值
- 确认对话框防止误操作
- 重置后刷新页面

## Admin 路由注册

### 蓝图结构
统一前缀 `/api/admin`，子路由：
- `/auth/*`：认证相关
- `/keys/*`：密钥管理
- `/backups/*`：备份恢复
- `/config/*`：配置管理

### 中间件
- 所有 `/admin` 路由强制 `require_admin`（除 `/login`）
- JWT token 解析+验证
- 全局错误处理（400/401/403/404/500）

### CORS 支持
- OPTIONS 预检请求处理
- `Access-Control-Allow-Origin` 头
- 开发环境允许所有源（生产环境需限制）

## 交付物
- `src/llm_router/admin/keys.py`：密钥管理 API
- `src/llm_router/admin/app.py`：Admin 蓝图注册
- 单元测试覆盖率 ≥ 80%
- API 文档（Swagger/OpenAPI 可选）

---

## ADDED Requirements

### Requirement: CRUD 端点 (P0-2-rest-api 阶段交付)
- `GET /admin/api/keys`：分页列表（query: page/per_page/provider）
- `POST /admin/api/keys`：创建密钥（body: provider_id/api_key/weight）
- `GET /admin/api/keys/{id}`：单个密钥详情
- `PUT /admin/api/keys/{id}`：更新密钥（同创建字段）
- `DELETE /admin/api/keys/{id}`：删除密钥

#### Scenario: CRUD 端点 正常路径
- **WHEN CRUD 端点 按 spec 实施**
- **THEN 系统按 CRUD 端点 设计实现, 单元 + 集成测试覆盖**

#### Scenario: CRUD 端点 异常路径
- **WHEN CRUD 端点 实施失败或配置缺失**
- **THEN 系统记录 ERROR 日志并降级到安全默认**

### Requirement: P0-2-rest-api 实施完整性
P0-2 阶段实现完整的 REST API，包括密钥管理、备份恢复、配置热重载和 Admin 路由注册。本阶段预计 16 小时完成。

#### Scenario: P0-2-rest-api 任务全完成
- **WHEN P0-2-rest-api 阶段实施**
- **THEN 全部子任务完成 (R7 8 mark x 实证), 集成测试覆盖 5+ 端点**

---

## ADDED Requirements

### Requirement: CRUD 端点 (P0-2-rest-api 阶段交付)
系统SHALL实现该能力. - `GET /admin/api/keys`：分页列表（query: page/per_page/provider）
- `POST /admin/api/keys`：创建密钥（body: provider_id/api_key/weight）
- `GET /admin/api/keys/{id}`：单个密钥详情
- `PUT /admin/api/keys/{id}`：更新密钥（同创建字段）
- `DELETE /admin/api/keys/{id}`：删除密钥

#### Scenario: CRUD 端点 正常路径
- **WHEN CRUD 端点 按 spec 实施并接收合规输入**
- **THEN 系统SHALL按 CRUD 端点 设计返回预期结果, 单元测试覆盖**

#### Scenario: CRUD 端点 异常路径
- **WHEN CRUD 端点 接收非法输入或依赖缺失**
- **THEN 系统SHALL记录 ERROR 日志并降级到安全默认行为, 不崩溃**

### Requirement: P0-2-rest-api 实施完整性
系统SHALL实现该能力. P0-2 阶段实现完整的 REST API，包括密钥管理、备份恢复、配置热重载和 Admin 路由注册。本阶段预计 16 小时完成。

#### Scenario: P0-2-rest-api 任务全完成
- **WHEN P0-2-rest-api 阶段实施**
- **THEN 系统SHALL满足 R7 标完 119 task, 集成测试覆盖 5+ 端点, 0 pre-existing fail**

#### Scenario: P0-2-rest-api OpenSpec validate PASS
- **WHEN 跑 openspec validate P0-2-rest-api**
- **THEN 系统SHALL返回 PASS, 5 spec 都有 ## ADDED Requirements + #### Scenario: 头**
