# P0-3 前端 UI 规格

## 概述
P0-3 阶段实现前端 UI，使用 HTMX+Jinja2 模板 + Chart.js 图表，提供密钥管理、监控 Dashboard、设置和备份恢复界面。本阶段预计 14 小时完成。

## UI 框架

### 技术栈
- **模板引擎**：Jinja2（Flask 内置）
- **前端框架**：HTMX（通过 CDN 引入）
- **图表库**：Chart.js（通过 CDN 引入）
- **无构建步骤**：纯服务端渲染 + HTMX 增量更新

### 布局结构
`base.html` 定义主布局：
- 顶部导航栏（Logo + 菜单）
- 侧边栏（Dashboard/Keys/Monitoring/Settings/Backups）
- 内容区（content block 扩展点）
- 页脚（版本信息）

### HTMX 集成
- 所有表单提交使用 `hx-post`/`hx-put`
- 列表加载使用 `hx-get` + `hx-trigger="load"`
- 自动刷新使用 `hx-trigger="every 5s"`

## 核心页面

### Dashboard 页面
`dashboard.html` 总览面板：
- 统计卡片：总请求数/活跃密钥数/错误率/平均响应时间
- HTMX 从 `/api/stats` 拉取数据
- 每 5 秒自动刷新
- 异常值红色高亮（错误率 > 5%）

### 密钥管理页面
`keys.html` 密钥列表+操作：
- 表格：Provider/Masked Key/权重/操作（编辑/删除/测试）
- HTMX 分页（`hx-get="/admin/api/keys?page=2"`）
- 模态框创建/编辑密钥（HTMX 弹出层）
- 删除确认对话框（防止误操作）

### 监控 Dashboard

#### 框架结构
`monitoring.html` 布局：
- 顶部统计卡（同 Dashboard）
- 图表区（3 个 canvas 元素）
- HTMX 拉取数据端点

#### 图表实现（待完善）
1. **请求量趋势图**（`monitoring_trends`）
   - 折线图显示 24 小时请求量
   - 从 `/api/monitoring/trends` 拉取数据
   - Chart.js 配置：时间轴/蓝色填充/响应式

2. **错误率分布图**（`monitoring_errors`）
   - 柱状图显示各 provider 错误率
   - 从 `/api/monitoring/errors` 拉取数据
   - 红色高亮错误率 > 5% 的 provider

3. **响应时间热图**（`monitoring_latency`）
   - 热力图显示各时段响应时间
   - 从 `/api/monitoring/latency` 拉取数据
   - 绿（<200ms）/黄（200-500ms）/红（>500ms）色阶

### 设置页面
`settings.html` 配置管理：
- 配置表单（routing/scanner/limits）
- HTMX 提交 `PUT /admin/api/config`
- 输入验证（范围/类型）
- 成功/失败 Toast 提示（HTMX 事件）
- 配置重置按钮（POST `/admin/api/config/reset`）

### 备份恢复页面
`backups.html` 备份列表+操作：
- 表格：文件名/大小/创建时间/操作（下载/恢复）
- HTMX 从 `/admin/api/backups` 拉取
- 备份创建按钮（POST `/admin/api/backup`）
- 进度指示器（HTMX 加载状态）
- 恢复确认流程（上传 .gz + 警告对话框）

## 静态资源

### CSS 样式
- `static/css/admin.css`：统一样式
- 响应式设计（移动端适配）
- 暗色模式支持（可选）

### JavaScript 工具
- `static/js/admin.js`：工具函数
- Toast 提示组件
- 模态框管理
- 图表初始化

## 交付物
- `src/llm_router/ui/templates/*.html`：所有页面模板
- `src/llm_router/ui/static/`：CSS/JS 静态资源
- 端到端测试（Selenium/Playwright 可选）
- 浏览器兼容性测试（Chrome/Firefox/Safari）


## R13 实装状态 (2026-08-05 三方共识 — R2 PASS 11A 标记)

### 已实装 (监控 Dashboard 框架)

**3.4 监控页面框架**: src/llm_router/ui/templates/monitoring.html (187 行, 4491 字符) 已实装, 含总请求数/成功率/活跃密钥数/平均响应时间 4 个统计卡片 + Chart.js CDN 占位.

### 留 Phase5 (3 个 follow-up)

