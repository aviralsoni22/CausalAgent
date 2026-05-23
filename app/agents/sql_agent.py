"""SQL Agent node.

Translates the user's natural-language question into a single read-only SELECT
against the e-commerce schema, executes it via SQLAlchemy, and persists the
result to ``data/{task_id}.csv``. Per Architecture Rule 2 the rows themselves
never enter the graph state or the LLM context — only the file path and the
column names move forward.
"""
from __future__ import annotations

import re
import traceback
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from app.agents.feedback import retry_hint
from app.core import config
from app.core.db import get_engine
from app.core.llm import get_llm
from app.core.schema_def import SCHEMA_PROMPT
from app.core.state import CausalGraphState
from app.models.schemas import AnalysisSpec, SQLGeneration

_SYSTEM_PROMPT = """You are a senior analytics engineer. Given a database schema \
and a business question, write ONE read-only PostgreSQL SELECT statement and an \
explicit causal identification spec.

Rules:
- Output a SINGLE SELECT statement (a leading WITH/CTE is allowed). No INSERT, \
UPDATE, DELETE, DDL, or multiple statements.
- Produce a flat, model-ready result: one row per observational unit, with the \
treatment column, the outcome column, and the confounder columns as plain \
columns. Use clear column aliases.
- Return a `spec` naming the treatment (binary 0/1), the numeric outcome, and \
the confounders to adjust for. EVERY column named in the spec MUST be projected \
by your SELECT (matching alias).
- Do not invent tables or columns that are not in the schema.

Schema:
{schema}
"""

# Used when the caller has already declared the identification; the LLM only has
# to write a SELECT that projects exactly those columns.
_SYSTEM_PROMPT_FIXED_SPEC = """You are a senior analytics engineer. Write ONE \
read-only PostgreSQL SELECT (a leading WITH/CTE is allowed; no other statement \
types) that projects EXACTLY these columns (use these names as aliases):
treatment="{treatment}", outcome="{outcome}", confounders={confounders}.
Do not invent tables or columns outside the schema.

Schema:
{schema}
"""

# Statements we refuse to execute even if the LLM produces them. Defence in
# depth: the prompt forbids them, but the query is still executed against a live
# database, so we validate before running.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|"
    r"merge|comment|vacuum|copy)\b",
    re.IGNORECASE,
)


def _validate_select(sql: str) -> str:
    """Raise if ``sql`` is not a single, read-only SELECT/CTE statement."""
    cleaned = sql.strip().rstrip(";").strip()
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed.")
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("Only SELECT (or WITH ... SELECT) statements are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("Query contains a forbidden, non-read-only keyword.")
    return cleaned


def _validate_spec(spec: AnalysisSpec, columns: list[str]) -> None:
    """Every role named in the spec must actually be a projected column."""
    available = set(columns)
    named = [spec.treatment, spec.outcome, *spec.confounders]
    missing = [c for c in named if c not in available]
    if missing:
        raise ValueError(
            f"Spec references columns not in the result {missing}; "
            f"available columns are {sorted(available)}."
        )


def sql_agent_node(state: CausalGraphState) -> dict:
    try:
        llm = get_llm()
        # Honour a caller-provided identification if present; otherwise let the
        # LLM propose one. Either way the spec ends up explicit in the state.
        user_spec = state.get("analysis_spec")
        if user_spec:
            spec = AnalysisSpec(**user_spec)
            system = _SYSTEM_PROMPT_FIXED_SPEC.format(
                treatment=spec.treatment,
                outcome=spec.outcome,
                confounders=spec.confounders,
                schema=SCHEMA_PROMPT,
            )
            safe_sql = _validate_select(
                llm.with_structured_output(SQLGeneration)
                .invoke([("system", system), ("human", state["user_query"] + retry_hint(state))])
                .sql_query
            )
        else:
            result: SQLGeneration = llm.with_structured_output(SQLGeneration).invoke(
                [
                    ("system", _SYSTEM_PROMPT.format(schema=SCHEMA_PROMPT)),
                    ("human", state["user_query"] + retry_hint(state)),
                ]
            )
            spec = result.spec
            safe_sql = _validate_select(result.sql_query)

        # Defence in depth: run inside a Postgres READ ONLY connection so the
        # database itself rejects any write that slipped past _validate_select
        # (e.g. a data-modifying CTE). The pooled engine is shared, never
        # recreated per task.
        engine = get_engine()
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            df = pd.read_sql(text(safe_sql), conn)

        if df.empty:
            raise ValueError("Query returned zero rows; cannot run a model on it.")

        columns = list(df.columns)
        _validate_spec(spec, columns)

        data_dir = Path(config.DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        file_path = data_dir / f"{state['task_id']}.csv"
        df.to_csv(file_path, index=False)

        return {
            "sql_query": safe_sql,
            "data_file_path": str(file_path),
            # Trust the real dataframe columns over the LLM's claim.
            "extracted_columns": columns,
            "analysis_spec": spec.model_dump(),
            "current_status": "sql_done",
        }
    except Exception:
        return {
            "errors": state["errors"] + [f"[sql_agent]\n{traceback.format_exc()}"],
            "retry_count": state["retry_count"] + 1,
            "current_status": "sql_failed",
        }
