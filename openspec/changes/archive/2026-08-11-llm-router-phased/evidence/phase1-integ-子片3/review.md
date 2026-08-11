# 子片 3 OpenCode 异构对抗审查 — CC 回应(HERMES 标签协议)

**审查模型**:deepseek-v4-pro Volcano(异构)
**审查文件**:`tests/integration/test_phase1_bug_interactions.py`(初版 579 行 → 修后约 800 行)
**审查命令**:`opencode run --pure --model volcengine-coding-plan/deepseek-v4-pro --dir /home/lin/projects/llm-router "$(cat .../opencode_review_prompt.md)"`
**审查 wall time**:~3 min(自包含 prompt v1 模式,继承 v2 教训)
**输出原始日志**:`opencode_review_output.log`(66 行)

## OpenCode 12 项发现 + CC 裁定

| # | OpenCode 标签 | OpenCode 严重度 | CC 标签 | 处理 |
|---|---|---|---|---|
| 1 | `test_breaker_global_open` last_reason 析取过弱 | **CRITICAL** | `[CONCEDE 立即修]` | 严格断言 `== "global_open"`(`_global_is_open` 短路失效抓不到) |
| 2 | `test_breaker_opens_key` 缺 cascade 第 4 次集成 | **HIGH** | `[CONCEDE 立即修]` | 加 `test_cascade_run_after_three_accumulated_hard_failures_skips_bad` |
| 3 | CostGate fail-open 路径零覆盖 | **HIGH** | `[CONCEDE 立即修]` | 加 `test_cost_gate_fail_open_when_ledger_query_raises`(mock raise) |
| 4 | `record_success` 不 setdefault 边界零覆盖 | MED | `[CONCEDE 立即修]` | 加 `test_breaker_global_open_caveat_unregistered_provider_blocked` |
| 5 | `no_candidates` 不区分 health vs cost_gate 来源 | MED | `[CONCEDE 立即修]` | 加 `test_no_candidates_via_cost_gate_quota_zero_distinct_from_health` |
| 6 | A2 SOFT 注册 production 真实性争议 | MED | `[DEFEND 驳回]` | OpenCode 自[CONSENSUS]承认无假阴 + MED #1 caveat 顺手覆盖 |
| 7 | compliance 顺序 spy 不覆盖 health | MED | `[CONCEDE 立即修]` | 扩 spy 到 `_SpyHealthStore` + `_SpyLedgerStore` |
| 8 | HALF_OPEN 状态零覆盖 | MED | `[DEFER 子片 4]` | 压测会自然触发恢复路径,有可执行替代方案 |
| 9 | budget 耦合 7 provider 不直观 | MED | `[CONCEDE 立即修]` | 用 `range(DEFAULT_RETRY_BUDGET + 1)` 表达 |
| 10 | `HealthStore.init/close` 缺失(纯 cost 测试) | LOW | `[DEFEND 不修]` | 实际无连接(纯参数对象,只有 cascade.run 才连)|
| 11 | `is_complete` 改动报错路径长 | LOW | `[DEFEND 不修]` | 维护性 LOW |

## 修补衍生(本会话立即修)

### CRITICAL #1 修补(`test_breaker_global_open_blocks_all_via_derived_aggregate`)

**OpenCode 反例**:把 `_global_is_open` 改 `return False`,cascade 退到逐 key 检查每跳 `key_open` 仍绿——全局派生熔断完全失效但测试不报。

**修法**:删原 `assert res.last_reason in ("global_open", "key_open")`,改 `assert res.last_reason == "global_open"`。

**反例闭合**:把 `_global_is_open` 改 `return False`,cascade 退到 key 检查 → 实得 `last_reason="key_open"` ≠ `"global_open"` → 立败。

### HIGH #1 修补(新增测试)

**OpenCode 评估**:`test_breaker_opens_key_after_three_consecutive_hard_failures` 只验 breaker 状态机,不验 cascade 路由——子片 1 OpenCode #1 defer 的明确内容是「cascade 内累积 3 HARD 后第 4 跳被 hard-skip」。

**修法**:加 `test_cascade_run_after_three_accumulated_hard_failures_skips_bad`——跨多请求累积 3 HARD,第 4 次 cascade.run() 验 bad 被 allow_request 拒(counter[bad]==3 不增),good 兜底。注意预先用 `record_failure(good, SOFT_CONTENT)` 注册 good 到 `_keys`(避开 caveat:record_success 不 setdefault 致 good 不在 `_keys` → 派生 global 早期 all-open 边界)。

### HIGH #2 修补(新增测试)

**OpenCode 评估**:CostGate.survivors `except Exception: return list(names)` fail-open 设计选择层零覆盖,可能薅羊毛。

**修法**:加 `test_cost_gate_fail_open_when_ledger_query_raises`——子类 `_BrokenLedgerStore.total` 抛 RuntimeError,验 CostGate.survivors 返全 names。**这是产品决策**(软约束不阻请求,同 health fail-open 理念)的测试覆盖,非 bug 修补。

### MED #1 修补(新增 caveat 测试)

