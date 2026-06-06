"""Tests for the two trust-surfacing additions.

build_interpretation turns the identification spec into a plain-language line the
user can check against what they meant; _fallback_node turns an exhausted run
into an actionable next step instead of a traceback. Both are pure functions, so
they're pinned here without an LLM or DB.
"""
from __future__ import annotations

from app.agents.reviewer import build_interpretation
from app.core.graph import _fallback_node


def test_interpretation_with_confounders_and_n():
    out = build_interpretation(
        {"treatment": "received_discount", "outcome": "total_amount",
         "confounders": ["age", "region"]},
        {"n_used": 2940},
    )
    assert "received_discount" in out and "total_amount" in out
    assert "adjusting for age, region" in out
    assert "2940 observations" in out


def test_interpretation_without_confounders_or_n():
    out = build_interpretation(
        {"treatment": "exposed", "outcome": "spend", "confounders": []}, {}
    )
    assert "no confounders adjusted for" in out
    assert "observations" not in out  # n_used absent -> no count claimed


def test_interpretation_tolerates_empty_spec():
    # Never raises on a missing spec; falls back to placeholders.
    out = build_interpretation({}, {})
    assert "the treatment" in out and "the outcome" in out


def test_fallback_maps_each_failure_to_guidance():
    cases = {
        "sql_failed": "couldn't translate your question",
        "r_failed": "couldn't fit a valid model",
        "exec_failed_transient": "temporarily unavailable",
        "review_failed": "statistical result is available",
    }
    for status, expected in cases.items():
        out = _fallback_node({"current_status": status, "retry_count": 3})
        assert out["current_status"] == "failed"
        assert expected in out["business_narrative"]
        assert "3 attempt(s)" in out["business_narrative"]


def test_fallback_unknown_status_uses_default():
    out = _fallback_node({"current_status": "something_new", "retry_count": 1})
    assert out["current_status"] == "failed"
    assert "could not be completed" in out["business_narrative"]
