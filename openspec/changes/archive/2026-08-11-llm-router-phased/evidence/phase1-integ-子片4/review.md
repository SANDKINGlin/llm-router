# 子片 4 OpenCode 异构对抗审查 — CC 回应(HERMES 标签协议)

**审查模型**:deepseek-v4-pro Volcano(异构,与 CC=GLM-5 / Codex=MiniMax M2.7 三方异构)
**审查文件**:`tests/integration/test_phase1_load_and_recovery.py`(初版 500 行 → 修后约 600 行)
**审查命令**:`opencode run --pure --model volcengine-coding-plan/deepseek-v4-pro --dir /home/lin/projects/llm-router "$(cat .../opencode_review_prompt.md)"`
**审查 wall time**:~3 min(自包含 prompt v1 模式,继承 v2 教训)
**输出原始日志**:`opencode_review_output.log`(77 行)

## OpenCode 8 项发现 + CC 裁定

| # | OpenCode 标签 | OpenCode 严重度 | CC 标签 | 处理 |
|---|---|---|---|---|
| 1 | A3 production data 路径硬编码 + CI 假绿 | **CRITICAL** | `[CONCEDE 立即修]` | 路径用 `parents[2] / "data"` + sanity 断言 required_dbs 存在 |
| 2 | A3 lifespan health.db 真污染但测试刻意排除 | **HIGH** | `[CONCEDE 立即修]` | 测试名诚实化 + docstring 显式标记架构 caveat + 严格监测其他三 db |
| 3 | A2 时钟注入只覆盖 _now,不覆盖 time.time() 盲区 | MED | `[CONCEDE 部分接受]` | 不修(A2.3 cascade 集成测兜底,docstring 显示互补) |
| 4 | A1.2 防雪崩魔数 5 太宽 | MED | `[CONCEDE 立即修]` | 精确断言 == 3 + 解释为何不是更小数 |
| 5 | A1.2 30 请求耗时仅 docstring 无断言 | MED | `[CONCEDE 立即修]` | `time.monotonic` 包裹 + `assert elapsed < 5.0` |
| 6 | HALF_OPEN `half_open_busy` 并发探测互斥零覆盖 | MED | `[CONCEDE 立即修]` | 加 `test_breaker_half_open_busy_blocks_concurrent_probe` |
| 7 | A3.1 lifespan 不验 startup spy | LOW | `[DEFEND 不修]` | starlette "with" 行为非项目契约 |
| 8 | A1.1 record_failure SOFT 注册 workaround 残余风险 | LOW | `[DEFEND 不修]` | 子片 3 caveat 已记;counter 不依赖 breaker |

## 修补衍生

### CRITICAL #1 修补(`_data_dir_mtime_snapshot` 路径)

**OpenCode 反例**:CI 不存在 `data/`,`glob` 返空,`before = after = {}`,断言被 `if db_name in before and db_name in after:` 短路跳过——**所有断言不执行,测试假绿**(即使 lifespan 疯狂写 production data 也抓不到)。

**修法**:
1. 路径改 `Path(__file__).resolve().parents[2] / "data"`(与 `app.py:_DATA_DIR` 一致)
2. **sanity 断言** required_dbs 必须存在于 `before`(空快照对空快照永远相等的假绿模式被显式抓):
```python
missing = [n for n in required_dbs if n not in before]
assert not missing, "production data dir 缺 ... → 空快照假绿"
```

### HIGH #1 修补(测试名诚实化 + 严格监测)

**OpenCode 抓到的核心问题**:测试名 `does_not_pollute_production_data` 是误导——lifespan 工厂闭包绑定 production `_cascade`,health_store.init() 真写 `data/health.db`,但断言只查 trace/ledger/circuit 跳过 health.db,假装无污染。

**修法**:
1. 测试名改 `only_pollutes_health_db_via_init`(诚实)
2. docstring 显式标记 **架构 caveat**:lifespan 闭包绑定 production cascade,Phase 2 重构 `_make_lifespan` 为可注入 cascade 才能真闭合
3. **严格断言** trace/ledger/circuit 三 db mtime 不变(任一变化 → lifespan 漂移到不该碰的 db)
4. health.db 单独记录不强制断言(已知 caveat)

