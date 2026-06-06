"""Tests for permanent-vs-transient LLM error handling.

A permanent failure (bad key, malformed request, policy refusal) can't be fixed
by regenerating, so the node is exhausted immediately and the run fails fast to
the terminal fallback. A transient failure (429/5xx/timeout) stays retryable —
the SDK already backs off and a fresh regenerate is fine. These pin that split so
a regression can't quietly turn a doomed call into a full retry-budget burn.
"""
from __future__ import annotations

from app.agents.feedback import _is_permanent_llm_error, record_failure
from app.core import config
from app.core.graph import _fallback_node


class _ApiError(Exception):
    def __init__(self, status_code=None, request_id=None):
        super().__init__("api error")
        self.status_code = status_code
        self.request_id = request_id


def test_classifies_permanent_vs_transient():
    assert _is_permanent_llm_error(_ApiError(401))
    assert _is_permanent_llm_error(_ApiError(400))
    assert _is_permanent_llm_error(_ApiError(403))
    assert not _is_permanent_llm_error(_ApiError(429))   # rate limit -> transient
    assert not _is_permanent_llm_error(_ApiError(500))   # server error -> transient
    assert not _is_permanent_llm_error(ValueError("parse"))  # not an API error


def test_classifies_through_a_wrapping_exception():
    # langchain may wrap the SDK error; the chain must still be inspected.
    try:
        try:
            raise _ApiError(401)
        except Exception as inner:
            raise RuntimeError("wrapped by langchain") from inner
    except Exception as exc:
        assert _is_permanent_llm_error(exc)


def _record_during(exc, node="sql_agent", status="sql_failed"):
    base = {"errors": [], "retry_count": 0, "retries": {}}
    try:
        raise exc
    except Exception:
        return record_failure(base, node, status)


def test_permanent_error_fails_fast():
    out = _record_during(_ApiError(401, request_id="req_123"))
    # Node exhausted immediately so the router goes straight to fallback...
    assert out["retries"]["sql_agent"] >= config.MAX_RETRIES
    # ...under a distinct terminal status (not the user-facing "rephrase" path).
    assert out["current_status"] == "fatal_llm_error"


def test_transient_error_stays_retryable():
    out = _record_during(_ApiError(429))
    assert out["retries"]["sql_agent"] == 1          # one strike, not exhausted
    assert out["current_status"] == "sql_failed"     # status unchanged


def test_explicit_error_detail_is_never_classified():
    # Sandbox failures pass error_detail; with no live exception they must keep
    # the caller's status and a normal single increment.
    out = record_failure(
        {"errors": [], "retry_count": 0, "retries": {}},
        "executor", "exec_failed_script", error_detail="stderr: boom",
    )
    assert out["current_status"] == "exec_failed_script"
    assert out["retries"]["executor"] == 1


def test_fatal_llm_error_has_operator_facing_guidance():
    out = _fallback_node({"current_status": "fatal_llm_error", "retry_count": 1})
    assert out["current_status"] == "failed"
    assert "misconfigured or unavailable" in out["business_narrative"]
    assert "not a problem with your question" in out["business_narrative"]
