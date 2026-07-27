#!/usr/bin/env python3
"""生成智能路由层完整方案 PDF(add-doc 生成交付)。

reportlab CIDFont 中文渲染(守 feedback_pdf-generation:禁用 fpdf2)。
内容:功能/执行逻辑/任务环节/使用/架构/数据/后续。
"""
from __future__ import annotations
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

OUT = "/home/lin/桌面/智能路由层-完整方案.pdf"

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontName="STSong-Light", fontSize=18, spaceAfter=12, textColor=HexColor("#1a5276"))
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName="STSong-Light", fontSize=14, spaceAfter=8, textColor=HexColor("#2874a6"))
H3 = ParagraphStyle("H3", parent=styles["Heading3"], fontName="STSong-Light", fontSize=12, spaceAfter=6, textColor=HexColor("#2e86c1"))
BODY = ParagraphStyle("Body", parent=styles["Normal"], fontName="STSong-Light", fontSize=10, leading=15, spaceAfter=6)
CODE = ParagraphStyle("Code", parent=BODY, fontName="Courier", fontSize=9, leading=12, leftIndent=12, textColor=HexColor("#1e8449"))
TIP = ParagraphStyle("Tip", parent=BODY, leftIndent=12, textColor=HexColor("#7d3c98"))

def P(t, s=BODY): return Paragraph(t, s)
def code(t): return Paragraph(t.replace(" ","&nbsp;").replace("\n","<br/>"), CODE)

doc = SimpleDocTemplate(OUT, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm, leftMargin=2*cm, rightMargin=2*cm)
story = []

# ── 封面 ──
story += [Spacer(1, 4*cm), P("智能路由层", H1), P("完整方案文档", H2), Spacer(1, 1*cm),
          P("版本 1.0 · 2026-06-22", BODY), P("llm-router :8789 · Docker 部署 · 6543 行代码 · 510 测试", BODY),
          Spacer(1, 0.5*cm), P("本文档覆盖:功能清单 / 执行逻辑 / 任务环节 / 部署使用 / 架构 / 运行数据 / 后续方向", BODY),
          PageBreak()]

# ── 1. 方案概述 ──
story += [P("1. 方案概述", H1),
    P("<b>是什么:</b> llm-router 是一个智能 LLM 路由层,聚合多个免费模型来源(NVIDIA NIM / OpenRouter / 动态扫描发现的模型),"
      "提供统一的 OpenAI 兼容 API(:8789),agent(Cline/CC/Roo/Codex)连一个端点即可用多个免费模型。", BODY),
    P("<b>为什么:</b> 免费模型来源分散、额度有限、偶发限流/风控。路由层自动筛选合格模型、熔断失败 provider、"
      "动态发现新模型热入池、按能力/免费/成本字典序选最优——agent 无感切换,始终拿到可用响应。", BODY),
    P("<b>核心价值:</b>", BODY),
    P("① 自动筛选(scanner 轮询→面试指令门→入池,过滤分类器/视觉/小模型/reasoning)", BODY),
    P("② 自动轮换(三层熔断 + fallback 链 + 探活 hard-skip,失败自动换下一跳)", BODY),
    P("③ 动态热入(每 1h 扫描新免费模型→面试→热入候选池,不重启服务)", BODY),
    P("④ 协议完整(OpenAI 兼容 streaming SSE + tools/function calling,Cline agentic 工具调用)", BODY),
    P("⑤ 可迁移(Docker 打包,63MB 镜像 tar 复制到任何机器即用)", BODY),
    Spacer(1, 0.3*cm)]

