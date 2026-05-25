"""Unit tests for the Phase 4 observability layer.

Pure-function tests of the tracing gate and the per-run tagging — no LangSmith,
network, or LLM. They pin the two behaviours that matter for safety and
usefulness: (1) tracing is OFF unless explicitly switched on *with* a key, and
the gate-off path forces LANGCHAIN_TRACING_V2 to "false" so an ambient env var
can't leak prompts; (2) run_config tags each call with task / node / attempt.
"""
from __future__ import annotations

import os

import pytest

from app.core import config
from app.core import observability


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Each test gets a clean env and a fresh 'configured-once' flag."""
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
    monkeypatch.setattr(observability, "_configured", False)
    yield


def test_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "LANGSMITH_TRACING", False)
    monkeypatch.setattr(config, "LANGSMITH_API_KEY", "")
    assert observability.configure_tracing() is False
    # Forced off, not merely left unset — defends against ambient env leakage.
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert "LANGCHAIN_API_KEY" not in os.environ


def test_on_when_switched_on_with_key(monkeypatch):
    monkeypatch.setattr(config, "LANGSMITH_TRACING", True)
    monkeypatch.setattr(config, "LANGSMITH_API_KEY", "ls-secret")
    monkeypatch.setattr(config, "LANGSMITH_PROJECT", "proj-x")
    assert observability.configure_tracing() is True
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "ls-secret"
    assert os.environ["LANGCHAIN_PROJECT"] == "proj-x"


def test_switched_on_without_key_stays_off(monkeypatch):
    # The foot-gun: flag on, key missing. Must NOT enable tracing.
    monkeypatch.setattr(config, "LANGSMITH_TRACING", True)
    monkeypatch.setattr(config, "LANGSMITH_API_KEY", "")
    assert observability.configure_tracing() is False
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_run_config_tags_task_node_attempt():
    state = {"task_id": "abc123", "retries": {"r_agent": 2}}
    rc = observability.run_config(state, "r_agent")
    assert rc["run_name"] == "r_agent"
    assert "r_agent" in rc["tags"]
    assert "task:abc123" in rc["tags"]
    assert "attempt:2" in rc["tags"]
    assert rc["metadata"]["task_id"] == "abc123"
    assert rc["metadata"]["node"] == "r_agent"
    assert rc["metadata"]["attempt"] == 2


def test_run_config_defaults_when_state_sparse():
    # First attempt of a node (no retries recorded yet) → attempt 0.
    rc = observability.run_config({"task_id": "t1"}, "sql_agent")
    assert rc["metadata"]["attempt"] == 0
    assert "attempt:0" in rc["tags"]
