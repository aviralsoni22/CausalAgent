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


# --- MLflow tracking -------------------------------------------------------

def _completed_state() -> dict:
    return {
        "task_id": "run-1",
        "current_status": "completed",
        "retry_count": 1,
        "analysis_spec": {
            "treatment": "got_discount",
            "outcome": "order_total",
            "confounders": ["age", "region"],
        },
        "statistical_output": {
            "ate": 14.2,
            "p_value": 0.001,
            "method": "psm_matchit_lm",
            "n_used": 2900,
            "max_smd": 0.04,
            "is_significant": True,
            "balanced": True,
        },
        "sql_query": "SELECT 1",
        "r_script": "cat('{}')",
        "business_narrative": "Discounts raised spend.",
        "window": None,
    }


def test_log_causal_run_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MLFLOW_TRACKING", False)
    # Must return False without importing/calling mlflow at all.
    assert observability.log_causal_run(_completed_state()) is False


def test_mlflow_payload_completed_run():
    p = observability.mlflow_payload(_completed_state())
    assert p["params"]["treatment"] == "got_discount"
    assert p["params"]["confounders"] == "age, region"
    assert p["params"]["method"] == "psm_matchit_lm"
    assert p["params"]["windowed"] == "False"
    assert p["metrics"]["ate"] == 14.2
    assert p["metrics"]["max_smd"] == 0.04
    assert p["metrics"]["is_significant"] == 1.0
    assert p["metrics"]["retry_count"] == 1.0
    assert p["tags"]["status"] == "completed"
    assert p["tags"]["balanced"] == "True"
    assert set(p["artifacts"]) == {"query.sql", "model.R", "narrative.txt"}


def test_mlflow_payload_drops_none_and_nan():
    state = {
        "task_id": "run-2",
        "current_status": "completed",
        "statistical_output": {
            "ate": 5.0,
            "p_value": float("nan"),  # NaN must be dropped, not logged
            "method": "unadjusted_lm",
            "max_smd": None,           # N/A balance must be dropped
            "is_significant": False,
        },
        "analysis_spec": {"treatment": "t", "outcome": "y", "confounders": []},
    }
    p = observability.mlflow_payload(state)
    assert "p_value" not in p["metrics"]
    assert "max_smd" not in p["metrics"]
    assert p["metrics"]["is_significant"] == 0.0
    assert "confounders" not in p["params"]  # empty list → omitted
    assert "balanced" not in p["tags"]        # not a bool → omitted


def test_mlflow_payload_failed_run_still_logs():
    state = {
        "task_id": "run-3",
        "current_status": "failed",
        "retry_count": 3,
        "errors": ["boom"],
    }
    p = observability.mlflow_payload(state)
    assert p["tags"]["status"] == "failed"
    assert p["metrics"]["retry_count"] == 3.0
    assert p["artifacts"] == {}  # nothing produced