# ── 2. 功能清单 ──
story += [P("2. 功能清单(已实现)", H1),
    P("<b>路由决策</b>", H3),
    P("• 字典序排序键 (capability_match DESC, is_free DESC, cost_multiplier ASC)——非加权和,守 routing-priority-principle", BODY),
    P("• ε-greedy 策略(ε=0.3 起步,每 1000 请求衰减 10%,下限 0.05;mock 排除探索池)", BODY),
    P("• 能力匹配 TierMatcher(tier rank fast/medium/strong,task_type 关键词推断对口)", BODY),
    P("• 灰度 gray_percent(hash(session_id)%100,逐 agent 灰度可控)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>熔断与 fallback</b>", H3),
    P("• 三层级联 CircuitBreaker(key → provider → global,派生模型非独立状态机)", BODY),
    P("• key 级:3 连续硬失败 → OPEN(退避 30×2ⁿ 翻倍)→ HALF_OPEN(放 1 探测)→ CLOSED", BODY),
    P("• 429 精准退避(status_code + retry_after → RATE_LIMIT trip,不翻倍)", BODY),
    P("• SOFT_CONTENT(3 软=1 硬,空内容/残缺触发)", BODY),
    P("• Cascade fallback 链(最多 6 跳 budget,逐跳 trace 归因)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>动态池(scanner)</b>", H3),
    P("• 每 1h 轮询 NVIDIA NIM + OpenRouter /models 端点", BODY),
    P("• diff vs 上次快照 → added(新模型)/removed(下架)", BODY),
    P("• 面试指令门(probe 发 'Reply with exactly: PONG',回复含 PONG 才合格)", BODY),
    P("• 黑名单(safety/guard/vl/vision/thinking/reasoning/-1b/-2b/-3b/mini 结构性拒)", BODY),
    P("• 面试延迟上限 10s(慢模型直接 fail 不入池)", BODY),
    P("• on_tick_complete 回调 → apply_policy 原子重建候选池(content-hash version)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>探活(health-probe-lightweight)</b>", H3),
    P("• 每 5min GET {base_url}/models(轻量,不烧 token,不触发 thinking/限流排队)", BODY),
    P("• 探活池 41 个(静态 + 动态候选全覆盖)", BODY),
    P("• alive=False 的 key 路由前 hard-skip", BODY),
    Spacer(1, 0.2*cm),
    P("<b>协议透传(chat-protocol-passthrough)</b>", H3),
    P("• messages 结构保留(system/user/assistant 分离,不拍平)", BODY),
    P("• tools/tool_choice 透传(function calling 完整到达模型)", BODY),
    P("• ChatResult.tool_calls 响应(agent 据此触发工具)", BODY),
    P("• 真流式 SSE(stream=true 逐 token chunk,首字节秒回)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>安全与可观测</b>", H3),
    P("• 合规门 PolicyEnforcer(同 provider 多账号薅羊毛检测)", BODY),
    P("• CostGate(token 预算超额降级)", BODY),
    P("• 5 个 SQLite 持久化(trace/circuit/health/ledger/scanner,独立 WAL)", BODY),
    P("• /admin/rollback 回滚端点(policy_version 一致 guard,403 锁)", BODY),
    P("• monitor_routing.py 实时监测脚本(provider 分布/mock%/熔断/额度)", BODY),
    PageBreak()]

# ── 3. 执行逻辑 ──
story += [P("3. 执行逻辑(单次请求)", H1),
    P("<b>请求流(stream=false 非流式):</b>", H2),
    code("""Agent → POST /v1/chat/completions (messages + tools)
  → Cascade.run(messages, tools)
    ① 合规门 PolicyEnforcer.check()       ← 同 provider 多账号检测
    ② _surviving_candidates:               ← 路由前过滤
         health hard-skip (alive=False 剔)
         CostGate (超 token 预算 剔)
    ③ EpsilonGreedy.plan(survivors):
         字典序排序 (capability, is_free, cost)
         ε=0.3 探索 (非 mock 候选随机挑 primary)
         利用取 ordered[0]
    ④ 逐跳 (chain 最多 6 跳):
         trace acquire → CB allow? → provider.complete(messages, tools)
           ProviderError(429/5xx) → CB HARD (3次 trip OPEN)
           content空+无tool_calls → CB SOFT_CONTENT (3软=1硬)
           成功 → record_success + 返 ChatResult
    ⑤ 失败 fallback 下一跳;链耗尽 → 失败
  → JSON 响应 (content + tool_calls + finish_reason)"""),
    Spacer(1, 0.3*cm),
    P("<b>请求流(stream=true 真流式):</b>", H2),
    code("""Agent → POST /v1/chat/completions (stream=true)
  → _pick_stream_provider (首幸存候选)
  → provider.complete_stream (SDK stream=True)
  → 逐 chunk yield SSE:
       chunk1: delta.role + content 增量
       chunk2: tool_calls delta (function calling)
       ...
       末 chunk: finish_reason
       [DONE]
  首 chunk 前失败 → 回退非流式 _cascade.run (完整 fallback)"""),
    Spacer(1, 0.3*cm),
    P("<b>动态池维护(侧挂后台):</b>", H2),
    code("""DynamicScanner.run_loop (每 1h):
  tick():
    poll_all (NVIDIA + OpenRouter /models)
    diff vs snapshot → added / removed
    interview_batch (added):
      probe "Reply with exactly: PONG"
      passed = PONG in reply + 不在黑名单 + <10s
    removed → expire_entry
    on_tick_complete → apply_policy 重建候选池"""),
    Spacer(1, 0.3*cm),
    P("<b>探活(侧挂后台):</b>", H2),
    code("""HealthProber.run_loop (每 5min):
  对 41 个 _resolve_probe_targets (静态+动态):
    provider.health_check() → GET {base_url}/models (8s)
    2xx → alive=True / 非2xx超时 → alive=False
  写 health.db → Cascade 路由前 hard-skip 死亡 key"""),
    PageBreak()]

