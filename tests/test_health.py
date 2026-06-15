"""S0.0 验收:/healthz + 双协议经 MockProvider 返 canned。TDD:先于实现的契约。"""
from fastapi.testclient import TestClient

from llm_router.app import app

client = TestClient(app)


def test_healthz_returns_200():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_openai_endpoint_returns_mock():
    r = client.post(
        "/v1/chat/completions",
        json={"model": "mock", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert "[mock]" in body["choices"][0]["message"]["content"]


def test_anthropic_endpoint_returns_mock():
    r = client.post(
        "/v1/messages",
        json={"model": "mock", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 10},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert "[mock]" in body["content"][0]["text"]
