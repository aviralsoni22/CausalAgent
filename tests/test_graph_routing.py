"""Unit tests for the orchestrator's conditional routers.

The routers in ``app.core.graph`` decide where the graph goes after each node
runs. They are pure functions of the state, so the whole retry/fallback state
machine — the system's headline orchestration feature — is testable here with
no LLM, database, or sandbox. Importing the module compiles the graph but never
invokes a node, so this stays infrastructure-free.

What each case pins down:
- happy path advances to the next stage;
- a failure with budget remaining re-enters the right recovery node;
- a failure that exhausts the node's own budget routes to ``fallback``;
- the executor's transient-vs-script branch picks executor vs r_agent.
"""
from __future__ import annotations

from langgraph.graph import END

from app.core import config
from app.core.graph import (
    _after_evaluator,
    _after_executor,
    _after_r,
    _after_reviewer,
    _after_sql,
)

_MAX = config.MAX_RETRIES


def _state(status: str, **retries: int) -> dict:
    """Minimal state carrying just what the routers read."""
    return {"current_status": status, "retries": dict(retries)}


# --- happy path: each node advances to the next stage ----------------------

def test_happy_path_transitions():
    assert _after_sql(_state("sql_done")) == "r_agent"
    assert _after_r(_state("r_generated")) == "executor"
    assert _after_executor(_state("executed")) == "evaluator"
    assert _after_evaluator(_state("evaluated")) == "reviewer"
    assert _after_reviewer(_state("completed")) == END


# --- failure with budget remaining: retry the right node -------------------

def test_failure_with_budget_retries_recovery_node():
    # SQL failures retry the SQL agent.
    assert _after_sql(_state("sql_failed", sql_agent=1)) == "sql_agent"
    # A bad R script regenerates R.
    assert _after_r(_state("r_failed", r_agent=1)) == "r_agent"
    # Unparseable R output is a script problem -> regenerate R, not re-evaluate.
    assert _after_evaluator(_state("eval_failed", evaluator=1)) == "r_agent"
    # The reviewer just retries itself.
    assert _after_reviewer(_state("review_failed", reviewer=1)) == "reviewer"


# --- exhaustion: route to the terminal fallback ----------------------------

def test_exhaustion_routes_to_fallback():
    assert _after_sql(_state("sql_failed", sql_agent=_MAX)) == "fallback"
    assert _after_r(_state("r_failed", r_agent=_MAX)) == "fallback"
    assert _after_evaluator(_state("eval_failed", evaluator=_MAX)) == "fallback"
    assert _after_reviewer(_state("review_failed", reviewer=_MAX)) == "fallback"
    # Executor exhaustion wins regardless of which failure kind it was.
    assert _after_executor(_state("exec_failed_transient", executor=_MAX)) == "fallback"
    assert _after_executor(_state("exec_failed_script", executor=_MAX)) == "fallback"


# --- executor recovery depends on the *kind* of failure --------------------

def test_executor_transient_failure_retries_executor():
    # Unreachable/timeout/5xx sandbox: regenerating a fine script can't help, so
    # retry the call itself.
    assert _after_executor(_state("exec_failed_transient", executor=1)) == "executor"


def test_executor_script_failure_regenerates_r():
    # Non-zero Rscript exit means the script is bad: go back to the R agent.
    assert _after_executor(_state("exec_failed_script", executor=1)) == "r_agent"


# --- per-node budgets are independent --------------------------------------

def test_budgets_are_per_node():
    # The SQL agent having exhausted its budget must not strand a later,
    # first-time R failure into fallback: each node carries its own count.
    state = _state("r_failed", sql_agent=_MAX, r_agent=1)
    assert _after_r(state) == "r_agent"


# --- a missing retries map is treated as zero failures ---------------------

def test_missing_retries_map_is_safe():
    assert _after_sql({"current_status": "sql_failed"}) == "sql_agent"
