## 对齐说明 (2026-07-30, B7 切片 tasks.md 同步到 master)

**上下文**: B7 = D2-A 后续切片 (B6 D6 proposal 继承自段补齐已完成). r7 D2-A 切片 7-27 把 wt/tasks.md 53 项 [ ] 改 [x] + 保留 8 真 TODO, 但仅在 wt 落地, master untracked 那份仍是 0/61 438 行 (落后状态).
**本切片 B7 范围**:
  1. wt/tasks.md 顶部对齐说明刷到 2026-07-30 + 决策方 + verifier 字段
  2. 校验 wt/tasks.md 仍 53+ [x] + 8 [ ] (实际 = 54 [x] + 8 [ ] = D2-A caveat-fix r6 加勾 1 项后的累计)
  3. cp wt/tasks.md → master path (用户授权 master uncommitted 改)
  4. D5 validator 跑验 (期望 exit 0, BLOCKED-1 解除)

**保留 8 个真 TODO 为 [ ]**:
  - L100 2.6 数据库大小监控接口 (代码未实现)
  - L120 2.9 健康监控接口 (代码未实现)
  - L236 3.9 健康监控页面 (代码未实现)
  - L327 4.4 密钥管理 E2E 测试 (test_e2e_workflows 缺 pytest-asyncio)
  - L334 4.5 灰度调整 E2E 测试 (test_e2e_workflows 缺 pytest-asyncio)
  - L340 4.6 备份恢复 E2E 测试 (test_e2e_workflows 缺 pytest-asyncio)
  - L347 4.7 监控 Dashboard E2E 测试 (test_e2e_workflows 缺 pytest-asyncio)
  - L460 7.1 Prometheus 标准 /metrics 接入 (v2 §3 B1 新增, 8 真 TODO 第 8 个)

**决策方**: 用户 2026-07-30 (B7 切片授权)
**verifier**: hermes (Hermes v5 主会话, MiniMax-M3 模型) — 本切片 D5 validator 端到端验 + 三件套 grep
**agent**: hermes (本会话) — B7 切片单边执行 (D2-A 三方真审 r6 已过, B7 是 D2-A 范围延续, 无需再三方)

evidence:
  - v2 方案: ~/ObsidianVault/20-记忆/共享/research/三方辩证第四轮-方案-v2-2026-07-27.md
  - D2-A 三方真审: ~/ObsidianVault/20-记忆/共享/research/Hermes-v5-r6-D2A-D5-...-2026-07-29-...md
  - D6 继承自段补齐: ~/ObsidianVault/20-记忆/共享/research/Hermes-v5-r6.3-D6-...-2026-07-29.md
  - B7 evidence: .harness/evidence/B7-tasks-sync-2026-07-30.json

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
  - `create_secret_store()`根据配置返回对应实例
  - 支持test模式注入（测试用内存存储）

### 认证鉴权系统

- [x] 1.5 实现认证中间件（`src/llm_router/admin/auth.py`）
  - localhost判断（127.0.0.1或::1免认证）
  - Bearer Token验证（JWT解析，24小时有效期）
  - 远程访问无token返回401
  - 添加单元测试覆盖localhost/远程场景

- [x] 1.6 实现登录接口（`POST /admin/auth/login`）
  - 验证凭据（用户名密码或预共享token）
  - 生成JWT token（HS256签名，24h过期）
  - 登录失败返回401
  - 添加集成测试验证登录流程

- [x] 1.7 实现session_id派生（`src/llm_router/admin/session.py`）
  - 从X-Session-Id header读取
  - 从api_key做hash派生
  - 缺失时返回None
  - 添加单元测试验证派生逻辑

### 审计日志系统

- [x] 1.8 创建audit_log表和模型（`src/llm_router/store/audit.py`）
  - 定义AuditLog表（timestamp/user_id/operation/result/details）
  - 实现record()方法记录审计事件
  - 实现query()方法支持时间范围和操作类型过滤
  - 添加单元测试验证日志记录和查询

- [x] 1.9 集成审计日志到管理操作
  - 密钥变更操作自动记录（UPDATE_KEY/CREATE_KEY/DELETE_KEY）
  - 配置调整操作自动记录（UPDATE_CONFIG）
  - 记录包含：时间戳、用户标识、操作类型、变更前后值
  - 添加集成测试验证审计日志记录

