"""Run provenance persistence.

Celery's result backend is ephemeral, so on its own the platform keeps no
durable record of what it computed. For an enterprise analytics tool the audit
trail matters: which question produced which SQL, which R script, and which
number. This module writes one row per run to ``analysis_runs`` so every result
is reproducible and traceable.

Persistence is best-effort: a failure here must never lose the actual result,
so the caller wraps it and swallows errors.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from app.core.db import get_engine
from app.core.state import CausalGraphState

_DDL = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    task_id            TEXT PRIMARY KEY,
    user_query         TEXT,
    analysis_spec      JSONB,
    sql_query          TEXT,
    r_script           TEXT,
    statistical_output JSONB,
    method             TEXT,
    business_narrative TEXT,
    current_status     TEXT,
    errors             JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Idempotent so a table created before analysis_spec existed gets the column.
_MIGRATE = "ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS analysis_spec JSONB"

_UPSERT = """
INSERT INTO analysis_runs (
    task_id, user_query, analysis_spec, sql_query, r_script, statistical_output,
    method, business_narrative, current_status, errors
) VALUES (
    :task_id, :user_query, CAST(:analysis_spec AS JSONB), :sql_query, :r_script,
    CAST(:statistical_output AS JSONB),
    :method, :business_narrative, :current_status, CAST(:errors AS JSONB)
)
ON CONFLICT (task_id) DO UPDATE SET
    user_query         = EXCLUDED.user_query,
    analysis_spec      = EXCLUDED.analysis_spec,
    sql_query          = EXCLUDED.sql_query,
    r_script           = EXCLUDED.r_script,
    statistical_output = EXCLUDED.statistical_output,
    method             = EXCLUDED.method,
    business_narrative = EXCLUDED.business_narrative,
    current_status     = EXCLUDED.current_status,
    errors             = EXCLUDED.errors,
    created_at         = now()
"""


def save_run(state: CausalGraphState) -> None:
    """Upsert the final state of a run. Raises on failure (caller decides)."""
    stats = state.get("statistical_output") or {}
    params = {
        "task_id": state["task_id"],
        "user_query": state.get("user_query"),
        "analysis_spec": json.dumps(state.get("analysis_spec")),
        "sql_query": state.get("sql_query"),
        "r_script": state.get("r_script"),
        "statistical_output": json.dumps(stats),
        "method": stats.get("method"),
        "business_narrative": state.get("business_narrative"),
        "current_status": state.get("current_status"),
        "errors": json.dumps(state.get("errors") or []),
    }
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(_DDL))
        conn.execute(text(_MIGRATE))
        conn.execute(text(_UPSERT), params)