# ── 4. 任务环节 ──
story += [P("4. 任务执行环节(开发历程)", H1),
    P("<b>Phase 1 引擎(S0~S4,27 task)</b>", H3),
    P("• S0.0 骨架 → S1.3 ProviderEntry schema → S1.6 recovery_window 熔断", BODY),
    P("• S2.1 ε-greedy → S2.3 真 adapter → S2.4 cost gate → S2.7 合规门", BODY),
    P("• S2.8 探活 → S2.9 能力匹配 → S2.10 动态 scanner → S3.4 Golden Set Wilson", BODY),
    P("• S4.1 灰度 → S4.3 apply_policy 回滚", BODY),
    P("• Phase 1 集成验证 4 子片(BUG 跨场景 + happy path + 压测 + lifespan)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>Phase B 动态池真切流量(11 task)</b>", H3),
    P("• B1 造 ProviderEntry → B2 三层候选池 → B3 tick 重建回调", BODY),
    P("• B4 灰度/回滚守门 → B5 e2e 压测", BODY),
    P("• gray_percent 灰度可控(0=关动态池,100=全启用)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>Cline 真流量上线(2026-06-22 集中突破)</b>", H3),
    P("• health-probe-lightweight:探活 complete()→GET /models(大模型不超时误杀)", BODY),
    P("• chat-protocol-passthrough:messages+tools+tool_calls 透传(14 测试文件迁移)", BODY),
    P("• chat-stream-support→true-streaming:伪流式→真流式逐 token(ponytail 3 文件改)", BODY),
    P("• JSON 解析容错 + openrouter 换 qwen3-coder(防 reasoning 破损 JSON 500)", BODY),
    P("• scanner-interview-quality-gate:面试指令门(PONG)+ 黑名单(分类器/视觉/小模型拒)", BODY),
    P("• mock 排除探索池(72% mock→0%)+ 动态候选池探活(0→47 全 alive)", BODY),
    P("• 面试黑名单扩展+延迟上限(reasoning/小模型慢根因修)", BODY),
    Spacer(1, 0.2*cm),
    P("<b>Docker 打包收尾(11 task)</b>", H3),
    P("• .dockerignore + .env.example + README 3 步部署", BODY),
    P("• pyproject scripts + cli.py serve(纯 Python 安装)", BODY),
    P("• 镜像 tar 导出 63MB + 源码包 266KB(可迁移)", BODY),
    PageBreak()]

# ── 5. 如何使用 ──
story += [P("5. 如何使用", H1),
    P("<b>5.1 部署(3 步)</b>", H2),
    code("""# 1. 配置
cp .env.example .env
vi .env  # 填 NVIDIA_API_KEY=nvapi-xxx (build.nvidia.com 免费注册)

# 2. 启动
docker compose up -d
curl localhost:8789/healthz  # {"status":"ok"}

# 3. 接 agent
# Cline: API Provider → OpenAI Compatible
#   Base URL: http://localhost:8789/v1
#   API Key: 任意非空"""),
    Spacer(1, 0.3*cm),
    P("<b>5.2 换 key / 改配置</b>", H2),
    code("""docker compose restart           # 改 .env / yaml 后 restart 即生效
docker compose build && up -d    # 改源码后重建"""),
    Spacer(1, 0.3*cm),
    P("<b>5.3 迁移到新机(两种方式)</b>", H2),
    P("<b>方式 1:镜像 tar(63MB,免构建)</b>", BODY),
    code("""# 新机
docker load < llm-router-image.tar.gz
cp .env.example .env && vi .env
docker compose up -d"""),
    P("<b>方式 2:源码包(266KB,需 Docker)</b>", BODY),
    code("""tar xzf llm-router-bundle.tar.gz
cp .env.example .env && vi .env
docker compose build && docker compose up -d"""),
    Spacer(1, 0.3*cm),
    P("<b>5.4 监测</b>", H2),
    code("""python scripts/monitor_routing.py              # 单次快照
python scripts/monitor_routing.py --watch 30   # 每 30s 采样
python scripts/cline_loadtest.py               # agentic 压测(L1-L4)"""),
    Spacer(1, 0.3*cm),
    P("<b>5.5 风控红线(脏 IP 环境)</b>", H2),
    P("✅ NVIDIA NIM(脏 IP 不风控,免费档专属速率,主力推荐)", BODY),
    P("✅ OpenRouter(聚合器宽容,零额度高峰 429)", BODY),
    P("⛔ Groq/Together/Fireworks(海外直连,脏 IP 易风控)", BODY),
    P("⛔ Gemini/Google/OpenAI 直连(IP 干净度敏感)", BODY),
    PageBreak()]

