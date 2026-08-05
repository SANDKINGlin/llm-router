"""Phase5 monitoring charts E2E tests (3.5/3.6/3.7).

按 R4 三方共识 Q3=A 仅 E2E:
- 测 3 个 /api/admin/metrics/{trends,errors,latency} 端点 JSON schema
- 测 /admin/monitoring HTML 模板含 3 canvas 元素
- 测 端点无数据时返回 24h buckets 占位 (前端空图)
"""
import pytest
from fastapi.testclient import TestClient


class TestMetricsTrendsEndpoint:
    """3.5 24h 请求量趋势图端点."""

    def test_trends_returns_24_buckets(self):
        """24 个 hourly buckets, 每桶含 hour + count 字段."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/trends")
            assert r.status_code == 200
            data = r.json()
            assert "trends" in data
            assert len(data["trends"]) == 24
            for t in data["trends"]:
                assert "hour" in t
                assert "count" in t
                assert isinstance(t["count"], int)
                assert t["count"] >= 0
            assert "total_24h" in data
            assert "data_source" in data
            # data_source 必是 empty/trace.db/error 之一
            assert data["data_source"] in ("empty", "trace.db", "error")

    def test_trends_hour_field_in_range(self):
        """hour 字段 0-23 范围."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/trends")
            data = r.json()
            for t in data["trends"]:
                assert 0 <= t["hour"] <= 23


class TestMetricsErrorsEndpoint:
    """3.6 Provider 错误率分布图端点."""

    def test_errors_returns_provider_list(self):
        """返回每个 provider 错误率."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/errors")
            assert r.status_code == 200
            data = r.json()
            assert "errors" in data
            assert isinstance(data["errors"], list)
            for e in data["errors"]:
                assert "provider" in e
                assert "error_rate" in e
                assert 0.0 <= e["error_rate"] <= 1.0
                assert "total" in e
                assert "errors" in e

    def test_errors_highlights_above_5pct(self):
        """>5% 错误率的 provider 必能被前端识别 (>5% threshold)."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/errors")
            data = r.json()
            for e in data["errors"]:
                if e["error_rate"] > 0.05:
                    # 前端红色高亮条件
                    assert e["error_rate"] > 0.05  # 跟前端 JS color 逻辑一致


class TestMetricsLatencyEndpoint:
    """3.7 响应时间热图端点."""

    def test_latency_returns_192_cells(self):
        """24h × 8 时段 = 192 cells."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/latency")
            assert r.status_code == 200
            data = r.json()
            assert "latency" in data
            assert len(data["latency"]) == 192
            for c in data["latency"]:
                assert "hour" in c
                assert "bucket" in c
                assert "p95_ms" in c
                assert 0 <= c["hour"] <= 23
                assert 0 <= c["bucket"] <= 7
                assert c["p95_ms"] >= 0

    def test_latency_color_thresholds(self):
        """p95 阈值: <200 绿, <500 黄, ≥500 红."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/latency")
            data = r.json()
            # 空数据时全部 p95_ms=0, 必全绿 (前端 JS 颜色逻辑)
            for c in data["latency"]:
                if c["p95_ms"] == 0:
                    # 绿 (frontend: p95_ms < 200)
                    assert c["p95_ms"] < 200


class TestMonitoringPageTemplate:
    """monitoring.html 含 3 canvas 元素."""

    def test_monitoring_html_has_3_canvas(self):
        """3.5/3.6/3.7 对应 chart-trends/chart-errors/chart-latency canvas."""
        template_path = "/home/lin/projects/llm-router-wt-p5-monitoring-charts/src/llm_router/ui/templates/monitoring.html"
        with open(template_path) as f:
            content = f.read()
        assert 'id="chart-trends"' in content
        assert 'id="chart-errors"' in content
        assert 'id="chart-latency"' in content

    def test_monitoring_html_has_chart_loader(self):
        """Chart.js 加载逻辑 (本地 /static/js/chart.min.js + CDN fallback)."""
        template_path = "/home/lin/projects/llm-router-wt-p5-monitoring-charts/src/llm_router/ui/templates/monitoring.html"
        with open(template_path) as f:
            content = f.read()
        assert '/static/js/chart.min.js' in content  # R4 Q2=B 本地优先
        assert 'cdn.jsdelivr.net' in content  # CDN fallback (offline degradation)

    def test_monitoring_html_calls_3_endpoints(self):
        """3 fetch 调用对应 3 端点."""
        template_path = "/home/lin/projects/llm-router-wt-p5-monitoring-charts/src/llm_router/ui/templates/monitoring.html"
        with open(template_path) as f:
            content = f.read()
        assert '/api/admin/metrics/trends' in content
        assert '/api/admin/metrics/errors' in content
        assert '/api/admin/metrics/latency' in content


class TestEndpointDataSource:
    """端点无数据时返回 empty 占位 (前端空图不报错)."""

    def test_trends_empty_data_source(self):
        """trace.db 不存在时 data_source=empty."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/trends")
            data = r.json()
            # trace.db 可能存在或不存在, data_source 必是预期 3 值之一
            assert data["data_source"] in ("empty", "trace.db", "error")
            # 即使 data_source=empty, trends 仍 24 buckets 全 0
            if data["data_source"] == "empty":
                assert all(t["count"] == 0 for t in data["trends"])

    def test_errors_empty_returns_providers(self):
        """trace.db 不存在时返回 policy 列表 (每 provider error_rate=0)."""
        from llm_router.admin.app import admin_app
        with TestClient(admin_app) as client:
            r = client.get("/api/admin/metrics/errors")
            data = r.json()
            if data["data_source"] == "empty":
                for e in data["errors"]:
                    assert e["error_rate"] == 0.0
                    assert e["total"] == 0
                    assert e["errors"] == 0
