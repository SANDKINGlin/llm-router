# P0-4-integration Specification

## Purpose
TBD - created by archiving change llm-router-phased. Update Purpose after archive.
## Requirements
### Requirement: Dockerfile (P0-4-integration 阶段交付)
系统SHALL实现该能力. 基于 `python:3.11-slim`：
- 安装系统依赖（gcc/redis-tools）
- 复制 `requirements.txt` 并 pip 安装
- 复制源代码
- 暴露 8765 端口
- CMD 启动 uvicorn（`src.llm_router.app:app`）

#### Scenario: Dockerfile 正常路径
- **WHEN Dockerfile 按 spec 实施并接收合规输入**
- **THEN 系统SHALL按 Dockerfile 设计返回预期结果, 单元测试覆盖**

#### Scenario: Dockerfile 异常路径
- **WHEN Dockerfile 接收非法输入或依赖缺失**
- **THEN 系统SHALL记录 ERROR 日志并降级到安全默认行为, 不崩溃**

### Requirement: P0-4-integration 实施完整性
系统SHALL实现该能力. P0-4 阶段完成 Docker 化、端到端测试、Prometheus 接入、安全测试和缺失端点补全，确保整个系统可部署、可监控、安全。本阶段预计 8 小时完成。

#### Scenario: P0-4-integration 任务全完成
- **WHEN P0-4-integration 阶段实施**
- **THEN 系统SHALL满足 R7 标完 119 task, 集成测试覆盖 5+ 端点, 0 pre-existing fail**

#### Scenario: P0-4-integration OpenSpec validate PASS
- **WHEN 跑 openspec validate P0-4-integration**
- **THEN 系统SHALL返回 PASS, 5 spec 都有 ## ADDED Requirements + #### Scenario: 头**

