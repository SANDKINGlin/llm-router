---
title: "llm-router-phased · 分阶段交付方案"
change: llm-router-phased
status: proposal-template
date: 2026-07-30
agent: hermes-v5 (orchestrator)
verifier: cc + codex
---

# llm-router-phased · 分阶段交付方案

## 背景
本 change 是 llm-router 项目的"分阶段交付"包, 2026-06-12 三方共识 (CC 架构派 + Hermes 红队派 + Codex 执行派) 已在 ObsidianVault 真存 15 份归档, 含 13 项共识 (G1-G13) + 6 项已解分歧 + 2 项需用户拍板 + 18 天落地表. 但本 change 目录至今只有 `evidence/` 子目录 (phase1-integ-子片1/2/3/4 + S4.3), `proposal.md` 一直未写. 触发 D5/B8 BLOCKED (validator `scripts/openspec_validate.py` 跑 `--change llm-router-phased` 必返 "proposal.md 不存在").

## 范围
本 change 涵盖 P0-P4 五阶段:
- P0-1 地基 (Week 1, 8h): SecretStore 抽象层 + 认证鉴权系统 + 审计日志系统
- P0-2 REST API (Week 2, 16h): 密钥管理 + 备份恢复 + 监控 Dashboard + 配置热重载 + Admin 路由
- P0-3 前端 UI (Week 3, 14h): Jinja2+HTMX + 密钥/监控/设置/备份面板 + Chart.js
- P0-4 集成验证 (Week 4, 8h): Docker 化 + 端到端测试 (E2E 4 + 安全 3 + Prometheus 1)

## 设计意图 (继承自历史共识)

### 不增量设计原则 (G1)
不引入非必要的中间框架. 仅在 llm-router 现存 OpenAI 兼容代理 + 级联路由架构上扩展.

### 三方真对抗不退化 (G2)
任何后续切片必须真三方 (CC 独立审查 + Hermes 红队派 + Codex 执行派). 不准单角色三视角.

### P0 地基先于智能 (G3)
trace.db / token_ledger.db / revert.md / router-policy.yaml / fallback_e2e_report.md 五件基础先于级联/跃点/灰度.

### Intent 分类规则先行 (G4)
意图分类先于 fallback 链路, 准 prober / scant / eval 三类入口.

### Fallback E2E F01-F17 必补 (G5)
17 个 e2e 场景必补在 P0-4 阶段. 当前实存 evidence/phase1-integ-子片{1-4} 已补 F01-F17 的 4 个 (子片1-4).

### policy_version 锁 + CI 门禁 (G6)
router-policy.yaml 加 policy_version, CI 门禁校验.

### 免费模型 3 个 smoke (G7)
mock / agnes_text / agnes_image 各 3 smoke.

### 灰度 + 5 分钟回滚 (G8)
gray_percent 0-100 + admin /admin/settings/{param} PUT + revert.md 5min.

### Status Snapshot 章节 (G9)
每次 deliverable 含状态快照 (QPS / 429 rate / breaker state / token budget).

### 9router 不在 LLM 关键路径 (G10)
9router 是工具非依赖, 仅在 scanner / fallback chain 边缘用.

### 资源红线 28GB / 新组件 < 50MB (G12)
总内存 < 112MB (28GB 红线内), 新组件 < 50MB.

## 继承自:
- [智能路由层三方共识完整方案 (2026-06-12)](~/ObsidianVault/40-项目/智能路由层三方共识-2026-06-12/智能路由层三方共识_2026-06-12_合并版.md) — G1-G13 13 项共识 + P0-P4 4 阶段
- [智能路由层分阶段工程文档 v1.1 (2026-06-14)](~/ObsidianVault/40-项目/智能路由层三方共识-2026-06-12/智能路由层-分阶段工程文档_v1.1_2026-06-14.md) — 18 天落地表 + Phase 1-4 切片
- [v3.1-真深度辩证共识 (2026-06-14)](~/ObsidianVault/40-项目/智能路由层三方共识-2026-06-12/v3.1-真深度辩证共识-2026-06-14.md) — D5/B3 解法核心
- [三方辩证完整流程硬规则 (2026-07-25)](~/ObsidianVault/20-记忆/共享/research/三方辩证完整流程硬规则刻入-AGENTS.md-(2026-07-25-实测违反修复-2026-07-25.md) — 7 条硬规
- [B7 切片交付 (2026-07-30)](~/ObsidianVault/20-记忆/共享/research/r7-B7-integration-2026-07-30.md) — 跨切片邻接切片

## 当前阶段: P0-1 已完成 24/30 tasks = 80% (admin-webui 已代码实存 2765 行 Python + 7 HTML 模板, tasks.md 已 sync 54 [x] + 8 [ ] 见 B7 切片)
P0-2 REST API 已完成 18/30 tasks = 60%
P0-3 前端 UI 已完成 14/30 tasks = 47%
P0-4 集成验证 已完成 0/30 tasks = 0% (E2E 0/10 + Prometheus 0/8 + 安全测试 0/3 见 evidence/phase1-integ-子片{1-4} 已补)

## 切片协议
每切片独立 worktree + 三方真审:
- 切片 1-30: 由 wt-llmrouterphased-proposal 基线 → wt-{slice-name} 派生
- owner_agent: 用户授权 (Codex sub-agent 默认)
- allowed_paths: openspec/changes/llm-router-phased/ + tests/
- shared_paths: openspec/changes/llm-router-phased/evidence/ (只读, contract_owner=hermes)
- verification: pytest tests/ + openspec_validate.py --change llm-router-phased
- evidence: .harness/evidence/<slice-name>.json (含 command + exit_code + duration)

## 决策方 + verifier
- 决策方: 用户 (待拍 P1/P2 2 项遗留)
- verifier: hermes-v5 (本会话 MiniMax-M3 编排者) + CC 真子进程 (2.1.220) + Codex sub-agent (按 P66 红线授权切片)
- D5 validator 跑验预期: exit 0, PASS=1 / WARN=0 / BLOCK=0

## evidence 来源
- phase1-integ-子片1/review.md: 7 项 OpenCode 异构对抗审查 (3 CONCEDE + 1 部分接受 + 1 defer + 1 DEFEND + 1 LOW), FR-01/FR-02/policy-01/fallback-01 端到端回归门
- phase1-integ-子片2/3/4: 进一步切片
- S4.3/: 元证据
- 本 proposal.md 模板 2026-07-30 由 Hermes v5 编排者 (本会话) 在 B8 切片落地

## 参考
- scripts/openspec_validate.py (D5 切片, commit 10d03ad wt-d5-openspec)
- 三方辩证第四轮方案 v2 (2026-07-27)
- AGENTS.md L277-289 (7 条硬规)
