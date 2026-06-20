# 子片 2 OpenCode 异构对抗审查 — CC 回应(HERMES 标签协议)

**审查模型**:deepseek-v4-pro Volcano(异构,与 CC=GLM-5 / Codex=MiniMax M2.7 三方异构)
**审查文件**:`tests/integration/test_phase1_happy_path.py`(初版 516 行 → 修后约 660 行)
**审查命令**:`opencode run --pure --model volcengine-coding-plan/deepseek-v4-pro --dir /home/lin/projects/llm-router "$(cat .../opencode_review_prompt.md)"`
**审查 wall time**:~3 min(自包含 prompt v1 一次过,继承子片 1 v2 教训:嵌入完整源码 + 显式禁 Read)
**输出原始日志**:`opencode_review_output.log`

## OpenCode 7 项主要发现 + CC 裁定

| # | OpenCode 标签 | OpenCode 严重度 | CC 标签 | 处理 |
|---|---|---|---|---|
| 1 | TestClient lifespan 假设依赖实现细节 | HIGH | `[CONCEDE 等级降]` MED + defer | defer 子片 4(压测真启 lifespan) |
| 2 | `_extract_prompt` 完全未测试(provider 不消费 prompt) | MED | `[CONCEDE]` 真假绿 | **本会话立即修**:加 prompts 抓取 + 2 测试(双协议) |
| 3 | 缺失失败路径覆盖(端点静默 200) | MED | `[CONCEDE 部分接受]` | 加 1 compliance_blocked sanity 测试;主体 defer 子片 3 |
| 4 | Anthropic trace 不检查 hop_attribution(对称性) | MED | `[CONCEDE]` 接受 | **本会话立即修**:补 attr 断言 |
| 5 | `_record_usage` 顺序盲区(SOFT 跳应记账) | MED | `[CONCEDE]` 真盲区 | **本会话立即修**:_SoftProvider 配 usage + ledger 验 SOFT 行 |
| 6 | session_id 优先级分层无法区分 | MED | `[DEFEND]` 驳回 | 不修:契约只关心"显式优先",实现层封装非测试 bug |
| 7 | Anthropic 响应字段不完整(id/usage/created) | MED | `[CONCEDE 部分]` | **本会话立即修**:补 id 字段断言(顺手 OpenAI 也补) |

外加 LOW 与 [DEADLOCK]:
- LOW #1 monkeypatch 同名 provider 假绿:概率近零(conftest 清 key,production 候选只有 mock),**记 debt 不修**
- LOW #2 Bearer split JWT 反例:OpenCode 自反例分析最终承认"对无空格 token,split 行为一致" → **驳回(反例自我抵消)**
- LOW #3 `_FixedOrderStrategy` 候选去重未覆盖:测试 candidates 无重复,YAGNI **不修**
- [DEADLOCK] `LedgerStore`/`TraceStore` `__init__` 副作用未知:CC 已读源码,`__init__` 仅存 path,`init()` 才建 schema 惰性 → **解锁不修**
- [DEADLOCK] `build_adapters` / `policy()` 副作用:`build_adapters` 不发网络(只读 manifest yaml + env vars),`policy()` 读 yaml → **解锁不修**

---

## 修补 commit + 4 项 MED 详细说明

### #2 [CONCEDE 立即修] _extract_prompt 完全未测试 → 真假绿

**OpenCode 反例**:`_StubProvider.complete` 忽略 `prompt` 参数,所以即便把 `app.py:_extract_prompt` 改成 `return ""` 也全 10 测试绿。

**CC 修法**:
1. 给 `_StubProvider` 加可选 `prompts: list[str] | None` 参数,`complete()` 时 append。
2. 加 `test_extract_prompt_pipes_messages_to_provider`(OpenAI 路径)+ `test_extract_prompt_anthropic_path_also_pipes_messages`(Anthropic 路径)2 测试,验证拍平模式 `f"{role}: {content}"\n` 真送 provider。

**反例闭合验证**:把 `app.py:_extract_prompt` 改成 `return ""` → 新 2 测试断言 `captured == ["system: be concise\nuser: ping-A"]` 立败。

### #3 [CONCEDE 部分接受] 失败路径覆盖

**OpenCode 反例**:把 `PolicyEnforcer.check()` 改永远抛 `ComplianceError`,所有 10 测试仍绿——端点静默 200 + 空 content,断言不抓。

