#!/usr/bin/env python3
"""路由层压测监测脚本(cline-mcp-loadtest-nvidia M2)。

采样 trace/circuit/health/ledger,聚合输出:provider 命中分布 / 熔断触发 / 动态模型生死 /
429 频率 / token 消耗 / NVIDIA 日额度。供方案方向判断。

用法:python3 scripts/monitor_routing.py [--watch 60] [--out report.md]
  --watch N  每 N 秒采样一次(Ctrl-C 退出);不传则单次快照。
  --out      写报告到文件(默认 stdout)。
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("/app/data") if Path("/app/data/trace.db").exists() else Path(__file__).resolve().parents[1] / "data"


def _q(db: str, sql: str, args=()):
    p = DATA / db
    if not p.exists():
        return []
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def snapshot() -> dict:
    """单次快照:聚合所有维度。"""
    ts = datetime.now(timezone.utc).isoformat()

    # trace: provider 命中分布 + mock 占比
    traces = _q("trace.db", "SELECT provider, result, created_at FROM trace ORDER BY rowid")
    prov_counter = Counter()
    mock_count = 0
    fail_count = 0  # result 空或 None = 失败(fallback 或 500)
    for t in traces:
        p = t["provider"] or "(none)"
        prov_counter[p] += 1
        if p == "mock":
            mock_count += 1
        if not t.get("result"):
            fail_count += 1
    total = len(traces)
    mock_pct = (mock_count / total * 100) if total else 0

    # 今天的数据(按 created_at 前缀过滤当天)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_traces = [t for t in traces if (t.get("created_at") or "").startswith(today)]
    today_prov = Counter(t["provider"] for t in today_traces)

    # circuit:熔断状态
    cb = _q("circuit.db", "SELECT provider, state, hard_failures, soft_failures FROM circuit_keys")
    open_keys = [r for r in cb if r["state"] == "open"]
    halfopen = [r for r in cb if r["state"] == "half_open"]

    # health:动态模型生死
    health = _q("health.db", "SELECT provider, alive, latency_ms FROM health")
    alive_n = sum(1 for r in health if r["alive"])
    dead_n = sum(1 for r in health if not r["alive"])
    dyn_dead = [r["provider"] for r in health if not r["alive"] and r["provider"].startswith("dyn-")]

    # ledger:NVIDIA 日 token 消耗 + 估算额度
    ledger = _q("ledger.db",
                "SELECT provider, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, COUNT(*) as n "
                "FROM token_ledger WHERE timestamp LIKE ? GROUP BY provider", (f"{today}%",))
    nv_today = next((r for r in ledger if "nvidia" in (r["provider"] or "")), None)
    nv_tokens = (nv_today["p"] + nv_today["c"]) if nv_today else 0
    nv_calls = nv_today["n"] if nv_today else 0
    # NVIDIA NIM 免费档约 1000 credits/天/模型(粗估 1 credit ≈ 1k token);阈值 800k token 告警
    NV_QUOTA_TOKENS = 800_000
    nv_pct = (nv_tokens / NV_QUOTA_TOKENS * 100) if NV_QUOTA_TOKENS else 0

    return {
        "timestamp": ts,
        "total_requests": total,
        "today_requests": len(today_traces),
        "provider_dist": dict(prov_counter.most_common()),
        "today_provider_dist": dict(today_prov.most_common()),
        "mock_pct": round(mock_pct, 1),
        "fail_count": fail_count,
        "circuit": {
            "open": [f'{r["provider"]} (hard={r["hard_failures"]})' for r in open_keys],
            "half_open": [r["provider"] for r in halfopen],
            "total_tracked": len(cb),
        },
        "health": {
            "alive": alive_n, "dead": dead_n,
            "dynamic_dead": dyn_dead[:10],
        },
        "nvidia_today": {
            "calls": nv_calls,
            "tokens": nv_tokens,
            "quota_pct": round(nv_pct, 1),
            "quota_limit": NV_QUOTA_TOKENS,
            "warning": nv_pct > 80,
        },
    }


def fmt_report(s: dict) -> str:
    lines = [
        f"# 路由层监测快照 {s['timestamp'][:19]}",
        "",
        f"## 总览",
        f"- 总请求: **{s['total_requests']}**(今天 {s['today_requests']})",
        f"- mock 占比: **{s['mock_pct']}%**(目标 <10%,修复前 72%)",
        f"- 失败(result 空): {s['fail_count']}",
        "",
        f"## provider 命中分布(全部 / 今天)",
    ]
    for p, n in s["provider_dist"].items():
        today_n = s["today_provider_dist"].get(p, 0)
        lines.append(f"- {p:40} {n:4} (今天 {today_n})")
    lines += [
        "",
        f"## 熔断状态",
        f"- OPEN: {s['circuit']['open'] or '(无)'}",
        f"- HALF_OPEN: {s['circuit']['half_open'] or '(无)'}",
        f"- 记账行数: {s['circuit']['total_tracked']}",
        "",
        f"## 探活/动态模型",
        f"- alive: {s['health']['alive']}, dead: {s['health']['dead']}",
        f"- 动态死亡: {s['health']['dynamic_dead'] or '(无)'}",
        "",
        f"## NVIDIA 日额度",
        f"- 今天调用: {s['nvidia_today']['calls']} 次",
        f"- 今天 token: {s['nvidia_today']['tokens']:,} / {s['nvidia_today']['quota_limit']:,} ({s['nvidia_today']['quota_pct']}%)",
        f"- {'⚠️ 接近额度上限!' if s['nvidia_today']['warning'] else '✅ 额度充足'}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, help="每 N 秒采样")
    ap.add_argument("--out", help="写报告到文件")
    args = ap.parse_args()

    def emit(report: str):
        if args.out:
            Path(args.out).write_text(report)
            print(f"写入 {args.out}", file=sys.stderr)
        else:
            print(report)
            print("\n" + "=" * 60 + "\n", flush=True)

    if args.watch:
        try:
            while True:
                emit(fmt_report(snapshot()))
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n停止监测", file=sys.stderr)
    else:
        emit(fmt_report(snapshot()))


if __name__ == "__main__":
    main()
