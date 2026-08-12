# P0-2-rest-api Specification

## Purpose
REST API 端到端 (OpenAI 兼容 + admin API + 监控). FastAPI + Pydantic schema + JWT auth. P0-2 阶段交付, 跟 admin WebUI (P0-3) 共享后端.

**R 链路映射**: R26 observability (circuit-keys / rate-limits / trends / errors / latency) + R30 trace endpoint

---

_(TBD 段已被 8-12 P+ 闭环任务补全, 见 /home/lin/ObsidianVault/20-记忆/共享/research/P+6件follow-up闭环-2026-08-12.md)_

## Requirements change llm-router-phased. Update Purpose after archive.
## Requirements
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