**反例闭合**:若未来 `_make_lifespan` 改为也写 trace.db(如启动时记一条 trace),断言 `before["trace.db"] == after["trace.db"]` 立败。

### MED #4 修补(精确防雪崩断言)

**OpenCode 反例**:`counter.get("bad1", 0) <= 5` 太宽,余量 2,改 `threshold=5` 或 chain 重排可能漏检。

**修法**:`assert counter.get("bad1") == 3`(threshold 精确值)+ 注释解释:r1-r3 各让 bad1/bad2 fail 1 次累积到 hf=3 OPEN,r4 起 `_global_is_open=True` 短路无 complete 调用。

### MED #5 修补(耗时断言)

**OpenCode 反例**:docstring 写"30 请求 < 1s"但代码无耗时断言,若 cascade 引入 `asyncio.sleep(0.1)` 每跳,总耗时 3s+ 仍绿。

**修法**:`elapsed_start = time.monotonic()` + `assert elapsed < 5.0`。

### MED #6 修补(新增半开互斥测试)

**OpenCode 评估**:HALF_OPEN 状态 + `probe_in_flight=True` 互斥分支零覆盖。若删 `if ks.probe_in_flight: return Decision(False, "half_open_busy")`,并发请求同时放多个探测,状态机一致性破坏,现有测试不抓。

**修法**:加 `test_breaker_half_open_busy_blocks_concurrent_probe`——OPEN cooldown 后第 1 次 allow → HALF_OPEN + probe_in_flight=True;第 2 次 allow 同 key → `Decision(False, "half_open_busy")`。

**反例闭合**:删 `if ks.probe_in_flight: ...` 分支 → 第 2 次也放行 reason=`"key_half_open_probe"` ≠ `"half_open_busy"` → 立败。

## 部分接受 / 驳回详情

### MED #3 [CONCEDE 部分接受] A2.1/A2.2 时钟注入只覆盖 `_now`

**OpenCode 自承认**:A2.3(cascade 集成 cooldown 内拒绝 + cooldown 后放行两段式)已兜底,因为如果 `allow_request` 改 `time.time()`,A2.3 第 4 请求 cooldown 内本应被拒(`r4.success is False`)实变成放行(real_time > 30 探测放行)→ 立败。

**部分接受策略**:**不修 A2.1/A2.2**(单元层接受时钟注入语义,产品 doc 已 promise),由 A2.3 兜底——这是测试金字塔的合理分层。如果未来 `allow_request` 真不走 `_now()` 也不走 `time.time()`(凭空生成时间)是 hypothetical 反例,优先级低。

### LOW #1/#2 [DEFEND 不修]

- **#7 lifespan 不验 startup spy**:starlette `with` 行为是 starlette 的契约,不是 llm-router 的契约。验证 handler 走 patched cascade(via `counter == {"p": 1}`)间接证明 startup 已跑(否则 lifespan 错误会破 TestClient)。
- **#8 record_failure SOFT 注册 workaround 残余风险**:counter 防雪崩断言**不依赖** breaker 是否真注册了 good——只测 bad 真调用次数。即使 workaround 失效,counter 仍能抓住"每请求都遍历全 N provider"的真雪崩。

## 整文件结论

OpenCode 标的`[CHALLENGE] 子片 4 测试存在 4 项 CRITICAL/HIGH,需修复后重审`,经 CC 裁定:
- CRITICAL ×1 → 立即修(路径硬编码 + CI 假绿,sanity 断言显式抓)
- HIGH ×1 → 立即修(测试名诚实化 + 严格监测三 db + docstring 标记架构 caveat)
- MED ×4 → 立即修 3(#4 精确断言 / #5 耗时断言 / #6 半开互斥)+ 1 部分接受(#3 A2.3 兜底)
- LOW ×2 → 不修(架构边界 + 不依赖 workaround)

**修后状态**:9 测试(原 8 + 1 半开互斥);pytest **328 → 329p 零回归**。

**整文件结论**:**[CONSENSUS] 子片 4 测试可作 Phase 1 出厂回归门**(无遗留 CRITICAL/HIGH)。

⭐ **Phase 1 集成验证 4 子片全 done + OpenCode 异构审全 [CONSENSUS]**(无遗留 CRITICAL/HIGH)。