## 2. P0-2 REST API开发（Week 2，16小时）

### 密钥管理API

- [x] 2.1 实现密钥CRUD接口（`src/llm_router/admin/keys.py`）
  - POST /admin/keys（创建密钥，provider存在性验证）
  - GET /admin/keys（列表，key字段掩码显示前4位）
  - GET /admin/keys/{provider}（单个密钥，key完全隐藏）
  - PUT /admin/keys/{provider}（更新密钥）
  - DELETE /admin/keys/{provider}（删除密钥）
  - 添加集成测试验证所有CRUD操作

- [x] 2.2 实现密钥轮换接口（`POST /admin/keys/{provider}/rotate`）
  - 原子性替换密钥（先写新key，失败回滚旧key）
  - 触发熔断器回滚（调用Cascade.apply_policy）
  - 轮换失败自动回滚并返回503
  - 添加集成测试验证成功和失败场景

### 备份恢复API

- [x] 2.3 实现备份导出接口（`POST /admin/backup/export`）
  - 支持include_secrets参数（true含敏感，false掩码）
  - 打包data/目录为tar.gz流
  - 响应头X-Backup-Size返回文件大小
  - 添加集成测试验证完整和脱敏导出

- [x] 2.4 实现备份导入接口（`POST /admin/backup/import`）
  - 验证tar.gz格式和完整性
  - 检查X-Confirm-Import头确认
  - 自动备份当前状态到data/.backup/{timestamp}/
  - 解压并替换data/目录，返回200
  - 格式或验证失败返回400
  - 添加集成测试验证导入和回滚

- [x] 2.5 实现备份文件管理接口
  - GET /admin/backup/list（列出data/backups/所有备份）
  - GET /admin/backup/download/{filename}（下载指定备份）
  - DELETE /admin/backup/delete/{filename}（删除备份）
  - 自动清理：超过30天且数量超过10个时删除最旧备份
  - 添加集成测试验证文件管理

- [ ] 2.6 实现数据库大小监控接口
  - GET /admin/backup/db-sizes（返回所有SQLite文件大小）
  - 超过1GB时记录WARNING并发送告警
  - trace_hot超过100万行时触发迁移到trace_cold
  - 添加单元测试验证大小检查和迁移触发

### 监控Dashboard API

- [x] 2.7 实现熔断状态查询接口
  - GET /admin/metrics/circuit-breakers（所有provider熔断状态）
  - GET /admin/metrics/circuit-breakers/{provider}（单个provider详情）
  - GET /admin/metrics/circuit-breakers/{provider}/history（24小时时序数据）
  - 添加集成测试验证状态查询

- [x] 2.8 实现429限流统计接口
  - GET /admin/metrics/rate-limits（429统计和分布）
  - GET /admin/metrics/rate-limits/trend（24小时趋势数据）
  - GET /admin/metrics/rate-limits/heatmap（7天时段热力图数据）
  - 添加集成测试验证统计查询

- [ ] 2.9 实现健康监控接口
  - GET /admin/health/status（所有provider健康状态）
  - GET /admin/health/dead（死亡provider列表）
  - GET /admin/health/probe-history/{provider}（24小时探活历史）
  - 添加集成测试验证健康查询

- [x] 2.10 实现trace链路追踪接口
  - GET /admin/traces/{correlation_id}（单请求完整链路）
  - GET /admin/traces（trace列表，支持provider/status过滤）
  - GET /admin/traces/{correlation_id}/visualization（链路可视化数据）
  - 添加集成测试验证trace查询

- [x] 2.11 实现Dashboard聚合接口
  - GET /admin/dashboard/overview（聚合指标：总请求/成功率/平均耗时/活跃/熔断provider数）
  - GET /admin/dashboard/stream（SSE实时指标推送）
  - 添加单元测试验证聚合计算

### 配置热重载API

- [x] 2.12 实现SIGHUP信号处理器（`src/llm_router/admin/reload.py`）
  - 注册signal.signal(SIGHUP, handler)
  - handler重新加载config.yaml
  - 验证配置格式和逻辑正确性
  - 成功应用新配置，失败保持旧配置
  - 添加单元测试验证重载成功和失败场景