**3.5 请求量趋势图**: monitoring.html 实测 grep `Chart|canvas|<script src=.*Chart` = **0 命中**. 折线图 (24h 请求量) 待实施, 需 src/llm_router/admin/metrics_api.py 加 `/api/admin/metrics/trends` 端点 + monitoring.html 加 `<script src="...chart.min.js"></script>` + Chart.js 配置块.

**3.6 错误率分布图**: 同上 0 命中. 柱状图 (各 provider 错误率, >5% 红色高亮) 待实施, 需 `/api/admin/metrics/errors` 端点 + monitoring.html Chart.js bar config.

**3.7 响应时间热图**: 同上 0 命中. 热力图 (各时段响应时间, 绿/黄/红色阶) 待实施, 需 `/api/admin/metrics/latency` 端点 + monitoring.html Chart.js heatmap config.

### Phase5 切片规划

- slice_id: phase5-monitoring-charts
- 范围: src/llm_router/admin/metrics_api.py (3 端点 trends/errors/latency) + monitoring.html (Chart.js JS 块) + tests/integration/test_monitoring_charts.py
- 风险: MED (前端 + 后端 + 测试 3 文件)
- 三方会诊: 必起 (B 类新功能切片, P126 触发)
- 依赖: phase4-r13-mark-complete 合并后

### 三方共识溯源

- R1: cc=timeout/codex=1104B/hermes=5561B
- R2: cc=743B/codex=886B/hermes=2467B
- 归档: ~/ObsidianVault/20-记忆/共享/research/R{{1,2}}-三方-llm-router-phase4-r13-20260805.md

---

## ADDED Requirements

### Requirement: 技术栈 (P0-3-frontend 阶段交付)
- **模板引擎**：Jinja2（Flask 内置）
- **前端框架**：HTMX（通过 CDN 引入）
- **图表库**：Chart.js（通过 CDN 引入）
- **无构建步骤**：纯服务端渲染 + HTMX 增量更新

#### Scenario: 技术栈 正常路径
- **WHEN 技术栈 按 spec 实施**
- **THEN 系统按 技术栈 设计实现, 单元 + 集成测试覆盖**

#### Scenario: 技术栈 异常路径
- **WHEN 技术栈 实施失败或配置缺失**
- **THEN 系统记录 ERROR 日志并降级到安全默认**

### Requirement: P0-3-frontend 实施完整性
P0-3 阶段实现前端 UI，使用 HTMX+Jinja2 模板 + Chart.js 图表，提供密钥管理、监控 Dashboard、设置和备份恢复界面。本阶段预计 14 小时完成。

#### Scenario: P0-3-frontend 任务全完成
- **WHEN P0-3-frontend 阶段实施**
- **THEN 全部子任务完成 (R7 8 mark x 实证), 集成测试覆盖 5+ 端点**

---

## ADDED Requirements

### Requirement: 技术栈 (P0-3-frontend 阶段交付)
系统SHALL实现该能力. - **模板引擎**：Jinja2（Flask 内置）
- **前端框架**：HTMX（通过 CDN 引入）
- **图表库**：Chart.js（通过 CDN 引入）
- **无构建步骤**：纯服务端渲染 + HTMX 增量更新

#### Scenario: 技术栈 正常路径
- **WHEN 技术栈 按 spec 实施并接收合规输入**
- **THEN 系统SHALL按 技术栈 设计返回预期结果, 单元测试覆盖**

#### Scenario: 技术栈 异常路径
- **WHEN 技术栈 接收非法输入或依赖缺失**
- **THEN 系统SHALL记录 ERROR 日志并降级到安全默认行为, 不崩溃**

### Requirement: P0-3-frontend 实施完整性
系统SHALL实现该能力. P0-3 阶段实现前端 UI，使用 HTMX+Jinja2 模板 + Chart.js 图表，提供密钥管理、监控 Dashboard、设置和备份恢复界面。本阶段预计 14 小时完成。

#### Scenario: P0-3-frontend 任务全完成
- **WHEN P0-3-frontend 阶段实施**
- **THEN 系统SHALL满足 R7 标完 119 task, 集成测试覆盖 5+ 端点, 0 pre-existing fail**

#### Scenario: P0-3-frontend OpenSpec validate PASS
- **WHEN 跑 openspec validate P0-3-frontend**
- **THEN 系统SHALL返回 PASS, 5 spec 都有 ## ADDED Requirements + #### Scenario: 头**
