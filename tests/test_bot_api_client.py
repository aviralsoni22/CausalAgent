"""Tests for the chat-adapter client logic — no Discord or network required."""
from __future__ import annotations

from app.bots import api_client
from app.sim import effects


def test_pinned_spec_for_known_treatment_uses_true_confounder():
    spec = api_client.pinned_spec("received_discount")
    assert spec == {
        "treatment": "received_discount",
        "outcome": "total_amount",
        "confounders": [effects.CONFOUNDER["received_discount"]],
    }


def test_pinned_spec_is_none_for_freeform_or_unknown():
    assert api_client.pinned_spec(None) is None
    assert api_client.pinned_spec("not_a_treatment") is None


def test_is_terminal():
    assert api_client.is_terminal({"state": "SUCCESS"}) is True
    assert api_client.is_terminal({"state": "FAILURE"}) is True
    assert api_client.is_terminal({"state": "PROGRESS"}) is False
    assert api_client.is_terminal({"state": "PENDING"}) is False


def test_stage_label_prefers_worker_stage_then_falls_back_to_state():
    assert api_client.stage_label({"state": "PROGRESS", "progress": {"stage": "model run"}}) == "model run"
    assert api_client.stage_label({"state": "PENDING"}) == "queued"
    assert api_client.stage_label({"state": "PROGRESS"}) == "working"


def test_summarize_result_builds_estimate_line_and_passes_text():
    result = {
        "business_narrative": "Discounts raised order value.",
        "interpretation": "Effect of received_discount on total_amount, adjusting for age.",
        "statistical_output": {
            "ate": 13.8,
            "p_value": 0.001,
            "method": "MatchIt PSM",
            "n_used": 1840,
            "max_smd": 0.04,
        },
    }
    s = api_client.summarize_result(result)
    assert s["narrative"].startswith("Discounts")
    assert "received_discount" in s["interpretation"]
    assert "ATE = 13.8" in s["stat_line"]
    assert "MatchIt PSM" in s["stat_line"] and "n=1840" in s["stat_line"]
    assert s["error"] is None


def test_summarize_result_handles_json_string_and_errors():
    result = {
        "business_narrative": "Fallback message.",
        "statistical_output": '{"ate": 0.2, "method": "lm", "n_used": 10}',
        "errors": ["r_failed: something"],
    }
    s = api_client.summarize_result(result)
    assert "ATE = 0.2" in s["stat_line"]  # parsed from the JSON string
    assert s["error"] is not None  # errors present -> note surfaced
