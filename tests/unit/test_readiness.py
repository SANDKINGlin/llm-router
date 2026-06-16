"""readiness · /healthz 真实就绪检查。

修审计 F1:/healthz 不再恒 ready——policy 没加载/无 provider 或 data 不可写 → 503,
Docker HEALTHCHECK 据此判健康(不再假绿)。
CB 恢复由 import 期 CircuitBreaker._load_state 保证(app 起来即恢复完成,不在此查)。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from llm_router.config import Policy, ProviderEntry
from llm_router.readiness import check_ready


def _mock_entry():
    return ProviderEntry(
        name="mock", tier="fast", quota=1, cooldown_s=1, is_free=True, cost_multiplier=0.0
    )


def _pol(providers):
    """造一个返回固定 Policy 的可调用(注入测试,免依赖真 yaml)。"""
    return lambda: Policy(providers=providers)


class TestCheckReady:
    def test_ok_when_policy_loaded_and_data_writable(self, tmp_path):
        ok, detail = check_ready(data_dir=tmp_path, policy_fn=_pol([_mock_entry()]))
        assert ok is True
        assert detail["policy"] == "ok"
        assert detail["data_writable"] == "ok"

    def test_fails_when_no_providers(self, tmp_path):
        """policy 加载但无 provider → 路由无候选 → 不就绪。"""
        ok, detail = check_ready(data_dir=tmp_path, policy_fn=_pol([]))
        assert ok is False
        assert detail["policy"] == "no_providers"

    def test_fails_when_policy_raises(self, tmp_path):
        """policy 加载抛异常(yaml 损坏等)→ 不就绪。"""
        def boom():
            raise RuntimeError("bad yaml")
        ok, detail = check_ready(data_dir=tmp_path, policy_fn=boom)
        assert ok is False
        assert detail["policy"].startswith("error")

    def test_fails_when_data_not_writable(self, tmp_path):
        """data 目录不可写(权限/挂载/满盘)→ 三库无法 init → 不就绪。"""
        if os.geteuid() == 0:
            pytest.skip("root 写只读目录仍成功,验不了")
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)  # 只读
        ok, detail = check_ready(data_dir=ro, policy_fn=_pol([_mock_entry()]))
        assert ok is False
        assert detail["data_writable"].startswith("error")


class TestHealthzEndpoint:
    def test_healthz_503_when_not_ready(self, monkeypatch):
        """不就绪 → /healthz 503(Docker HEALTHCHECK 据此标 unhealthy,修 F1 假绿)。"""
        import llm_router.readiness as r

        monkeypatch.setattr(r, "check_ready", lambda: (False, {"policy": "no_providers"}))
        from llm_router.app import app

        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"

    def test_healthz_200_with_detail_when_ready(self, monkeypatch, tmp_path):
        """就绪 → 200,detail 含各项检查(app.py healthz 默认调真 check_ready)。"""
        from llm_router.app import app

        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["detail"]["policy"] == "ok"  # 真检查(非恒 ready)
        assert body["detail"]["data_writable"] == "ok"
