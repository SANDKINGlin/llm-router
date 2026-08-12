# R37 R4 V5 三方数字交叉验证 (Codex 独立复现)

日期: 2026-08-11
WT: /home/lin/projects/llm-router-wt-r37-dynamic-quota
基准: Hermes R3-R4 (`hermes-r3-r4.md`)

## 真验独立复现 (3 验)

| 验 | 命令 | Codex | Hermes | 一致? |
|---|---|---|---|---|
| V1 | `pytest tests/unit -q` | **678** passed, 4 skipped, 9 warnings | 672 passed, 4 skipped, 9 warnings | ⚠️ +6 |
| V3 | `pytest tests/unit/test_dynamic_quota_r37.py -q` | **6** passed | 6 passed | ✅ |
| V4 | `pytest tests -q` | **851** passed, 9 skipped, 104 warnings | 851 passed, 9 skipped, 104 warnings | ✅ |

## V1 偏差解析

Hermes 报 V1=672 ("不变"), 但 672+6(R37 新测试)=678. Codex 实测 678 = 正确.
Hermes 报告的 V1=672 为实施前基线 (clean master), 非实施后数值.

## V3/V4 交叉一致

V3: 6/6 R37 治本精准真验 — 双方一致.
V4: 851 passed / 9 skipped / 104 warnings — 双方逐项一致, 零回归.

## 判定: ✅ 治本

- 1 file 改 (`scanner/dynamic.py` ~22 lines) source-based quota 查表
- 跟 L153 `_SOURCE_BASE_URL.get` 同款模式, 架构自然融合
- 0 schema 改, 0 prod 业务逻辑侵入
- 风险 LOW, 跟 R30/R32/R34 治本 precedent 一致
