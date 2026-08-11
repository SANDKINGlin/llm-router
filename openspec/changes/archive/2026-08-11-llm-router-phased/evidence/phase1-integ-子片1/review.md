# 子片 1 OpenCode 异构对抗审查 — 回应（HERMES 标签协议）

**审查模型**：deepseek-v4-pro Volcano（异构）
**审查文件**：`tests/integration/test_phase1_critical_bugs.py` (332 → 354 行，含本会话补丁)
**审查命令**：`opencode run --pure --dir /home/lin/projects/llm-router -f opencode_review_prompt_v2.md ...`
**审查 wall time**：~6 min（自包含 prompt v2,禁止 Read 其他文件）
**输出原始日志**：`opencode_review_output_v2.log`

## OpenCode 7 项发现 + CC 回应

| # | OpenCode 标签 | OpenCode 严重度 | CC 标签 | 处理 |
|---|---|---|---|---|
| 1 | breaker 阻断零覆盖（`if not dec.allowed`） | HIGH | `[CONCEDE]` 等级降为 MED | defer 子片 2/3 |
| 2 | record_probe 签名不一致 | MED | `[DEFEND]` 锚点④文档错误 | 驳回 |
| 3 | SOFT_CONTENT 路径零覆盖 | MED | `[CONCEDE]` 接受 | defer 子片 2/3 |
| 4 | no_candidates 路径零覆盖 | MED | `[CONCEDE]` 接受 | defer 子片 2/3 |
| 5 | cost_gate 静默旁路 | MED | `[CONCEDE]` 部分接受 | defer 子片 3 |
| 6 | 前 6 跳 hop_attribution 未验证 | MED | `[CONCEDE]` 真假绿 | **本会话立即修** |
| 7 | `_store_ready` 私有属性脆性 | LOW | `[CONCEDE]` 不修 | 记 debt |

---

### #1 [CONCEDE 等级降] breaker 阻断零覆盖 → HIGH 降为 MED

**OpenCode 反例**：把 `cascade.py:341` 的 `if not dec.allowed:` 改成 `if False:`，4 测试仍绿。

**CC 判断**：反例成立，但严重度等级不应是 HIGH。理由：
- 子片 1 设计意图是 **4 CRITICAL BUG 端到端回归门**（FR-01/FR-02/policy-01/fallback-01），**不含 breaker 阻断路径覆盖**
- breaker 阻断路径在 `tests/unit/test_cascade.py` 已覆盖（unit 层职责）
- 子片 1 的"全组件 Cascade"声明意在证明 **多过滤层不互相干扰**（budget/health/policy/cost_gate 同时启用），而非"每条路径都触发"
- breaker 在子片 1 的状态是"装着但不触发"——这是 **单元 vs 集成的覆盖切分** 决定的

**处理**：defer 到子片 3（BUG 跨场景交互测试），届时加 breaker × policy_enforcer × budget 同触发场景。本会话不修。

---

### #2 [DEFEND] record_probe 签名不一致 — 驳回

**OpenCode 怀疑**：`_surviving_candidates` 调 `latest_probe(provider, key)`，但测试中 `record_probe(provider, latency_ms, alive)` 没传 key。

**CC 反驳**：health_store 实际就是 **provider 粒度**，不是 (provider, key) 粒度。证据：
- `src/llm_router/store/health_store.py:123-128` `latest_probe(providers: list[str] | None = None, *, alive_only: bool = False)` — **不需要 key 参数**
- `src/llm_router/store/health_store.py:156-163` `record_probe(provider: str, *, latency_ms, alive, at=None)` — **不需要 key 参数**
- `src/llm_router/api/cascade.py:155-157` 实际调用：`self._health_store.latest_probe(providers=self._candidate_names)` —— 只传 provider 名列表

**根因**：CC 写 prompt v2 §源码锚点④ 时把签名描述错了（写成"latest_probe(provider, key)"），误导了 OpenCode。**测试逻辑实际正确**（pytest 4 testcase 全绿，pDead/pB 确实被剔除）。

**处理**：驳回 OpenCode 这条 MED；勘误 prompt 锚点④。

---

### #3 [CONCEDE] SOFT_CONTENT 路径零覆盖 — defer

同 #1：不在 4 BUG 之列，已在 unit 层覆盖。defer 子片 2（端到端 happy path 应该测一条 SOFT_CONTENT → 切换到下一 provider 的链路）。

---

### #4 [CONCEDE] no_candidates 路径零覆盖 — defer

`_surviving_candidates` 全死返 `no_candidates`。defer 子片 3（BUG 跨场景交互），届时加"所有 provider 标 alive=False → cascade 直接返 no_candidates"测试。

