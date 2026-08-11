## ADDED Requirements

### Requirement: SecretStore抽象接口
系统SHALL定义SecretStore抽象接口，支持多种后端存储实现。

#### Scenario: 接口定义
- **WHEN** 系统初始化SecretStore
- **THEN** 系统提供get/set/delete接口，支持密钥读写删除

#### Scenario: 环境变量后端
- **WHEN** 使用EnvSecretStore
- **THEN** 密钥从环境变量读取，set操作更新运行时内存和环境变量

#### Scenario: 文件后端
- **WHEN** 使用FileSecretStore
- **THEN** 密钥从加密文件读取，set操作写入加密文件

### Requirement: 密钥加密存储
系统SHALL支持加密存储敏感密钥，防止明文泄露。

#### Scenario: 加密存储
- **WHEN** SecretStore配置加密模式
- **THEN** 密钥使用AES-256加密后存储，密钥从专用环境变量获取

#### Scenario: 解密读取
- **WHEN** 读取加密密钥
- **THEN** 系统自动解密返回明文给调用方

#### Scenario: 加密密钥缺失
- **WHEN** 加密模式但SECRET_ENCRYPTION_KEY环境变量缺失
- **THEN** 系统启动时记录WARNING并降级到非加密模式

### Requirement: 密钥CRUD接口
系统SHALL提供REST API接口管理provider密钥。

#### Scenario: 创建密钥
- **WHEN** POST /admin/keys，body包含provider/name/key
- **THEN** 系统验证provider存在，通过SecretStore存储密钥，返回201

#### Scenario: 读取密钥列表
- **WHEN** GET /admin/keys
- **THEN** 系统返回所有provider密钥，key字段掩码显示(仅显示前4位)

#### Scenario: 读取单个密钥
- **WHEN** GET /admin/keys/{provider}
- **THEN** 系统返回该provider密钥详情，key字段完全隐藏

#### Scenario: 更新密钥
- **WHEN** PUT /admin/keys/{provider}，body包含新key
- **THEN** 系统通过SecretStore更新密钥，返回200

#### Scenario: 删除密钥
- **WHEN** DELETE /admin/keys/{provider}
- **THEN** 系统从SecretStore删除密钥，返回204

### Requirement: 密钥轮换功能
系统SHALL提供密钥轮换接口，原子性替换旧密钥为新密钥。

#### Scenario: 成功轮换
- **WHEN** POST /admin/keys/{provider}/rotate，body包含新key
- **THEN** 系统原子性替换密钥，触发熔断器回滚，返回200

#### Scenario: 轮换回滚
- **WHEN** 密钥轮换失败或新密钥无效
- **THEN** 系统自动回滚到旧密钥，记录错误，返回503 Service Unavailable

### Requirement: 密钥验证
系统SHALL在创建或更新密钥时验证格式和有效性。

#### Scenario: 格式验证
- **WHEN** POST/PUT密钥时key格式不符合要求(非空、字符串)
- **THEN** 系统返回400 Bad Request，说明格式错误

#### Scenario: provider存在性验证
- **WHEN** POST密钥时provider名称不在候选列表
- **THEN** 系统返回404 Not Found，说明provider不存在

### Requirement: 测试环境注入
系统SHALL支持测试环境注入测试密钥，不依赖真实环境变量。

#### Scenario: 测试注入
- **WHEN** 测试用例调用SecretStore.set_test_mode()
- **THEN** 系统使用内存存储，不读取环境变量，测试结束后自动清空


## R7 实装状态 (2026-08-05 三方共识 — 7A+1A+C 标记)

按 R7 三方实测 (Hermes 翻盘版跟 Codex 6 项反驳 + 7.1 pushgateway 留 follow-up):

- tasks.md [x]: 本 capability 对应的 P0-1/P0-2/P0-3/P0-4 tasks 已标 [x] (跟 llm-router-phased R3+R5 对齐)
- 物证: test_e2e_admin.py 16 passed + test_e2e_workflows 10 passed + 2 skipped (合理 skip, 跨文件覆盖)
- 端点实测: db-sizes 端点 + 4 health 端点 + /metrics 端点全 PASS
- 模板实测: health.html 3438B 模板实装
- 三方共识: 7A (标 [x]) + 1A+C (7.1 端点标 [x] + pushgateway 留 R8+)

测试文件: tests/integration/test_e2e_admin.py (TestKeyManagementCRUD + TestBackupRestore + TestPermissionBoundary + TestConfigCRUD)
WT: wt/r7-admin-combo-mark-x-20260805