# ── 6. 当前运行数据 ──
story += [P("6. 当前运行数据(2026-06-22 实测)", H1)]
data = [
    ["指标", "值", "说明"],
    ["候选池规模", "42", "nvidia + openrouter + 39 动态 + mock"],
    ["探活池规模", "41", "静态 + 动态全覆盖"],
    ["动态模型 active", "39", "过了面试指令门+黑名单"],
    ["gray_percent", "100", "动态池全启用"],
    ["mock 占比", "0%", "修复前 72%→0%(排除探索池)"],
    ["延迟范围", "0.4-3.1s", "修复前 0.4-20s(过滤 reasoning/小模型)"],
    ["代码行数", "6543", "src/ Python"],
    ["测试数", "510", "unit + integration + e2e"],
    ["pytest", "557p+3skip", "零回归"],
    ["git commits", "69", "Phase1 + PhaseB + Cline 真流量 + 打包"],
    ["镜像大小", "195MB", "多阶段构建,非 root"],
    ["镜像 tar", "63MB", "gzip 导出"],
    ["NVIDIA 额度", "0.0%", "3474 token / 800k 上限"],
]
t = Table(data, colWidths=[4*cm, 3*cm, 8*cm])
t.setStyle(TableStyle([
    ("FONTNAME", (0,0), (-1,-1), "STSong-Light"),
    ("FONTSIZE", (0,0), (-1,-1), 9),
    ("BACKGROUND", (0,0), (-1,0), HexColor("#2874a6")),
    ("TEXTCOLOR", (0,0), (-1,0), HexColor("#ffffff")),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [HexColor("#ebf5fb"), HexColor("#ffffff")]),
    ("GRID", (0,0), (-1,-1), 0.5, HexColor("#aed6f1")),
    ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
]))
story += [t, Spacer(1, 0.3*cm)]

# ── 7. 数据库 ──
story += [P("7. 持久化(5 个 SQLite WAL)", H1),
    P("• trace.db — 请求链路(每跳归因 hop_attribution,幂等 replay)", BODY),
    P("• circuit.db — 熔断状态(key/provider/global 派生)", BODY),
    P("• health.db — 探活结果(latest-wins UPSERT)", BODY),
    P("• ledger.db — token 用量记账(CostGate 读)", BODY),
    P("• scanner.db — 动态模型快照 + active 条目", BODY),
    P("• bandit_state.db — schema 预留(S3+ bandit 触发后填)", BODY),
    Spacer(1, 0.3*cm)]

# ── 8. 后续方向 ──
story += [P("8. 后续方向", H1),
    P("<b>已验证通过(不需要再做):</b>", BODY),
    P("• 动态池轮换(7 模型命中)、熔断记账、mock 治理、协议透传、真流式", BODY),
    Spacer(1, 0.1*cm),
    P("<b>门控等待(需真流量累积,不能硬做):</b>", BODY),
    P("• S3+ bandit(Thompson Sampling/UCB 自适应,触发条件:provider 翻转≥3次/2周)", BODY),
    P("• Phase2 集成验证(bandit 收敛 + 能力匹配 e2e)", BODY),
    Spacer(1, 0.1*cm),
    P("<b>可做但优先级低:</b>", BODY),
    P("• Web 管理面(add-admin-webui 18 task,CLI+JSON API 可替代)", BODY),
    P("• spec 文档合并到 openspec/specs/(归档时 --skip-specs 跳了)", BODY),
    P("• Anthropic /v1/messages 协议透传(CC 要 function calling 时再做)", BODY),
    Spacer(1, 0.1*cm),
    P("<b>大决策(需用户拍板):</b>", BODY),
    P("• 全局统一 :8789(让 CC/Roo/Codex 也走路由层,目前只有 Cline)", BODY),
    P("• 加国产 provider(魔搭/智谱,需用户给 key;NVIDIA 额度目前充足)", BODY),
    Spacer(1, 0.5*cm),
    P("— 文档结束 —", H3),
    P("生成:claude-code · 2026-06-22 · llm-router HEAD 08ebbb8", BODY),
]

doc.build(story)
print(f"PDF 生成: {OUT}")