---

### #5 [CONCEDE 部分接受] cost_gate 静默旁路

**OpenCode 怀疑**：`_entry()` 默认 `quota=1_000_000 / is_free=True / cost_multiplier=0.0`，cost_gate 实际未触发剔出行为。

**CC 部分确认**：
- 集成路径 **存在**（`cascade.py:168-169` 在 `_surviving_candidates` 中调 `self._cost_gate.survivors(survivors)`）
- 剔出行为 **零覆盖**（quota=1M 让所有 provider 通过）
- 这层旁路是子片 1 设计意图（"不让 cost_gate 干扰 4 BUG 验证"，见 `_entry()` docstring）

**处理**：defer 子片 3，届时加 quota 边界场景（quota=0、quota=exact、cost_gate × budget × policy 多过滤层同触发）。

---

### #6 [CONCEDE 立即修] 前 6 跳 hop_attribution 未验证 — **真假绿**

**OpenCode 反例**：把 `cascade.py:299-303` 的 `advance()` 改成 `initial_attribution(name)`（前 6 跳归因相同），`calls` 计数 + `len(chain)==7` + 末跳断言仍通过，**前 6 跳 from/to/reason 错乱抓不到**。

**CC 接受**：本切片是回归门，必须真覆盖 hop_attribution 的 `from/to/reason` 单调正确。

**修复**（本会话已落）：在 `test_bug_fr01_budget_exhausts_at_seventh_in_full_stack` 末尾追加循环断言：
```python
h0 = parse_attribution(chain[0].hop_attribution)
assert h0.depth == 0 and h0.reason == "initial"
assert h0.from_provider is None and h0.to_provider == names[0]
for i in range(1, 6):
    hi = parse_attribution(chain[i].hop_attribution)
    assert hi.depth == i
    assert hi.reason == "hard_failure"
    assert hi.from_provider == names[i - 1]
    assert hi.to_provider == names[i]
```

**验证**：`pytest tests -q` → **293 passed，零回归**（vs OpenCode 审前 293p 同一基线）。
若把 `advance()` 反例改成始终返 `initial_attribution`，新增的 `assert hi.reason == "hard_failure"` 必败 → 真覆盖。

---

### #7 [CONCEDE 记 debt] `_store_ready` 私有属性 — LOW 不修

**OpenCode 顾虑**：`tests/.../test_phase1_critical_bugs.py:286` `cascade._store_ready is False` 直接访问私有属性，重命名时 AttributeError 而非语义失败。

**CC 判断**：LOW 级别。理由：
- 这是子片 1 唯一能精确验证 layer ① layering 契约（"合规拦截不应触发 store init"）的方式
- 提供公共 getter 等价于在生产代码加 testing-only API（YAGNI）
- 测试已加注释解释为何用（`# 合规拦截先于 store 惰性 init,store 未 init → 跳过 close。`）

**记 debt**：若未来 `Cascade.__init__` 重构 `_store_ready`，需同步更新该断言（grep 即可定位）。

---

## 整文件结论

`[CONSENSUS] 子片 1 测试可作 Phase 1 出厂回归门`

**理由**：
- 0 CRITICAL（OpenCode 未提）
- 0 真 HIGH（OpenCode 1 项 HIGH 经审计降为 MED+defer，理由：单元 vs 集成覆盖切分）
- 1 MED 真假绿已修（前 6 跳 hop_attribution 断言补完，pytest 293p 零回归）
- 1 MED 驳回（health_store 签名误判，由 CC 写 prompt 锚点④ 错误引起）
- 4 MED defer 子片 2/3（breaker × SOFT × no_candidates × cost_gate × quota 等多过滤层交互场景）
- 1 LOW 记 debt（_store_ready 私有属性，可接受）

子片 1 本身（4 CRITICAL BUG 端到端回归 + 多组件不冲突 + 防假绿断言）的设计意图未被 OpenCode 实质动摇。

## 后续待办（不在本会话）

- **子片 2**（3-4h，端到端 happy path）：补 SOFT_CONTENT 切换链路（OpenCode #3）+ TestClient(app) 经 `/v1/chat/completions` 写读 trace+ledger 全链路
- **子片 3**（2h，BUG 跨场景交互 + S2.4 defer 3 盲区）：补 breaker × policy 同触发（OpenCode #1）+ no_candidates（OpenCode #4）+ cost_gate × budget × policy（OpenCode #5）+ exact quota / quota=0 / compliance×cost 顺序
- **子片 4**（3-4h，压测）：429 burst、fallback 链不雪崩

完成后 Phase 1 引擎合格出厂 → multi-pool-bundle Phase B 真切流量。
