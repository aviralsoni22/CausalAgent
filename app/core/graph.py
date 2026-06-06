"""LangGraph orchestration.

Wires the five agent nodes into a directed graph over ``CausalGraphState``:

    sql_agent -> r_agent -> executor -> evaluator -> reviewer -> END

Every node can fail. On failure a node records the error and increments its OWN
counter in ``retries`` (each node has an independent ``MAX_RETRIES`` budget).
The conditional edges then either re-enter the pipeline at a sensible recovery
point or, once the failing node has exhausted its budget, route to a terminal
``fallback`` node so the task always ends cleanly.

Recovery target depends on the failure, not just the stage. A bad R script
(generation, a non-zero ``Rscript`` exit, or unparseable output) routes back to
``r_agent``. But a *transient* executor failure — the sandbox being unreachable,
timing out, or returning a 5xx — retries the executor itself, because
regenerating a probably-fine script cannot fix unreachable infrastructure.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, END

from app.agents.evaluator import evaluator_node
from app.agents.executor import executor_node
from app.agents.r_agent import r_agent_node
from app.agents.reviewer import reviewer_node
from app.agents.sql_agent import sql_agent_node
from app.core import config
from app.core.state import CausalGraphState


# Where a run gave up, mapped to a next action the user can actually take. Keyed
# by the failure status the failing node set before routing here, so a dead-end
# becomes "here's what to try" instead of a traceback dump.
_FAILURE_GUIDANCE = {
    "sql_failed": (
        "I couldn't translate your question into a valid query over the available "
        "data (customers, orders, marketing exposures). Try naming the outcome "
        "metric and the treatment/intervention explicitly — e.g. 'did receiving a "
        "discount raise order total?'"
    ),
    "r_failed": (
        "I extracted the data but couldn't fit a valid model to it. Causal "
        "estimation needs a yes/no treatment and a numeric outcome — try "
        "rephrasing so the intervention is binary and the outcome is a number."
    ),
    "exec_failed_script": (
        "The model couldn't run on the extracted data. Try rephrasing so the "
        "treatment is yes/no and the outcome is numeric; if it persists the data "
        "for this question may be too sparse to model."
    ),
    "exec_failed_transient": (
        "The analysis service was temporarily unavailable. Please retry in a "
        "moment — your question was understood, the run just couldn't complete."
    ),
    "eval_failed": (
        "The model ran but returned a result I couldn't read — usually transient. "
        "Please retry; if it persists, try rephrasing the question."
    ),
    "review_failed": (
        "The analysis completed but I couldn't write the plain-language summary. "
        "The statistical result is available in the output."
    ),
    "fatal_llm_error": (
        "The analysis service is temporarily misconfigured or unavailable (an "
        "authentication or request error reaching the model). This is not a "
        "problem with your question — please contact the operator or try again "
        "later."
    ),
}

_FAILURE_GUIDANCE_DEFAULT = (
    "The analysis could not be completed. Try rephrasing the question so the "
    "intervention is yes/no and the outcome is a number."
)


def _fallback_node(state: CausalGraphState) -> dict:
    """Terminal node reached when retries are exhausted.

    Translate the failing stage into a concrete next action rather than dumping
    attempt counts and tracebacks on the user; ``errors`` still holds the detail.
    """
    guidance = _FAILURE_GUIDANCE.get(state["current_status"], _FAILURE_GUIDANCE_DEFAULT)
    return {
        "current_status": "failed",
        "business_narrative": (
            f"{guidance} (Failed after {state['retry_count']} attempt(s); "
            "see `errors` for technical detail.)"
        ),
    }


def _exhausted(state: CausalGraphState, node: str) -> bool:
    """True once ``node`` has used up its own retry budget."""
    return (state.get("retries") or {}).get(node, 0) >= config.MAX_RETRIES


# --- Conditional routers ---------------------------------------------------
# Each returns the name of the next node based on the just-run node's status.
# Recovery target differs by failure: SQL failures retry SQL; a bad R script
# (generation, non-zero Rscript exit, or unparseable output) retries the R
# agent; a transient executor/sandbox failure retries the executor itself.
# Exhaustion is checked against the failing node's OWN budget.

def _after_sql(state: CausalGraphState) -> str:
    if state["current_status"] == "sql_done":
        return "r_agent"
    return "fallback" if _exhausted(state, "sql_agent") else "sql_agent"


def _after_r(state: CausalGraphState) -> str:
    if state["current_status"] == "r_generated":
        return "executor"
    return "fallback" if _exhausted(state, "r_agent") else "r_agent"


def _after_executor(state: CausalGraphState) -> str:
    if state["current_status"] == "executed":
        return "evaluator"
    # The executor's own budget bounds the loop regardless of recovery target.
    if _exhausted(state, "executor"):
        return "fallback"
    # Transient infra failure: retry the call. Script failure: regenerate R.
    if state["current_status"] == "exec_failed_transient":
        return "executor"
    return "r_agent"


def _after_evaluator(state: CausalGraphState) -> str:
    if state["current_status"] == "evaluated":
        return "reviewer"
    # Unparseable R output is a script problem → regenerate R.
    return "fallback" if _exhausted(state, "evaluator") else "r_agent"


def _after_reviewer(state: CausalGraphState) -> str:
    if state["current_status"] == "completed":
        return END
    return "fallback" if _exhausted(state, "reviewer") else "reviewer"


def build_graph():
    """Construct and compile the orchestrator graph."""
    builder = StateGraph(CausalGraphState)

    builder.add_node("sql_agent", sql_agent_node)
    builder.add_node("r_agent", r_agent_node)
    builder.add_node("executor", executor_node)
    builder.add_node("evaluator", evaluator_node)
    builder.add_node("reviewer", reviewer_node)
    builder.add_node("fallback", _fallback_node)

    builder.set_entry_point("sql_agent")

    builder.add_conditional_edges(
        "sql_agent", _after_sql, {"r_agent": "r_agent", "sql_agent": "sql_agent", "fallback": "fallback"}
    )
    builder.add_conditional_edges(
        "r_agent", _after_r, {"executor": "executor", "r_agent": "r_agent", "fallback": "fallback"}
    )
    builder.add_conditional_edges(
        "executor",
        _after_executor,
        {"evaluator": "evaluator", "executor": "executor", "r_agent": "r_agent", "fallback": "fallback"},
    )
    builder.add_conditional_edges(
        "evaluator", _after_evaluator, {"reviewer": "reviewer", "r_agent": "r_agent", "fallback": "fallback"}
    )
    builder.add_conditional_edges(
        "reviewer", _after_reviewer, {END: END, "reviewer": "reviewer", "fallback": "fallback"}
    )

    builder.add_edge("fallback", END)

    return builder.compile()


# Compiled once at import time and reused by the Celery worker.
compiled_graph = build_graph()


def initial_state(
    task_id: str,
    user_query: str,
    analysis_spec: dict | None = None,
    window: dict | None = None,
) -> CausalGraphState:
    """Build a fresh state for a new task.

    ``analysis_spec`` lets the caller declare the causal identification
    (treatment/outcome/confounders) up front; when None the SQL agent proposes
    it. Either way it becomes an explicit, auditable field in the state.

    ``window`` optionally restricts the analysis to orders with order_id in
    (lo, hi] — used by the event-driven layer to analyse one tumbling batch.
    """
    return CausalGraphState(
        task_id=task_id,
        user_query=user_query,
        analysis_spec=analysis_spec,
        window=window,
        sql_query=None,
        data_file_path=None,
        extracted_columns=None,
        r_script=None,
        statistical_output=None,
        business_narrative=None,
        interpretation=None,
        errors=[],
        retry_count=0,
        retries={},
        current_status="pending",
    )
