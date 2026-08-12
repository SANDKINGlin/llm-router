# admin-dashboard Specification

## Purpose
admin WebUI 完整功能 (key 管理 + 监控 + 配置 + metrics charts). 12 spec 已 R7 8-05 标完 119 task, R26 observability 接入真数据, R28c R30 R34 治本.

**R 链路映射**: R26 observability + R30 trace endpoint + R32 rotate_key + R34 key expiration

---

_(TBD 段已被 8-12 P+ 闭环任务补全, 见 /home/lin/ObsidianVault/20-记忆/共享/research/P+6件follow-up闭环-2026-08-12.md)_

## Requirements change admin-webui-aaa-combo. Update Purpose after archive.
## Requirements
### Requirement: 熔断状态可视化
系统SHALL提供熔断器状态实时查询接口和可视化展示。

#### Scenario: 查询所有熔断状态
- **WHEN** GET /admin/metrics/circuit-breakers
- **THEN** 系统返回所有provider熔断状态(CLOSED/OPEN/HALF_OPEN)、剩余冷却时间、失败次数

#### Scenario: 查询单个provider熔断状态
- **WHEN** GET /admin/metrics/circuit-breakers/{provider}
- **THEN** 系统返回该provider详细熔断状态：当前状态、最近失败时间、TripReason

#### Scenario: 熔断状态历史趋势
- **WHEN** GET /admin/metrics/circuit-breakers/{provider}/history?hours=24
- **THEN** 系统返回24小时熔断状态变化时序数据，用于绘图

### Requirement: 429限流统计
系统SHALL统计和展示429限流发生频率和分布。

#### Scenario: 429统计查询
- **WHEN** GET /admin/metrics/rate-limits
- **THEN** 系统返回各provider 429发生次数、最近时间分布、平均retry_after时长

#### Scenario: 429趋势图数据
- **WHEN** GET /admin/metrics/rate-limits/trend?hours=24
- **THEN** 系统返回24小时429发生趋势数据(每小时统计)，用于绘制时间序列图

#### Scenario: 429热力图数据
- **WHEN** GET /admin/metrics/rate-limits/heatmap?days=7
- **THEN** 系统返回7天429发生时段热力图数据(小时×星期几)

### Requirement: Provider健康监控
系统SHALL提供provider健康状态查询和监控能力。

#### Scenario: 健康状态查询
- **WHEN** GET /admin/health/status
- **THEN** 系统返回所有provider健康状态(alive/dead)、最近探活时间、连续失败次数

#### Scenario: 死亡provider列表
- **WHEN** GET /admin/health/dead
- **THEN** 系统返回所有alive=False的provider列表，用于运维关注

#### Scenario: 探活历史查询
- **WHEN** GET /admin/health/probe-history/{provider}?hours=24
- **THEN** 系统返回24小时探活结果时序数据(成功/失败、响应时间)

### Requirement: trace链路追踪
系统SHALL提供按correlation_id查询请求链路的能力。

#### Scenario: 查询单个请求链路
- **WHEN** GET /admin/traces/{correlation_id}
- **THEN** 系统返回该请求所有hop详情：provider、耗时、状态、失败原因

#### Scenario: 查询trace列表
- **WHEN** GET /admin/traces?provider=X&hours=1&status=failed
- **THEN** 系统返回符合条件trace列表，支持provider过滤、时间范围、状态过滤

#### Scenario: 链路可视化数据
- **WHEN** GET /admin/traces/{correlation_id}/visualization
- **THEN** 系统返回链路可视化格式数据(瀑布图/甘特图格式)，用于前端渲染

### Requirement: Dashboard聚合接口
系统SHALL提供Dashboard聚合查询接口，单次获取所有关键指标。

#### Scenario: Dashboard概览数据
- **WHEN** GET /admin/dashboard/overview
- **THEN** 系统返回聚合数据：总请求数、成功率、平均耗时、活跃provider数、熔断provider数

#### Scenario: 实时指标流
- **WHEN** GET /admin/dashboard/stream (SSE)
- **THEN** 系统建立Server-Sent Events连接，实时推送关键指标变化

### Requirement: 监控数据时区处理
系统SHALL统一使用UTC时区存储和返回时间数据，前端负责时区转换。

#### Scenario: 时间戳统一UTC
- **WHEN** 查询任何包含时间戳的监控接口
- **THEN** 所有时间字段使用ISO 8601格式，时区为UTC(后缀Z)

#### Scenario: 时区转换
- **WHEN** 前端接收UTC时间戳
- **THEN** 前端根据用户浏览器时区自动转换显示本地时间