**OpenCode 评估**:`record_success` 不 setdefault 是源码层已知 caveat,但零覆盖。

**修法**:加 `test_breaker_global_open_caveat_unregistered_provider_blocked`——production 真实场景:bad 经 cascade.run() 累积 3 HARD → OPEN(r3 内);good 从未失败 → 不在 `_keys`;派生 `_global_is_open` 仅遍历 `_providers_with_keys` = {bad} 全 OPEN → r3 中 good 被 `global_open` 无差别拒。

**关键发现**:**触发时机是 r3 的同一请求内**(bad 第 3 次 HARD 后立即 OPEN,接着 cascade 推进到 chain[1]=good 时 allow_request 已看到 derived global OPEN),不是 r4。这点初稿写错了,实测发现并修正。

**Phase 2 闭合路径**:若 `record_success` 改也 setdefault(让 good 在 `_keys` 保持 CLOSED),派生 global 看到 bad+good 不全 OPEN → 不冻结 → r3 兜底成功——本测会败 = caveat 已闭合。

### MED #2 修补(新增测试)

**OpenCode 反例**:`test_no_candidates_when_all_providers_dead_via_health` 用 health 全死触发 no_candidates,但若 health 过滤被静默删除,quota=0 同样能让 cost_gate 触发空 survivors → no_candidates,测试假绿。

**修法**:加 `test_no_candidates_via_cost_gate_quota_zero_distinct_from_health`——health 全活 + quota=0 → no_candidates,与 health 死亡路径互补。两测试同时存在 → 不依赖单一过滤层。

### MED #4 修补(扩 spy)

**OpenCode 反例**:原 spy 只包装 `CostGate.survivors`,若 compliance 被移到 `_surviving_candidates` 内部 health 之后(但仍早于 cost_gate),spy_calls 仍空,测试仍绿,但 layer 顺序已变 health → compliance → cost_gate,违 layer ① 契约。

**修法**:扩 spy 到 `_SpyHealthStore.latest_probe` + `_SpyLedgerStore.total`,验合规违规时三层(cost/health/ledger)均 0 调用——任一层被调即 layer 顺序错。

### MED #6 修补(语义化)

**修法**:`test_budget_exhausted_after_six_hard_failures_stops_at_seventh` 中 `names = [f"hard-{i}" for i in range(7)]` 改 `range(DEFAULT_RETRY_BUDGET + 1)`——budget 改动时自适应,失败信息直观。

## 驳回 / Defer 详情

### MED #6 [DEFEND 驳回] A2 SOFT 注册 production 真实性

**OpenCode 反例**:`record_failure(good, SOFT)` 是测试桩,production 中 record_success 不 setdefault,如果未来删除 record_failure 的 setdefault 逻辑,本测会失败暴露真实 bug 但语义倒置。

**驳回理由**:OpenCode 自己 [CONSENSUS] 后半承认"若 record_success 未来改也 setdefault,本测不受影响"——前半反例的"语义倒置"前提(删 record_failure setdefault)非生产改动方向,且**MED #1 caveat 测试已直接文档化这条 production 边界**,问题域已闭合。驳回。

### MED #5 [DEFER 子片 4] HALF_OPEN 零覆盖

**OpenCode 评估**:HALF_OPEN 状态(cooldown 到期后放 1 探测)零覆盖。

**Defer 理由**:HALF_OPEN 是熔断**恢复路径**,不是子片 3 的"BUG 跨场景交互"主题;子片 4 压测(模拟 burst 后冷却恢复)会自然触发,有可执行替代方案。

### LOW #1 [DEFEND 不修] HealthStore.init/close 缺失

**OpenCode 评估**:纯 cost_gate 测试创建 HealthStore 但未 init/close,SQLite 连接可能泄漏。

**驳回理由**:HealthStore `__init__` 仅存 path,真正的连接在 `init()` 才建立。纯 cost_gate 测试不调 cascade.run() → 不调 _ensure_store() → 永不 init() → 无连接需关闭。tmp_path 文件 OS 自然清理。

## 整文件结论

OpenCode 标的`[CHALLENGE] 子片 3 测试存在 2 项 CRITICAL/HIGH,需修复后重审`,经 CC 裁定:
- CRITICAL ×1 → 立即修(析取断言改严格)
- HIGH ×2 → 立即修(cascade 集成 + cost_gate fail-open)
- MED ×6 → 立即修 4(#1/#2/#4/#6/#9 实修;#7 spy 扩展)+ 1 驳回(#6 OpenCode 自相矛盾)+ 1 defer(#5 HALF_OPEN)
- LOW ×2 → 不修(实际无问题 + 维护性)

**修后状态**:14 测试(原 10 + 4 新);pytest **316 → 320p 零回归**。

**整文件结论**:**[CONSENSUS] 子片 3 测试可作 Phase 1 出厂回归门**(无遗留 CRITICAL/HIGH)。

**剩余 OpenCode defer 项的承接位置**:
- 子片 4 收口:#5 HALF_OPEN(压测后冷却恢复路径)+ 子片 2 OpenCode HIGH #1(lifespan 真启 + production data 监测)
