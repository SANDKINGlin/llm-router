---
title: "backup-restore spec archive (R46 候选池 3/3 共识 A 方案)"
change: backup-restore-spec-archive
status: proposal-archive
date: 2026-08-13
agent: hermes-v5 (orchestrator)
verifier: cc + codex (3/3 共识 R46 候选池)
---

# backup-restore spec archive (R46 候选池 3/3 共识 A 方案)

## 背景
8-13 R46 候选池 5 项三方真验 (3/3 共识 A+D, 跟 R45 决策 Yes B+D precedent 一致), 选 A = R46 backup-restore spec archive 优先实施 (1-2h LOW). 8 Requirements + 25 Scenarios 已 R7 8-05 标完 (3.5K chars), 但 Purpose 段 TBD 留, spec 仍 [ ] 候选态. R20 import_backup R1-F3 已实装 (commit ffe4b0b, 高危+中危一次到位), R25 R20 归档追写 (4ffc88a 派 D Alternative). 触发 D5/B8 BLOCKED (validator `scripts/openspec_validate.py` 跑 `--change backup-restore-spec-archive` 必返 "spec 候选态未 archive").

## 范围
本 change 涵盖 backup-restore spec archive 一步到位:
- 补全 backup-restore/spec.md "## Purpose" 段 (跟 5 已 archive spec 模式一致: 一句话描述 + **R 链路映射**)
- 同步 master (cp spec.md 到 master, 跟 R44 同步 R29 模式)
- openspec validate --change backup-restore-spec-archive PASS

## 设计意图 (跟 R29 archive 流程一致)

### 单一 spec archive 模式 (G1)
- 跟 R29 8-11 archive 5 spec 模式 100% 一致, 不引入新 OpenSpec 流程.

### 8 Requirements + 25 Scenarios 已实装 (G2)
- R7 8-05 标完 119 task, backup-restore 8 Requirements + 25 Scenarios 全部 [x], 无 TODO 留.

### R-slice 链映射 (G3)
- R20 import_backup R1+F2+F3+R3+R4 (高危+中危一次到位, commit ffe4b0b)
- R25 R20 归档追写 (派 D Alternative, commit 4ffc88a)

## 继承自:
- [R44 0 pre-existing TODO 切片三方交付 (8-13 16:30)](~/ObsidianVault/20-记忆/共享/research/R44-PR-40-MERGED-最终闭环-2026-08-13.md) — R-slice 链 0 TODO 治本 + PR #40 MERGED
- [R45 终极方案 B+D 一次固化 (8-13 16:06)](~/ObsidianVault/20-记忆/共享/research/R45-终极方案-B+D-一次固化-(Codex-决策-Yes-B+D,-Hermes-整合)-2026-08-13.md) — 智谱 GLM 官方支持 + wrapper v5 fallback + decision tree skill
- [R46 候选池 5 项 3/3 共识 A+D (8-13 16:48)](~/ObsidianVault/20-记忆/共享/research/R46-候选池5项-3-3-共识-A+D-2026-08-13.md) — 三方真验 A+D 决策 (CC 主推 A+D + 备选 C+D + fallback B+E)
- [R7 切片交付 (8-05)](~/ObsidianVault/20-记忆/共享/research/r7-B7-integration-2026-07-30.md) — 119 task [x] 标完, backup-restore 8 Requirements + 25 Scenarios

## 当前状态: backup-restore 8 Requirements + 25 Scenarios 全部 [x] (R7 8-05 标完, 0 TODO 留)
