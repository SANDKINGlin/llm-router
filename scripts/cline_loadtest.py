#!/usr/bin/env python3
"""模拟 Cline agentic 多轮负载(cline-mcp-loadtest-nvidia M4)。

递增复杂度,测路由层在真实 agentic 负载下的表现:
- L1: 单轮短(基线)
- L2: 长 system prompt(Cline 真实 system ~2k token)
- L3: 带 tools 定义(function calling)
- L4: 多轮上下文(模拟工具结果回填后继续)
每级发 5 次,记录命中 provider / 延迟 / 失败。数据进 trace.db,monitor 自动采集。
"""
from __future__ import annotations
import asyncio
import time
import httpx

URL = "http://localhost:8789/v1/chat/completions"

# Cline 真实 system prompt 的精简版(~500 token,测长上下文)
CLINE_SYS = """You are Cline, a highly skilled software engineer with extensive knowledge in many programming languages, frameworks, design patterns, and best practices. Follow the user's instructions carefully. Use available tools when needed. Reply in Chinese (Simplified). Be concise."""

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
]


async def call(messages, tools=None, label=""):
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(URL, headers={"Content-Type": "application/json"},
                             json={"messages": messages, "tools": tools, "max_tokens": 100, "stream": False})
            dt = time.time() - t0
            d = r.json()
            model = d.get("model", "?")
            content = d.get("choices", [{}])[0].get("message", {}).get("content", "")[:40]
            print(f"  [{label}] {dt:.1f}s model={model} | {content!r}")
            return model, dt
    except Exception as e:
        dt = time.time() - t0
        print(f"  [{label}] {dt:.1f}s ERR {type(e).__name__}: {str(e)[:60]}")
        return "ERR", dt


async def main():
    print("=== L1: 单轮短(基线)===")
    for i in range(5):
        await call([{"role": "user", "content": f"用中文说一个字:第{i+1}"}], label=f"L1-{i+1}")
        await asyncio.sleep(1)

    print("\n=== L2: 长 system prompt(模拟 Cline 真实 system)===")
    for i in range(5):
        await call([{"role": "system", "content": CLINE_SYS},
                    {"role": "user", "content": f"用中文一句话介绍你自己(第{i+1}次)"}], label=f"L2-{i+1}")
        await asyncio.sleep(1)

    print("\n=== L3: 带 tools 定义(function calling)===")
    for i in range(5):
        await call([{"role": "system", "content": CLINE_SYS},
                    {"role": "user", "content": f"读 /tmp/test{i}.txt 文件(第{i+1}次)"}],
                   tools=TOOLS, label=f"L3-{i+1}")
        await asyncio.sleep(1)

    print("\n=== L4: 多轮上下文(模拟工具结果回填)===")
    for i in range(3):
        msgs = [
            {"role": "system", "content": CLINE_SYS},
            {"role": "user", "content": "读 /tmp/config.yaml"},
            {"role": "assistant", "content": "<read_file><path>/tmp/config.yaml</path></read_file>"},
            {"role": "user", "content": "[tool_result]name: app\nversion: 1.0\nport: 8080"},
            {"role": "user", "content": f"基于上面配置,用中文总结(第{i+1}次)"},
        ]
        await call(msgs, tools=TOOLS, label=f"L4-{i+1}")
        await asyncio.sleep(1)

    print("\n=== 压测完成,等 monitor 采集最后一批数据 ===")
    await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
