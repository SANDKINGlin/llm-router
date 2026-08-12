# admin-auth Specification

## Purpose
JWT 认证 + 角色鉴权 (admin WebUI + REST API 共享). admin/auth_enhanced.py + admin/auth.py 双实现, Token 24h 过期, is_active 软删除 (R18).

**R 链路映射**: R18 user_roles.is_active + R30 trace endpoint 鉴权 + R34 密钥过期检查

---

_(TBD 段已被 8-12 P+ 闭环任务补全, 见 /home/lin/ObsidianVault/20-记忆/共享/research/P+6件follow-up闭环-2026-08-12.md)_

## Requirements change admin-webui-aaa-combo. Update Purpose after archive.
## Requirements
### Requirement: localhost默认暴露策略
系统从localhost访问Admin WebUI时SHALL允许匿名访问，无需认证。

#### Scenario: localhost直接访问
- **WHEN** 用户从localhost(127.0.0.1或::1)访问/admin/*
- **THEN** 系统允许访问，不要求认证

#### Scenario: 远程访问被拦截
- **WHEN** 用户从非localhost地址访问/admin/*
- **THEN** 系统返回401 Unauthorized，要求提供token

### Requirement: token认证机制
系统SHALL支持Bearer Token认证机制，通过HTTP头部传递认证信息。

#### Scenario: 有效token访问
- **WHEN** 用户提供有效Bearer Token访问/admin/*
- **THEN** 系统允许访问，请求正常处理

#### Scenario: 无效token访问
- **WHEN** 用户提供无效或过期Bearer Token
- **THEN** 系统返回401 Unauthorized，拒绝访问

#### Scenario: 缺失token访问
- **WHEN** 远程用户访问/admin/*且未提供Bearer Token
- **THEN** 系统返回401 Unauthorized，提示需要认证

### Requirement: 登录接口
系统SHALL提供`POST /admin/auth/login`接口生成访问token。

#### Scenario: 成功登录
- **WHEN** 用户POST正确凭据到/admin/auth/login
- **THEN** 系统返回JWT token，有效期24小时

#### Scenario: 登录失败
- **WHEN** 用户POST错误凭据到/admin/auth/login
- **THEN** 系统返回401 Unauthorized，不生成token

### Requirement: 操作审计日志
系统SHALL记录所有管理操作到审计日志，包含时间、用户、操作、结果。

#### Scenario: 密钥变更被记录
- **WHEN** 用户通过Admin UI修改API密钥
- **THEN** 系统记录审计日志：时间戳、用户标识、操作类型(UPDATE_KEY)、操作结果(SUCCESS/FAILURE)

#### Scenario: 配置调整被记录
- **WHEN** 用户通过Admin UI调整灰度百分比
- **THEN** 系统记录审计日志：时间戳、用户标识、操作类型(UPDATE_CONFIG)、变更前后值

#### Scenario: 审计日志查询
- **WHEN** 管理员查询/admin/audit/logs
- **THEN** 系统返回审计日志列表，支持时间范围和操作类型过滤

### Requirement: session_id派生
系统SHALL从请求头`X-Session-Id`或API key派生session_id用于灰度判定和审计。

#### Scenario: 从header派生session_id
- **WHEN** 请求头包含X-Session-Id
- **THEN** 系统使用该值作为session_id

#### Scenario: 从API key派生session_id
- **WHEN** 请求头不包含X-Session-Id但包含api_key
- **THEN** 系统对api_key做hash作为session_id

#### Scenario: session_id缺失
- **WHEN** 请求头不包含X-Session-Id和api_key
- **THEN** 系统session_id为None，不判定灰度

