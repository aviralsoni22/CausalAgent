"""Ingress auth + rate-limiting guards (app/core/security.py via app/main.py).

The Celery dispatch is stubbed so these exercise only the front-door guards, not
the worker. Each test sets the auth/limit posture explicitly via config so the
default (open) posture of one test never leaks into another.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import config, security


@pytest.fixture
def client(monkeypatch):
    # Never touch Celery/Redis — the front door is all we're testing.
    monkeypatch.setattr(main.run_causal_analysis, "apply_async", lambda *a, **k: None)
    security.reset_rate_limiter()
    # Generous default limit so auth tests aren't throttled; tightened per-test.
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1000)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_S", 60)
    return TestClient(main.app)


def _analyze(client, **headers):
    return client.post("/analyze", json={"query": "did the discount lift spend?"}, headers=headers)


def test_open_mode_allows_no_key(client, monkeypatch):
    monkeypatch.setattr(config, "INGRESS_API_KEYS", set())
    assert _analyze(client).status_code == 200


def test_enforced_rejects_missing_key(client, monkeypatch):
    monkeypatch.setattr(config, "INGRESS_API_KEYS", {"s3cret"})
    resp = _analyze(client)
    assert resp.status_code == 401


def test_enforced_rejects_wrong_key(client, monkeypatch):
    monkeypatch.setattr(config, "INGRESS_API_KEYS", {"s3cret"})
    assert _analyze(client, **{"X-API-Key": "nope"}).status_code == 401


def test_enforced_accepts_valid_key(client, monkeypatch):
    monkeypatch.setattr(config, "INGRESS_API_KEYS", {"s3cret"})
    resp = _analyze(client, **{"X-API-Key": "s3cret"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


def test_health_is_open_even_when_enforced(client, monkeypatch):
    monkeypatch.setattr(config, "INGRESS_API_KEYS", {"s3cret"})
    assert client.get("/health").status_code == 200


def test_rate_limit_trips_after_budget(client, monkeypatch):
    monkeypatch.setattr(config, "INGRESS_API_KEYS", set())
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 3)
    security.reset_rate_limiter()
    codes = [_analyze(client).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


def test_auth_runs_before_rate_limit(client, monkeypatch):
    # A caller with no key is rejected at auth even past the request budget — a
    # bad caller never consumes the limiter's allowance.
    monkeypatch.setattr(config, "INGRESS_API_KEYS", {"s3cret"})
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    security.reset_rate_limiter()
    assert [_analyze(client).status_code for _ in range(3)] == [401, 401, 401]