**CC 裁定**:这是**真问题但部分超出子片 2 范围**。
- 加 `test_compliance_blocked_returns_200_with_empty_content_sanity` 作 happy path 的**负面 sanity**——证 endpoint 真把 `(None, None, False, "compliance_blocked")` 转成空 content + model="mock"(`or ""` / `or "mock"`),**provider 未被调过**(`counter == {}`),**trace 表未建或为空**(_ensure_store 在 compliance check 后)。
- 主体失败路径(`no_candidates` / `budget_exhausted` / 全 SOFT)仍归子片 3(BUG 跨场景交互)。
- 错误响应塑形(把 `success=False` 映射 4xx/5xx)是已知设计 debt(子片 1 OpenCode #5 已 defer 同模式),**归未来切片**(非 Phase 1 范围)。

### #4 [CONCEDE 立即修] Anthropic trace 不检查 hop_attribution

**OpenCode 反例**:OpenAI 测试做了 `parse_attribution(rows[0]["hop_attribution"])` 断言,Anthropic 测试只查 provider/result。两者本应对称(同一 cascade.run 路径)。

**CC 修法**:`test_anthropic_endpoint_e2e_happy_path_writes_trace` 补 hop_attribution 断言(`depth=0/reason=initial/from=None/to=stubB`)+ `result == "hello-B"`。

### #5 [CONCEDE 立即修] _record_usage 顺序盲区(真盲区)

**OpenCode 反例**:把 `cascade.py` 的 `await self._record_usage(name, model, usage)` 从 is_complete **之前**移到**之后**(SOFT 跳不再记账)。`_SoftProvider` 返 `usage=None` → ledger 始终空 → 测试无感。

**CC 评估**:**真盲区**。设计意图是"无论内容是否完整,token 已消耗,故在 is_complete 判定**前**记"(`cascade.py:124` docstring + 行 371)。

**CC 修法**:
1. `_SoftProvider` 加可选 `usage` 参数(同 `_StubProvider`)。
2. `test_soft_content_falls_back_to_next_provider_with_correct_hop_chain` 给 _SoftProvider 配 `usage=Usage(prompt_tokens=20, completion_tokens=0)`,给 stubB 配真 usage。
3. 末尾加断言:`ledger_providers == ["soft", "stubB"]`(SOFT 跳必有记账行)。

**反例闭合验证**:把 `cascade.py:371-372` 的 `await self._record_usage(...)` 移到 `if not is_complete(...)` 之后 → 本测试 `assert ledger_providers == ["soft", "stubB"]` 实得 `["stubB"]` 立败。

### #7 [CONCEDE 部分立即修] Anthropic 响应字段不完整

**OpenCode 反例**:把 `app.py` Anthropic 端点的 `"id": "msg_mock"` 删除,测试仍绿(不查 id)。

**CC 修法**:Anthropic 测试补 `body["id"] == "msg_mock"`,顺手 OpenAI 测试补 `body["id"] == "chatcmpl-mock"`。`usage`/`created` 是 Phase1 hardcoded 全零,字段存在性不需特别测(已被 fastapi 的 pydantic 响应 schema 隐式守住——若删 endpoint 字段会被 200 → 内部异常)。LOW 不补。

---

## #1 [CONCEDE 等级降→MED + defer] HIGH TestClient lifespan 假设

**OpenCode 反例**:注释声明 "TestClient 不 with → lifespan 不触发,production data/*.db 不污染" 依赖 Starlette 实现细节。若 Starlette 未来版本默认触发 lifespan(无论 `with`),`_make_lifespan(_cascade, _probe_targets)` 会用 app 创建时捕获的**原始 production cascade**:`health_store.init()` 写 `data/health.db`、prober 对真 provider 发探测请求。**所有 10 测试仍绿**——因 endpoint patch 生效,trace/ledger 读 tmp_path。**唯一后果是静默污染 production data,测试无感**。

**CC 评估**:
1. 反例**前提是未来版本改 starlette**——hypothetical 而非当前事实。当前 `test_health.py` 等已有测试同样依赖此假设且 293p 全绿,这是项目级共识,非子片 2 引入的脆性。
2. 真要硬化,该用 conftest 加全局 fixture 检测 `data/` 写入,不该塞进单个测试文件(scope creep + 测试间假绿耦合)。
3. **子片 4(压测,3-4h)真启 lifespan**——届时验证 lifespan + 探活循环不雪崩,可顺带加 production data 监测。

**裁定**:HIGH **降 MED**(降级理由:hypothetical 依赖 + 现有项目级共识 + 有可执行替代方案——子片 4 真启 lifespan 时一并加监测);**defer 子片 4**(写入 tasks.md 子片 4 描述吸收)。

## #6 [DEFEND 驳回] session_id 优先级分层无法区分

**OpenCode 反例**:测试无法区分优先级逻辑在 `_extract_session_id` 还是在 `derive_session_id`。OpenCode 自己也走到"测试会绿"的结论,但又绕回"架构层假绿"。

**CC 驳回理由**:
1. 业务契约只关心**"explicit 优先于 Bearer 派生"**这一**外部行为**(`captured == ["explicit-session-42"]`),**不关心实现层封装**(在哪个函数实现优先级)。
2. OpenCode 自反例分析显示:把 `_extract_session_id` 优先级反转(`Bearer 优先`),`captured` 实得 blake2b 串而非 `"explicit-session-42"` → **测试立败**。这就证明了优先级行为契约可被本测试守住。
3. "无法区分实现在哪一层"是测试**粒度**问题,非测试**有效性**问题——契约级测试不该窥视实现细节,否则违反"black-box test"原则。
4. 真要测两层各自的责任分离,该写**单元测试**(`test_extract_session_id` / `test_derive_session_id` 各一),那是另一切片(单元测试现在分散在 `tests/unit/`,本切片是集成验证不重复)。

**驳回标签**:`[DEFEND]`。

---

## 整文件结论

OpenCode 标的`[CHALLENGE] 子片 2 测试存在 7 项 CRITICAL/HIGH/MEDIUM,需修复后重审`,经 CC 裁定:
- HIGH ×1 → 降级 MED + defer 子片 4(可执行替代方案)
- MED ×6 → 立即修 4(#2/#3/#4/#7 + #5 → 真盲区)+ 部分接受 1(#3 加 sanity 测试,主体 defer 子片 3)+ 驳回 1(#6 实现细节封装)
- LOW/DEADLOCK 全部解(读源码 + 概率近零 + 反例自我抵消)

**修后状态**:13 测试(原 10 + 3 新);pytest **303 → 306p 零回归**。

**整文件结论**:**[CONSENSUS] 子片 2 测试可作 Phase 1 出厂回归门**(无遗留 CRITICAL/HIGH;1 HIGH 降 MED 后已 defer 子片 4 有可执行替代方案)。

剩余 OpenCode defer 项的承接位置:
- 子片 3 收口:#3 主体失败路径(`no_candidates` / `budget_exhausted` / 全 SOFT 全链终态)
- 子片 4 收口:#1 lifespan 真启 + production data 监测