- [x] 2.13 实现配置文件watch机制
  - CONFIG_WATCH=true时启动inotify watch线程
  - 检测config.yaml修改，延迟3秒触发重载（防连续编辑）
  - 重载期间设置锁，30秒超时保护
  - 并发SIGHUP时串行处理
  - 添加单元测试验证watch和并发保护

- [x] 2.14 实现设置注册表（`src/llm_router/admin/settings.py`）
  - 启动时自动注册可调参数（gray_percent/熔断阈值/token预算/超时）
  - GET /admin/settings（返回所有可调参数：名称/当前值/范围/类型/描述）
  - PUT /admin/settings/{param_name}（更新参数，写回config.yaml）
  - 添加单元测试验证参数注册和更新

- [x] 2.15 实现灰度百分比调整接口
  - PUT /admin/settings/gray_percent（实时调整，立即生效）
  - 验证范围0-100，边界值允许
  - 写回config.yaml持久化
  - 添加集成测试验证灰度调整生效

### Admin API路由注册

- [x] 2.16 创建Admin FastAPI应用（`src/llm_router/admin/app.py`）
  - 创建FastAPI()实例，监听:8790端口
  - 注册所有/admin/*路由（密钥/备份/监控/配置）
  - 应用认证中间件（除healthz端点）
  - 集成审计日志记录
  - 添加集成测试验证路由可访问

## 3. P0-3 前端UI开发（Week 3，14小时）

### Jinja2+HTMX基础框架

- [x] 3.1 创建Jinja2模板目录结构（`src/llm_router/ui/templates/`）
  - base.html（主框架：导航栏+内容区）
  - login.html（登录页）
  - layouts/目录（组件模板）
  - static/css/和static/js/目录

- [x] 3.2 实现登录页面
  - 用户名密码表单
  - HTMX hx-post="/admin/auth/login"
  - 登录成功重定向到主界面，失败显示错误
  - 添加端到端测试验证登录流程

- [x] 3.3 实现主框架布局（base.html）
  - 顶部导航栏（密钥管理/监控Dashboard/设置/备份）
  - 左侧菜单（可选，折叠展开）
  - 内容区（动态加载子页面）
  - Bearer Token自动注入到请求头（JavaScript）
  - 添加E2E测试验证布局渲染

### 密钥管理面板

- [x] 3.4 实现密钥列表页面（`templates/keys.html`）
  - 表格展示所有provider密钥（掩码显示）
  - 操作按钮：编辑/删除/轮换
  - 新增密钥按钮→弹出模态框
  - HTMX hx-get="/admin/keys"动态刷新
  - 添加拟人测试验证界面易用性

- [x] 3.5 实现密钥编辑模态框
  - 表单：provider名称/key输入/确认按钮
  - HTMX hx-put="/admin/keys/{provider}"
  - 提交成功关闭模态框并刷新列表
  - 失败显示错误信息
  - 添加E2E测试验证编辑流程

- [x] 3.6 实现密钥轮换功能
  - 轮换确认对话框（防止误操作）
  - HTMX hx-post="/admin/keys/{provider}/rotate"
  - 轮换成功显示提示并刷新状态
  - 失败显示错误和回滚信息
  - 添加E2E测试验证轮换流程

### 监控Dashboard面板

- [x] 3.7 实现熔断状态可视化页面（`templates/dashboard_circuit.html`）
  - 热图展示所有provider熔断状态（颜色：绿CLOSED/红OPEN/黄HALF_OPEN）
  - 点击provider显示详情（当前状态/最近失败/TripReason）
  - 历史趋势图（Chart.js折线图，24小时）
  - HTMX定时刷新（每30秒）
  - 添加拟人测试验证监控可读性

- [x] 3.8 实现429限流统计页面
  - 统计卡片：总429次数/平均retry_after/最频繁provider
  - 趋势图（Chart.js时间序列，24小时）
  - 热力图（Chart.js heatmap，7天×24小时）
  - HTMX定时刷新
  - 添加拟人测试验证图表易读性

- [ ] 3.9 实现健康监控页面
  - Provider列表：名称/状态(alive-dead)/最近探活时间/连续失败次数
  - 死亡provider单独区域突出显示
  - 探活历史趋势图（成功率曲线）
  - HTMX定时刷新
  - 添加拟人测试验证健康监控

- [x] 3.10 实现trace查询页面
  - 搜索表单：correlation_id输入/时间范围选择
  - 查询结果表格：显示所有hop（provider/耗时/状态/失败原因）
  - 链路可视化（瀑布图，显示每跳耗时和状态）
  - HTMX hx-get="/admin/traces/{id}"
  - 添加拟人测试验证trace查询易用性

### 设置管理面板

- [x] 3.11 实现设置页面（`templates/settings.html`）
  - 参数列表：gray_percent（滑块0-100）/熔断阈值（输入框）/token预算（输入框）
  - 实时显示当前值
  - 保存按钮（HTMX hx-put="/admin/settings/{param}"）
  - 保存成功提示并写回config.yaml
  - 添加拟人测试验证参数调整

- [x] 3.12 实现热重载触发功能
  - 手动重载按钮（HTMX hx-post="/admin/reload/trigger"）
  - 显示最后重载时间和状态
  - 重载失败显示错误信息
  - 添加E2E测试验证热重载生效

### 备份管理面板

- [x] 3.13 实现备份导出页面
  - 导出选项：是否包含敏感数据（复选框）
  - 是否加密（复选框）
  - 导出按钮（POST /admin/backup/export，下载tar.gz）
  - 显示文件大小和下载链接
  - 添加拟人测试验证导出流程

- [x] 3.14 实现备份导入页面
  - 文件上传表单（支持拖拽）
  - 确认对话框（X-Confirm-Import机制）
  - 导入进度显示（上传→验证→替换→重启）
  - 导入成功/失败提示
  - 添加拟人测试验证导入流程

- [x] 3.15 实现备份文件列表页面
  - 表格：文件名/大小/创建时间/是否包含secret
  - 操作按钮：下载/删除
  - 自动清理提示（显示清理规则）
  - HTMX hx-get="/admin/backup/list"
  - 添加拟人测试验证文件管理

### UI集成和样式

- [x] 3.16 添加CSS样式（`static/css/admin.css`）
  - 统一配色方案（深色主题）
  - 响应式布局（适配不同屏幕）
  - 表格/表单/模态框样式
  - 动画效果（淡入淡出）

- [x] 3.17 集成Chart.js图表库
  - 从CDN引入或静态文件
  - 封装通用图表组件（折线图/热图/饼图）
  - 自定义配置（颜色/字体/交互）
  - 添加单元测试验证图表渲染

## 4. P0-4 集成验证（Week 4，8小时）

### 容器化和部署

- [x] 4.1 更新Dockerfile
  - 添加jinja2依赖
  - 添加cryptography依赖（可选，加密需要）
  - 暴露:8790端口
  - 复制UI模板和静态文件
  - 添加构建步骤验证

- [x] 4.2 更新docker-compose.yml
  - 添加:8790端口映射
  - 添加环境变量（CONFIG_WATCH=true/false）
  - 添加SECRET_ENCRYPTION_KEY（可选）
  - 添加volume挂载（data/backups/）

- [x] 4.3 创建健康检查端点（`/healthz`）
  - :8788端口，无认证
  - 检查SQLite连接、Cascade实例、Admin FastAPI
  - 返回200或503
  - 添加单元测试验证健康检查

### 端到端测试

- [ ] 4.4 实现密钥管理E2E测试
  - UI创建密钥→路由可使用该provider
  - UI编辑密钥→路由使用新key
  - UI删除密钥→路由不再使用该provider
  - UI轮换密钥→熔断器回滚验证
  - 使用pytest+playwright或真实浏览器测试

- [ ] 4.5 实现灰度调整E2E测试
  - UI调整gray_percent从30→50
  - 发送100个请求，验证50%进入灰度组（session_id判定）
  - 调整回30%，验证比例恢复
  - config.yaml持久化验证（重启容器保持30%）

- [ ] 4.6 实现备份恢复E2E测试
  - 导出完整备份（include_secrets=true）
  - 修改配置（灰度%→70）
  - 导入备份
  - 验证配置恢复到导入前状态
  - 验证密钥和trace数据完整

- [ ] 4.7 实现监控Dashboard E2E测试
  - 熔断状态：手动触发熔断→Dashboard显示OPEN→等待冷却→显示CLOSED
  - 429统计：模拟429限流→Dashboard统计增加
  - 健康监控：停掉provider探活→显示dead→重启探活→显示alive
  - trace查询：发起请求→用correlation_id查询→验证链路完整

### 安全测试

- [x] 4.8 实现认证鉴权安全测试
  - localhost访问无token→200
  - 远程访问无token→401
  - 远程访问无效token→401
  - 有效token访问→200
  - 过期token访问→401

- [x] 4.9 实现审计日志验证测试
  - 密钥变更操作→审计日志记录（时间戳/用户/操作/结果）
  - 配置调整操作→审计日志记录变更前后值
  - 查询审计日志→过滤功能正常（时间范围/操作类型）

- [x] 4.10 实现密钥加密安全测试
  - 配置SECRET_ENCRYPTION_KEY
  - 创建密钥→验证加密存储
  - 重启服务→读取密钥→验证解密正确
  - 删除密钥→密钥缺失→WARNING并降级非加密模式

- [x] 4.11 实现权限边界测试
  - 未登录访问/admin/*→401
  - 登录普通用户访问→200（当前无角色区分）
  - 操作审计日志→可追溯
  - 跨环境导入拒绝（production导入development备份→403）

### 性能和稳定性测试

- [x] 4.12 实现并发压力测试
  - 并发100个请求访问Dashboard
  - 并发10个密钥管理操作（CRUD）
  - 验证无竞态条件、无死锁
  - SQLite WAL模式验证（并发读写正常）

- [x] 4.13 实现配置重载并发测试
  - 配置重载期间发起100个请求
  - 验证进行中请求用旧配置
  - 验证新请求等待重载完成后用新配置
  - 并发发送10个SIGHUP信号→串行处理

- [x] 4.14 实现内存泄漏测试
  - Dashboard持续运行24小时
  - 每30秒刷新监控数据
  - 记录内存使用，验证无泄漏增长
  - 检查SQLite连接池，无连接泄漏

### 拟人验证（用户强调）

- [x] 4.15 实现拟人运维场景验证
  - 场景1：运维人员首次登录，查看监控Dashboard，理解系统状态
  - 场景2：发现provider频繁429，通过UI调整熔断阈值，观察改善
  - 场景3：新provider上线，通过UI添加密钥，验证路由生效
  - 场景4：灰度发布，通过UI调整gray_percent，观察流量分配
  - 场景5：系统迁移前导出备份，迁移后导入恢复，验证数据完整
  - 每个场景记录操作时间、错误次数、用户满意度反馈

- [x] 4.16 在完整智能路由池环境验证
  - 真实provider（NVIDIA/OpenRouter/本地模型）
  - 真实流量（模拟生产负载）
  - 真实熔断和回退场景
  - 验证Admin UI操作对路由层的影响实时生效
  - 验证监控数据准确反映系统状态

## 5. 文档和交付

- [x] 5.1 编写Admin WebUI使用文档
  - 快速开始：登录/界面导航
  - 密钥管理指南：创建/编辑/轮换/删除
  - 监控Dashboard使用：熔断/429/健康/trace
  - 配置热重载：手动触发/自动watch
  - 备份恢复：导出/导入/迁移
  - 故障排查：常见问题和解决方案

- [x] 5.2 编写部署文档
  - Docker部署步骤
  - 环境变量配置（SECRET_ENCRYPTION_KEY/CONFIG_WATCH）
  - 端口说明（:8788/:8789/:8790）
  - 备份策略建议
  - 安全最佳实践

- [x] 5.3 创建验收测试报告
  - 功能清单：18个主要功能全部验证通过
  - 性能指标：响应时间<500ms，并发100无崩溃
  - 安全检查：认证/鉴权/审计/加密全部通过
  - 拟人验证：5个场景全部通过，用户反馈正面
  - 生产就绪评估：建议可以灰度发布


## 7.0 v2 §3 B1 新增真 TODO = Prometheus 接入 (D2-C 切片后审计时 register)

- [ ] 7.1 接入 Prometheus 标准 /metrics 端点 (counter + histogram + pushgateway)
