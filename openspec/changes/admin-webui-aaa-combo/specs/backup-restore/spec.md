## ADDED Requirements

### Requirement: 配置数据导出
系统SHALL支持导出完整配置数据目录，包含所有SQLite数据库和配置文件。

#### Scenario: 完整导出
- **WHEN** POST /admin/backup/export，include_secrets=true
- **THEN** 系统打包data/目录所有文件(含secret)，返回tar.gz流，Content-Type: application/gzip

#### Scenario: 脱敏导出
- **WHEN** POST /admin/backup/export，include_secrets=false
- **THEN** 系统打包data/目录但掩码敏感字段(api_key显示前4位*)，返回tar.gz流

#### Scenario: 导出文件大小检查
- **WHEN** 导出操作完成
- **THEN** 系统在响应头X-Backup-Size返回文件大小(字节)，供前端显示进度

### Requirement: 配置数据导入
系统SHALL支持导入之前导出的配置备份，恢复系统状态。

#### Scenario: 成功导入
- **WHEN** POST /admin/backup/import，上传tar.gz文件
- **THEN** 系统验证格式、解压到临时目录、停止服务、替换data/目录、重启服务，返回200

#### Scenario: 导入验证失败
- **WHEN** POST /admin/backup/import，上传文件格式错误或损坏
- **THEN** 系统拒绝导入，保持当前状态，返回400 Bad Request说明原因

#### Scenario: 导入确认机制
- **WHEN** POST /admin/backup/import，请求头X-Confirm-Import为true
- **THEN** 系统执行导入；否则返回403 Forbidden要求确认

#### Scenario: 导入前自动备份
- **WHEN** 系统执行导入操作
- **THEN** 系统先自动备份当前data/目录到data/.backup/{timestamp}/，失败时可回滚

### Requirement: 增量备份支持
系统SHALL支持增量备份，仅导出自上次备份以来的变化数据。

#### Scenario: 增量备份
- **WHEN** POST /admin/backup/export，incremental=true，since={timestamp}
- **THEN** 系统仅导出自since时间以来变化的SQLite记录和配置文件

#### Scenario: 增量备份合并
- **WHEN** 系统需要恢复增量备份
- **THEN** 系统要求先恢复完整备份，再按时间顺序应用所有增量备份

### Requirement: 备份文件管理
系统SHALL提供备份文件列表、删除、下载管理能力。

#### Scenario: 备份列表查询
- **WHEN** GET /admin/backup/list
- **THEN** 系统返回data/backups/目录下所有备份文件：文件名、大小、创建时间、是否包含secret

#### Scenario: 备份文件下载
- **WHEN** GET /admin/backup/download/{filename}
- **THEN** 系统返回指定备份文件，Content-Type: application/gzip

#### Scenario: 备份文件删除
- **WHEN** DELETE /admin/backup/delete/{filename}
- **THEN** 系统删除指定备份文件，返回204 No Content

#### Scenario: 自动清理旧备份
- **WHEN** 备份文件超过30天且数量超过10个
- **THEN** 系统自动删除最旧的备份，保持最多10个备份

### Requirement: 数据库大小监控
系统SHALL提供数据库文件大小监控，防止磁盘空间耗尽。

#### Scenario: 数据库大小查询
- **WHEN** GET /admin/backup/db-sizes
- **THEN** 系统返回所有SQLite文件大小：trace_cold、trace_hot、health、scanner、ledger

#### Scenario: 大小告警阈值
- **WHEN** 任意数据库文件超过1GB
- **THEN** 系统记录WARNING日志，发送告警(如果配置了告警渠道)

#### Scenario: 自动清理机制
- **WHEN** trace_hot表超过100万行
- **THEN** 系统自动触发数据迁移，将30天前数据移入trace_cold表

### Requirement: 迁移支持
系统SHALL支持跨环境迁移配置数据(开发→测试→生产)。

#### Scenario: 导出时环境标记
- **WHEN** POST /admin/backup/export，请求头X-Environment=production
- **THEN** 系统在导出文件中添加metadata.json记录环境标识和导出时间

#### Scenario: 导入时环境检查
- **WHEN** POST /admin/backup/import，上传文件metadata.json显示environment=staging
- **THEN** 系统检查当前环境是否允许导入staging数据(配置白名单)

#### Scenario: 跨环境导入拒绝
- **WHEN** 当前环境为production但导入文件来自development
- **THEN** 系统拒绝导入，返回403 Forbidden说明环境不匹配

### Requirement: 备份加密(可选)
系统SHALL支持备份文件加密，保护敏感数据。

#### Scenario: 加密备份
- **WHEN** POST /admin/backup/export，encrypt=true
- **THEN** 系统使用AES-256加密tar.gz文件，返回加密流，需要密码解密

#### Scenario: 加密备份导入
- **WHEN** POST /admin/backup/import，上传加密文件，提供密码
- **THEN** 系统解密文件后执行导入流程

#### Scenario: 加密密钥管理
- **WHEN** 加密备份时BACKUP_ENCRYPTION_KEY环境变量缺失
- **THEN** 系统返回500 Internal Server Error，说明需要配置加密密钥

### Requirement: 备份完整性验证
系统SHALL在导入前验证备份文件完整性。

#### Scenario: 校验和验证
- **WHEN** 导入备份文件
- **THEN** 系统验证文件SHA256校验和与导出时记录一致，不一致则拒绝导入

#### Scenario: 数据库完整性检查
- **WHEN** 导入包含SQLite的备份
- **THEN** 系统运行PRAGMA integrity_check验证数据库未损坏

#### Scenario: 结构兼容性检查
- **WHEN** 导入备份到新版本系统
- **THEN** 系统检查表结构兼容性，自动迁移或拒绝导入说明需要手动升级


## R7 实装状态 (2026-08-05 三方共识 — 7A+1A+C 标记)

按 R7 三方实测 (Hermes 翻盘版跟 Codex 6 项反驳 + 7.1 pushgateway 留 follow-up):

- tasks.md [x]: 本 capability 对应的 P0-1/P0-2/P0-3/P0-4 tasks 已标 [x] (跟 llm-router-phased R3+R5 对齐)
- 物证: test_e2e_admin.py 16 passed + test_e2e_workflows 10 passed + 2 skipped (合理 skip, 跨文件覆盖)
- 端点实测: db-sizes 端点 + 4 health 端点 + /metrics 端点全 PASS
- 模板实测: health.html 3438B 模板实装
- 三方共识: 7A (标 [x]) + 1A+C (7.1 端点标 [x] + pushgateway 留 R8+)

测试文件: tests/integration/test_e2e_admin.py (TestKeyManagementCRUD + TestBackupRestore + TestPermissionBoundary + TestConfigCRUD)
WT: wt/r7-admin-combo-mark-x-20260805
