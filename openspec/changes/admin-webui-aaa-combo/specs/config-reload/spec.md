## ADDED Requirements

### Requirement: SIGHUP信号处理
系统SHALL监听SIGHUP信号，触发配置热重载而不中断服务。

#### Scenario: 接收SIGHUP信号
- **WHEN** 系统进程收到SIGHUP信号
- **THEN** 系统重新加载配置文件(config.yaml)，验证后应用新配置

#### Scenario: 配置重载成功
- **WHEN** SIGHUP触发配置重载且新配置验证通过
- **THEN** 系统应用新配置，日志记录"Config reloaded successfully"，返回200状态码

#### Scenario: 配置重载失败
- **WHEN** SIGHUP触发配置重载但新配置验证失败
- **THEN** 系统保持旧配置，日志记录ERROR并说明原因，返回500状态码

### Requirement: 配置文件watch机制
系统SHALL支持配置文件变化监听，自动触发热重载。

#### Scenario: 文件变化检测
- **WHEN** config.yaml文件被修改(文件系统事件)
- **THEN** 系统检测到变化，延迟3秒后触发配置重载(防连续编辑触发多次)

#### Scenario: watch机制启动
- **WHEN** 系统启动时CONFIG_WATCH环境变量为true
- **THEN** 系统启动inotify watch线程监听config.yaml变化

#### Scenario: watch机制关闭
- **WHEN** CONFIG_WATCH环境变量为false或未设置
- **THEN** 系统不启动watch线程，仅支持SIGHUP手动触发

### Requirement: 配置验证
系统SHALL在重载配置前验证新配置格式和逻辑正确性。

#### Scenario: YAML格式验证
- **WHEN** 重载配置时YAML格式错误
- **THEN** 系统拒绝重载，保持旧配置，日志记录"YAML parse error"

#### Scenario: 必填字段验证
- **WHEN** 重载配置时缺少必填字段(如policy_version、gray_percent)
- **THEN** 系统拒绝重载，记录具体缺失字段

#### Scenario: 值范围验证
- **WHEN** 重载配置时gray_percent不在0-100范围
- **THEN** 系统拒绝重载，记录"gray_percent must be between 0 and 100"

#### Scenario: provider存在性验证
- **WHEN** 重载配置时引用不存在的provider
- **THEN** 系统拒绝重载，记录"Provider X not found in candidate list"

### Requirement: 无缝配置切换
系统SHALL确保配置切换过程中进行中请求不受影响。

#### Scenario: 新请求用新配置
- **WHEN** 配置重载成功后新请求到达
- **THEN** 新请求使用新配置参数(如新的灰度百分比、新的候选池)

#### Scenario: 进行中请求用旧配置
- **WHEN** 配置重载时有请求正在处理
- **THEN** 进行中请求继续使用重载前的配置，不受影响

#### Scenario: 配置回滚
- **WHEN** 配置重载失败(验证不通过)
- **THEN** 所有后续请求继续使用旧配置，系统保持稳定

### Requirement: 设置注册表
系统SHALL提供settings_registry组件，动态注册和管理可调参数。

#### Scenario: 参数注册
- **WHEN** 系统启动时
- **THEN** settings_registry自动注册所有可调参数：gray_percent、熔断阈值、token预算、超时时间

#### Scenario: 参数查询
- **WHEN** GET /admin/settings
- **THEN** 系统返回所有可调参数：名称、当前值、范围、类型、描述

#### Scenario: 参数更新
- **WHEN** PUT /admin/settings/{param_name}
- **THEN** 系统验证参数在合法范围内，更新后立即生效(无需重启)

#### Scenario: 参数持久化
- **WHEN** 通过Admin UI更新参数
- **THEN** 系统写回config.yaml文件，下次启动保持该值

### Requirement: 灰度百分比调整
系统SHALL支持实时调整灰度发布百分比，无需重启。

#### Scenario: 调高灰度百分比
- **WHEN** PUT /admin/settings/gray_percent，value从30改为50
- **THEN** 系统立即生效，新请求50%进入灰度组，写回config.yaml

#### Scenario: 调低灰度百分比
- **WHEN** PUT /admin/settings/gray_percent，value从50改为20
- **THEN** 系统立即生效，新请求20%进入灰度组，写回config.yaml

#### Scenario: 灰度0或100
- **WHEN** PUT /admin/settings/gray_percent，value为0或100
- **THEN** 系统正常处理(0=全量旧版，100=全量新版)，允许边界值

### Requirement: 并发竞态保护
系统SHALL防止配置重载时的并发竞态条件。

#### Scenario: 并发SIGHUP处理
- **WHEN** 短时间内收到多个SIGHUP信号
- **THEN** 系统串行处理重载，后一个等待前一个完成

#### Scenario: 重载期间锁定
- **WHEN** 配置重载执行中
- **THEN** 系统设置重载锁，拒绝新的重载请求，返回503 Service Unavailable

#### Scenario: 重载超时保护
- **WHEN** 配置重载超过30秒未完成
- **THEN** 系统自动终止重载，保持旧配置，记录"Config reload timeout"
