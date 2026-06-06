"""Tests for the curated client-facing result.

The task must not hand the full internal state to callers (or the Redis result
backend) — the generated SQL/R, the CSV path, the column list, and retry
bookkeeping stay server-side in the audit table. These pin what is and isn't
exposed, so a field can't quietly leak into the API response later.
"""
from __future__ import annotations

from app.worker import public_result

_FULL_STATE = {
    "task_id": "t1",
    "user_query": "did discounts raise spend?",
    "analysis_spec": {"treatment": "received_discount", "outcome": "total_amount",
                      "confounders": ["age"]},
    "interpretation": "Measured the effect of 'received_discount' on 'total_amount'.",
    "statistical_output": {"ate": 14.0, "p_value": 0.001, "is_significant": True},
    "business_narrative": "Discounts raised order value by about $14.",
    "current_status": "completed",
    # Internal fields that must NOT be exposed:
    "sql_query": "SELECT ...",
    "r_script": "library(MatchIt) ...",
    "extracted_columns": ["received_discount", "total_amount", "age"],
    "data_file_path": "data/t1.csv",
    "retries": {"r_agent": 1},
    "retry_count": 1,
    "window": {"lo": 0, "hi": 500},
    "errors": [],
}

_INTERNAL_ONLY = {
    "sql_query", "r_script", "extracted_columns", "data_file_path",
    "retries", "retry_count", "window", "user_query",
}


def test_exposes_public_fields():
    out = public_result(_FULL_STATE)
    for field in ("task_id", "current_status", "analysis_spec", "interpretation",
                  "statistical_output", "business_narrative"):
        assert field in out
    assert out["business_narrative"] == "Discounts raised order value by about $14."


def test_excludes_internal_fields():
    out = public_result(_FULL_STATE)
    leaked = _INTERNAL_ONLY & out.keys()
    assert not leaked, f"internal fields leaked into the result: {leaked}"


def test_errors_included_only_when_present():
    assert "errors" not in public_result(_FULL_STATE)  # empty list -> omitted
    failed = {**_FULL_STATE, "errors": ["[r_agent]\nboom"]}
    assert public_result(failed)["errors"] == ["[r_agent]\nboom"]
