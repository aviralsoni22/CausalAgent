"""Unit tests for the evaluator's parsing and balance semantics.

Pure function over R's stdout — no sandbox, DB, or LLM. Pins the significance
rule and, in particular, the three-way balance state: True/False only when a
matching method actually ran, and N/A (None) when it did not (e.g. a
covariate-adjusted lm), so a non-matched estimate is never mislabelled as
"poorly balanced".
"""
from __future__ import annotations

from app.agents.evaluator import evaluator_node


def _eval(stdout: str) -> dict:
    state = {"statistical_output": {"raw_stdout": stdout}, "errors": [], "retry_count": 0}
    out = evaluator_node(state)
    assert out["current_status"] == "evaluated", out
    return out["statistical_output"]


def test_significance_rule():
    sig = _eval('{"p_value": 0.01, "ate": 5.0, "method": "unadjusted_lm", "n_used": 100}')
    assert sig["is_significant"] is True
    nonsig = _eval('{"p_value": 0.2, "ate": 1.0, "method": "unadjusted_lm", "n_used": 100}')
    assert nonsig["is_significant"] is False
    # Boundary: p == 0.05 counts as significant (<=).
    boundary = _eval('{"p_value": 0.05, "ate": 2.0, "method": "unadjusted_lm", "n_used": 10}')
    assert boundary["is_significant"] is True


def test_balance_true_when_match_balanced():
    out = _eval(
        '{"p_value": 0.0, "ate": 14.0, "method": "psm_matchit_lm", '
        '"n_used": 2900, "max_smd": 0.04}'
    )
    assert out["balanced"] is True
    assert out["max_smd"] == 0.04


def test_balance_false_when_match_imbalanced():
    out = _eval(
        '{"p_value": 0.0, "ate": 14.0, "method": "psm_matchit_lm", '
        '"n_used": 2900, "max_smd": 0.18}'
    )
    assert out["balanced"] is False


def test_balance_is_na_not_false_without_matching():
    # The balance-gate fallback prints max_smd: null. Balance is N/A here, not
    # False — reporting False would imply a non-matched estimate is "unbalanced".
    out = _eval(
        '{"p_value": 0.0, "ate": 13.9, "method": "covariate_adjusted_lm", '
        '"n_used": 3000, "max_smd": null}'
    )
    assert out["balanced"] is None
    assert out["max_smd"] is None


def test_balance_field_absent_when_not_reported():
    # An R script that omits max_smd entirely (e.g. plain covariate lm) yields no
    # balance key at all, distinct from an explicit null.
    out = _eval('{"p_value": 0.0, "ate": 13.9, "method": "covariate_adjusted_lm", "n_used": 3000}')
    assert "balanced" not in out
    assert "max_smd" not in out
