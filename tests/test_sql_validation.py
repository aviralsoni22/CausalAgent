"""Security regression tests for the SQL agent's query validation.

The SQL agent executes an LLM-generated query against a live database. The user
question (and the error text fed back on retry) is attacker-influenceable, so the
first containment layer — ``_validate_select`` rejecting anything that is not a
single read-only SELECT — must hold. These are pure-function tests over the
validator and the deterministic window wrapper; no LLM or DB involved.

Note this is only layer 1 of three: the SQL agent also connects as a SELECT-only
role and sets the session READ ONLY (see sql_agent / db.get_readonly_engine), so
anything that slips past this regex still cannot write or read other tables.
"""
from __future__ import annotations

import pytest

from app.agents.sql_agent import _apply_window, _validate_select


def test_accepts_plain_select():
    sql = "SELECT a, b FROM orders"
    assert _validate_select(sql) == sql


def test_accepts_leading_cte():
    sql = "WITH t AS (SELECT 1 AS x) SELECT x FROM t"
    assert _validate_select(sql) == sql


def test_strips_trailing_semicolon_and_whitespace():
    assert _validate_select("  SELECT 1 ;  ") == "SELECT 1"


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM orders",
        "UPDATE orders SET total_amount = 0",
        "INSERT INTO orders VALUES (1)",
        "TRUNCATE orders",
        "ALTER TABLE orders ADD COLUMN x int",
        "GRANT SELECT ON orders TO public",
        "COPY orders TO '/tmp/leak.csv'",
    ],
)
def test_rejects_non_read_only_statements(sql):
    with pytest.raises(ValueError):
        _validate_select(sql)


def test_rejects_stacked_statements():
    # The classic injection: a benign SELECT followed by a destructive one.
    with pytest.raises(ValueError):
        _validate_select("SELECT 1; DROP TABLE customers")


def test_rejects_data_modifying_cte():
    # A CTE can smuggle a write past a naive "starts with WITH" check.
    with pytest.raises(ValueError):
        _validate_select(
            "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d"
        )


def test_window_wrapper_uses_bound_params_not_interpolation():
    # The order window must never be string-formatted into the SQL — it is bound,
    # and coerced to int, so a hostile window value cannot inject.
    wrapped, params = _apply_window("SELECT order_id FROM orders", {"lo": "5", "hi": "9"})
    assert ":win_lo" in wrapped and ":win_hi" in wrapped
    assert params == {"win_lo": 5, "win_hi": 9}
    assert "5" not in wrapped and "9" not in wrapped
