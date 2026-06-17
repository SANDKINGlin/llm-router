# Plans.md · multi-pool-bundle Phase A 顶层账本

> **接手时间**:2026-06-17 18:43
> **接手自**:CC(Claude Code pts/3) 因 Anthropic 5h API 限额中断
> **本会话角色**:接班的 Codex(实施者) + OpenCode 节点 1/2/3 监督
> **OpenSpec change**:`openspec/changes/multi-pool-bundle/`(已 validate 通过)
> **上游决策锁死**:`openspec/changes/multi-pool-architecture/feasibility/decision-table.md`(8 项)
> **零回滚铁律**:不碰 `~/.claude/` / `~/.codex/` / `~/.cursor/` / `~/.roo/`

## 关键约束(任何违反 = 整个方案崩)

1. **Phase A 零回滚** — 只新加容器,不改任何现状配置
2. **不抽象 IDE** — Q2 锁死,各 IDE 升级不背 wrapper
3. **policy_enforcer 运行时拦截** — Q8 锁死,不能仅靠配置文件

## 任务账本

| Task # | 内容 | DoD | Depends | Status | Hash | Done at |
|---|---|---|---|---|---|---|
| A.1 | 验证 llm-router:test 镜像 + 源码 + git HEAD | 4 项命令输出符合预期,工作树干净 | - | cc:完了 | - | 2026-06-17 18:42 |
| A.2 | OpenSpec 立 change + 三件套 + specs/ | openspec validate 通过 | - | cc:完了 | - | 2026-06-17 18:45 |
| A.3 | 写 Plans.md 顶层账本(v2 格式) | Plans.md 存在,8 行 task 全列 | A.1 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:05 |
| A.3.5 | 🔴 OpenCode 节点 1 审查(架构定稿) | evidence/opencode-checkpoint-1.txt 含 approved | A.2,A.3 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:06 |
| A.4 | docker run 起容器 + /health 验证 | curl /health 返回 ok,evidence/docker-ps.txt 落盘 | A.1,A.2,A.3.5 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:06 |
| A.5 | 写最小 CLI(test-provider --mock) | pytest 绿 + 实际命令输出 [mock] openai OK | A.4 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:06 |
| A.6 | policy_enforcer 单测加强(同 provider 多账号) | 新测试存在,全量 pytest 零回归 | A.5 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:06 |
| A.4.5 | 🔴 OpenCode 节点 2 审查(编码完成) | evidence/opencode-checkpoint-2.txt 含 approved | A.4-A.6 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:06 |
| A.7 | git commit + tag v0.1.0-phaseA | commit + tag 在 git log 顶部 | A.4.5 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:06 |
| A.6.5 | 🔴 OpenCode 节点 3 审查(交付前) | evidence/opencode-checkpoint-3.txt 含 ship | A.4-A.6 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:15 |
| A.8 | 收尾(tasks.md 勾完 + evidence + SUMMARY) | openspec validate 通过,evidence ≥ 3 | A.6.5,A.7 | cc:完了 [5f51f1d] | 5f51f1d | 2026-06-17 19:15 |

## 状态统计

- **总任务**:11(含 3 个 OpenCode 审查节点)
- **已完成**:11(A.1 + A.2 + A.3 + A.3.5 + A.4 + A.4.5 + A.5 + A.6 + A.6.5 + A.7 + A.8)
- **进行中**:0
- **待办**:0
- **进度**:100%(11/11)
- **OpenCode 审查门**:3/3 approved + ship
  - 节点 1(架构定稿):approved
  - 节点 2(编码完成):approved
  - 节点 3(交付前):ship

## 决策日志(本会话)

| 决策 | 选择 | 理由 |
|---|---|---|
| OpenSpec 根路径 | `/home/lin/openspec/`(全局) | openspec CLI 用 CWD-relative,需从 `/home/lin/` 跑 |
| Plans.md 位置 | `/home/lin/projects/llm-router/Plans.md` | 项目内,跟代码一起 |
| CLI 入口 | `python -m llm_router.cli` | stdlib,无 typer/click 依赖 |
| OpenCode 触发方式 | 进程内 spawn_agent(Codex 托管) | 用户用 Codex 终端,跨终端复制不便 |
| evidence 目录 | `openspec/changes/multi-pool-bundle/evidence/` | 跟 change 走,变更关闭时一起归档 |

## 接手时检查清单(接班第一句)

- [x] CC 留下完整 research:multi-pool-architecture 19/20 task
- [x] 交班包就绪:01-codex-resume + 02-opencode-supervisor + 03-usage-guide
- [x] llm-router 底座:master @ 998e5a1,177p 绿
- [x] llm-router:test 镜像存在
- [ ] OpenCode 节点 1 审查(架构定稿)— **A.3.5 待触发**
- [ ] docker run 起容器验证 — **A.4 待执行**
- [ ] CLI 实际跑通 — **A.5 待执行**
- [ ] policy_enforcer 单测补全 — **A.6 待执行**
- [ ] git tag v0.1.0-phaseA — **A.7 待执行**

## 应急处理

| 情况 | 动作 |
|---|---|
| A.4 端口 8789 被占 | `docker ps` 找占用者,杀掉或换端口 |
| A.5 缺 typer/click | 改用 stdlib `argparse`,不引新依赖 |
| A.6 policy_enforcer 既有 API 不够 | 看 `src/llm_router/resilience/` 找接口,补 1 个 hook 而非重写 |
| A.3.5 OpenCode 打回 | 修 required_actions → 再召,直到 approved 才进 A.4 |
| 中途 API 限额 | 跟 CC 一样交班,留 Plans.md + 进度 evidence 即可 |

## 完成标志(Phase A DoD · 10 项)

- [ ] 镜像起得来 + /health 返回 ok
- [ ] 最小 CLI mock 跑通
- [ ] policy_enforcer 单测覆盖同 provider 多账号阻断
- [ ] git commit + tag v0.1.0-phaseA
- [ ] Plans.md 全标 `cc:完了 [hash]`
- [ ] evidence/ 至少 3 个文件
- [ ] OpenCode 节点 1 approved
- [ ] OpenCode 节点 2 approved
- [ ] OpenCode 节点 3 ship
- [ ] 全量 pytest 177+p 零回归
