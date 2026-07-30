# usage.py — r9.5 SQLite 跟踪 tpm/rpm/quota (仿 LiteLLM Redis 机制)

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from pathlib import Path


class UsageStore:
    """SQLite 跟踪 provider 使用情况: tpm/rpm/quota + capability/ip_safety"""

    def __init__(self, db_path: str = "/tmp/usage.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS provider_usage (
                    provider_name TEXT PRIMARY KEY,
                    tpm_used REAL DEFAULT 0,
                    rpm_used INTEGER DEFAULT 0,
                    quota_remaining INTEGER DEFAULT 100,
                    last_reset_at TIMESTAMP,
                    capability_count_json TEXT DEFAULT '{}',
                    ip_safety_skip_count INTEGER DEFAULT 0,
                    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS capability_match_log (
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    provider TEXT,
                    capability TEXT,
                    request_id TEXT
                )
            """)

            conn.commit()

    def record_request(
        self,
        provider: str,
        tokens_used: float = 0,
        capability: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        """记录一次请求，更新 tpm/rpm 使用量"""
        with sqlite3.connect(self.db_path) as conn:
            # 获取当前使用量
            cur = conn.execute(
                "SELECT tpm_used, rpm_used, capability_count_json FROM provider_usage WHERE provider_name = ?",
                (provider,),
            )
            row = cur.fetchone()

            if row:
                tpm_used, rpm_used, cap_json = row
                capability_counts = json.loads(cap_json) if cap_json else {}
            else:
                tpm_used, rpm_used = 0.0, 0
                capability_counts = {}

            # 更新使用量
            new_tpm = tpm_used + tokens_used
            new_rpm = rpm_used + 1

            # 更新 capability 计数
            if capability:
                capability_counts[capability] = capability_counts.get(capability, 0) + 1

            # 插入或更新
            conn.execute("""
                INSERT INTO provider_usage (
                    provider_name, tpm_used, rpm_used, capability_count_json, last_updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(provider_name) DO UPDATE SET
                    tpm_used = excluded.tpm_used,
                    rpm_used = excluded.rpm_used,
                    capability_count_json = excluded.capability_count_json,
                    last_updated_at = excluded.last_updated_at
            """, (provider, new_tpm, new_rpm, json.dumps(capability_counts)))

            # 记录 capability 匹配日志
            if capability and request_id:
                conn.execute(
                    "INSERT INTO capability_match_log (provider, capability, request_id) VALUES (?, ?, ?)",
                    (provider, capability, request_id),
                )

            conn.commit()

    def get_usage(self, provider: str) -> Dict:
        """获取 provider 使用情况"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT * FROM provider_usage WHERE provider_name = ?",
                (provider,),
            )
            row = cur.fetchone()

            if not row:
                return {
                    "provider_name": provider,
                    "tpm_used": 0,
                    "rpm_used": 0,
                    "quota_remaining": 100,
                    "capability_count_json": "{}",
                    "ip_safety_skip_count": 0,
                }

            return {
                "provider_name": row[0],
                "tpm_used": row[1],
                "rpm_used": row[2],
                "quota_remaining": row[3],
                "last_reset_at": row[4],
                "capability_count_json": row[5],
                "ip_safety_skip_count": row[6],
                "last_updated_at": row[7],
            }

    def reset_tpm_rpm(self, provider: str) -> None:
        """每分钟重置 tpm/rpm 计数"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE provider_usage
                SET tpm_used = 0,
                    rpm_used = 0,
                    last_reset_at = CURRENT_TIMESTAMP
                WHERE provider_name = ?
            """, (provider,))
            conn.commit()

    def check_quota_remaining(self, provider: str) -> int:
        """检查剩余额度"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT quota_remaining FROM provider_usage WHERE provider_name = ?",
                (provider,),
            )
            row = cur.fetchone()
            if row is None:
                return 0  # 不存在的 provider 返回 0
            return row[0] if row[0] is not None else 0

    def skip_provider(self, provider: str, reason: str = "quota") -> None:
        """标记 provider 跳过原因"""
        with sqlite3.connect(self.db_path) as conn:
            # 先确保 provider 存在
            conn.execute("""
                INSERT OR IGNORE INTO provider_usage (provider_name)
                VALUES (?)
            """, (provider,))

            if reason == "ip_safety":
                conn.execute("""
                    UPDATE provider_usage
                    SET ip_safety_skip_count = ip_safety_skip_count + 1,
                        quota_remaining = 0
                    WHERE provider_name = ?
                """, (provider,))
            else:  # quota or capability
                conn.execute("""
                    UPDATE provider_usage
                    SET quota_remaining = 0
                    WHERE provider_name = ?
                """, (provider,))
            conn.commit()

    def get_top_provider(
        self,
        providers: List[str],
        capability: Optional[str] = None,
        ip_safety: Optional[str] = None,
    ) -> str:
        """5 维排序返回最佳 provider

        排序维度 (优先级从高到低):
        1. quota_remaining > 0 (可用额度)
        2. ip_safety != "forbidden" (IP 安全)
        3. capability 匹配度 (capability_count_json 中该 capability 的计数)
        4. tpm_used (tokens per minute, 越低越好)
        5. rpm_used (requests per minute, 越低越好)
        """
        provider_scores = []

        for provider in providers:
            usage = self.get_usage(provider)

            # 维度 1: 额度检查
            if usage.get("quota_remaining", 0) <= 0:
                continue

            # 维度 2: IP 安全检查
            if ip_safety == "forbidden" and usage.get("ip_safety_skip_count", 0) > 0:
                continue

            # 维度 3: capability 匹配度
            cap_counts = json.loads(usage.get("capability_count_json", "{}"))
            cap_score = cap_counts.get(capability or "", 0) if capability else 0

            # 维度 4-5: 负向指标 (越低越好)
            tpm = usage.get("tpm_used", 0)
            rpm = usage.get("rpm_used", 0)

            # 综合评分: capability 越高越好，tpm/rpm 越低越好
            score = (cap_score * 1000) - (tpm + rpm * 10)
            provider_scores.append((provider, score))

        # 按评分降序排序，返回最高分 provider
        if not provider_scores:
            return providers[0] if providers else ""

        provider_scores.sort(key=lambda x: x[1], reverse=True)
        return provider_scores[0][0]

    def get_all_providers(self) -> List[str]:
        """获取所有 provider 列表"""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT provider_name FROM provider_usage")
            return [row[0] for row in cur.fetchall()]

    def cleanup_old_logs(self, days: int = 7) -> None:
        """清理旧日志"""
        cutoff = datetime.now() - timedelta(days=days)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM capability_match_log WHERE timestamp < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
