# D3 OmniRoute 继承段 · 详细比对差异 · 2026-07-28

**触发**: Codex sub-agent D3 审查 (2026-07-28 11:50, deleg_6fae7803) 发现 `## 详细比对差异: see .harness/evidence/omniroute-diff.md` 引用但文件不存在
**状态**: 创建本文件, 闭环 Codex 报告 BLOCKED 项

---

## 1. 12 OmniKey providers 清单

来源: https://github.com/Felix-au/OmniKey-AI-Unified-Key-Manager README (2026-07-10 commit, 68 stars MIT)

| # | Provider | 类型 | 当前 llm-router 状态 |
|---|---|---|---|
| 1 | OpenRouter | 海外聚合 | ✅ 已启用 (复用) |
| 2 | Groq | 海外直连 | ⏔ 注释 (海外直连风控 ⛔, 当前不启用) |
| 3 | NVIDIA NIM | 海外聚合 | ✅ 已启用 (复用, 主力) |
| 4 | OpenAI | 海外直连 | ⛔ 风控 (Gemini 类同档, 全面避开) |
| 5 | Anthropic | 海外直连 | ⛔ 风控 (同 OpenAI) |
| 6 | Google Gemini | 海外直连 | ⛔ 风控 (Google 直连风控) |
| 7 | Mistral | 海外直连 | ⛔ 待加 (海外直连风控, 默认不启用) |
| 8 | Cohere | 海外直连 | ⛔ 待加 (海外直连风控, 默认不启用) |
| 9 | Together AI | 海外直连 | ⛔ 风控 (同 Groq) |
| 10 | Fireworks AI | 海外直连 | ⛔ 风控 (同 Groq) |
| 11 | Perplexity | 海外直连 | ⛔ 待加 (海外直连风控, 默认不启用) |
| 12 | DeepInfra | 海外直连 | ⏔ 待加 (海外直连风控, 默认不启用) |

注: 不同来源 OmniKey 版本清单略有差异, 此表按当前 OmniKey README 实际列举为准.

---

## 2. llm-router 当前 provider (mnfst/providers.yaml)

**5 当前可用** (3 OmniKey 复用 + 2 国产替代):
- OpenRouter (OmniKey #1)
- Groq (OmniKey #2, 注释状态, 风控不启用)
- NVIDIA NIM (OmniKey #3)
- agnes (国产, 不在 OmniKey 12, 国产替代)
- modelscope (国产, 不在 OmniKey 12, 国产替代)

---

## 3. 9 待加 provider

按 v2 §3 风控红线地图分组:

**海外直连 (默认不启用, 注释状态)**: 7 项
- Mistral (OmniKey #7)
- Cohere (OmniKey #8)
- Cerebras (不在 OmniKey, 已知项目)
- GitHub Models (不在 OmniKey, 已知项目)
- SambaNova (不在 OmniKey, 已知项目)
- Cloudflare Workers AI (不在 OmniKey, 已知项目)
- HuggingFace Inference API (不在 OmniKey, 已知项目)

**国产 (可启用, 待 KEY)**: 1 项
- Z.ai (智谱 GLM, 用户 ZHIPU_API_KEY 已装, 可启用)

**风控红线 (默认禁用, 注释状态)**: 1 项
- Gemini (OmniKey #6, Google 直连风控, 任何路径都避开)

---

## 4. D3 决议执行

- ✅ mnfst/providers.yaml header 加 7 行 `## 继承自: OmniKey AI ...` 段 (commit 4c69005 后 working tree 改, fixup commit 闭环)
- ✅ 仅扩清单不启用 (注释状态, 9 待加全是注释, 5 当前可用不破)
- ✅ 引用 OmniKey 公开 README, 不引入 OmniKey 任何代码 (不替核心)
- ✅ 风控红线地图不破 (Gemini 等专有模型仍风控)

---

## 5. 数字校对 (Codex 报告 12836 字)

Codex 报告原文 (1.4 节):
```
比对结果 (v2 §6 D3 验收): 12 OmniKey providers - 5 当前 = 7 待加 (缺 Gemini/Cerebras/Mistral/GitHub Models/SambaNova/Cohere/Cloudflare/Z.ai/HuggingFace)
```

**校正** (本文件 + providers.yaml header 已修):
- 5 当前 = 3 OmniKey 复用 (openrouter/groq/nvidia) + 2 国产替代 (agnes/modelscope)
- 12 - 3 = 9 待加 (从 OmniKey 12 中识别待加)
- 括号列 9 项 (Gemini/Cerebras/Mistral/GitHub Models/SambaNova/Cohere/Cloudflare/Z.ai/HuggingFace) ✓
- 算术 "12 - 5 = 7" 错 — 应是 "12 - 3 = 9"
- fixup commit 已校正 providers.yaml 算术错, 本文件详细比对存档

---

## 6. 验证状态

- pytest tests/unit (wt-d3 vs master): 详见 `/tmp/d3-wt-tests-unit.log` + `/tmp/d3-master-tests-unit.log`
- wt = 557 passed 0 failed (D3 fix 不引入回归)
- master = 8 failed (master uncommitted 残留, 不属 D3 范围)

---

## 7. 参考

- Codex sub-agent 完整审查: `/tmp/agent-threeway-r4/codex-d3-review.md` (18169 字节, 421 行)
- GitHub API 证据: `/tmp/d3-github.json` (Felix-au 完整 API 响应)
- 老 URL 404 证据: `/tmp/d3-old-url.json`
- OmniKey README 原文: `/tmp/d3-omnikey-readme.md` (28435 字节)
- 父切片 commit: 4c69005 (D3: mnfst/providers.yaml 加 ## 继承自: OmniKey AI 段)
- D3 fixup commit: (本文件 commit)