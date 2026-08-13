## 对齐说明 (2026-08-13, R46 backup-restore spec archive)

**上下文**: 本 change (backup-restore-spec-archive) 是 R46 候选池 5 项三方真验 3/3 共识的 A 方案 (1-2h LOW 风险), 8 Requirements + 25 Scenarios 已 R7 8-05 标完, 但 Purpose 段 TBD 留, spec 候选态 [ ]. R20 import_backup R1-F3 已实装 (commit ffe4b0b, 高危+中危一次到位治本), R25 R20 归档追写 (4ffc88a 派 D Alternative).

**本切片范围**:
  1. 补全 backup-restore/spec.md "## Purpose" 段 (跟 5 已 archive spec 模式一致: 一句话描述 + **R 链路映射** + 注释 ObsidianVault 链接)
  2. 同步 master (cp spec.md 到 master, 跟 R44 同步 R29 模式)
  3. D5 validator 验证 (--change backup-restore-spec-archive PASS)
  4. 三方真验交付 (5 项真验 + 4 件套验证门)

**0 个真 TODO 留** (R7 8-05 标完):
  - 8 Requirements + 25 Scenarios 全部 [x] 治本

**决策方**: 用户 2026-08-13 16:50 (R46 候选池 3/3 共识 A+D, 你拍 GO 实施)
**verifier**: codex (Codex CLI, MiniMax-M3 模型) — D5 validator 端到端验 + 5 项真验
**agent**: cc (Claude Code, GLM-5 模型) — backup-restore spec archive 实施 (R46-A)
**orchestrator**: hermes-v5 — R46 候选池 3/3 共识整合 + 实施

evidence:
  - 三方共识归档: ~/ObsidianVault/20-记忆/共享/research/R46-候选池5项-3-3-共识-A+D-2026-08-13.md
  - R45 终极方案: ~/ObsidianVault/20-记忆/共享/research/R45-终极方案-B+D-一次固化-(Codex-决策-Yes-B+D,-Hermes-整合)-2026-08-13.md
  - R44 PR #40 MERGED: ~/ObsidianVault/20-记忆/共享/research/R44-PR-40-MERGED-最终闭环-2026-08-13.md

## 1. backup-restore spec Purpose 段补全 (8-13 16:50)

### [x] 1.1 改 backup-restore/spec.md "## Purpose" 段
- 改前: "TBD - created by archiving change admin-webui-aaa-combo. Update Purpose after archive."
- 改后: 配置数据备份/恢复/迁移/监控 (端到端). 8 Requirements + 25 Scenarios 已 R7 8-05 标完, R20 import_backup R1-F3 实装 (commit ffe4b0b), R20 高危/中危一次到位治本. spec 8 个 ADDED Requirements + Scenario 头 (8-13 R46 填, 跟 R29 archive 流程一致).
- 加 **R 链路映射** 段: R20 import_backup R1+F2+F3+R3+R4 + R25 R20 归档追写
- 加 _(TBD 段已被 8-13 R46 闭环任务补全, 见 .../R46-候选池5项-3-3-共识-A+D-2026-08-13.md)_ 注释

## 2. D5 validator 验证

### [x] 2.1 openspec validate --change backup-restore-spec-archive
- 命令: `cd /home/lin/projects/llm-router && .venv/bin/python scripts/openspec_validate.py --change backup-restore-spec-archive`
- 期望: PASS (proposal.md 含 ## 继承自: 段 + 5+ 链接到 ObsidianVault, tasks.md 全部 [x])
