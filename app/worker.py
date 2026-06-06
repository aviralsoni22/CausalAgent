"""Celery worker: the async execution layer.

The FastAPI ingress generates a ``task_id`` and enqueues ``run_causal_analysis``
onto Redis. This worker picks it up and drives the compiled LangGraph
orchestrator to completion, then returns the final state as the task result.
"""
from __future__ import annotations

import logging

from celery import Celery

from app.core import config
from app.core.cleanup import purge_extracted_data
from app.core.graph import compiled_graph, initial_state
from app.core.observability import configure_tracing, log_causal_run
from app.core.persistence import save_run

logger = logging.getLogger(__name__)

# Wire LangSmith tracing (or explicitly disable it) before any task runs, so
# every LLM call this worker makes is traced when the env gate is on.
configure_tracing()

celery_app = Celery(
    "causalagent",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    # A causal analysis (LLM + R) can take a while; give it room.
    task_soft_time_limit=600,
    task_time_limit=660,
)


# Internal pipeline statuses → a label a waiting caller can understand, so the
# poll shows "running the model" instead of an opaque Celery STARTED.
_STAGE_LABELS = {
    "pending": "queued",
    "sql_done": "data extracted",
    "r_generated": "model written",
    "executed": "model run",
    "evaluated": "results computed",
    "completed": "finalizing",
}


# The curated result the task returns — what a caller needs to consume the
# answer, nothing more. Internal fields (the generated SQL/R, the CSV path, the
# column list, retry bookkeeping) are deliberately excluded: they don't belong in
# the API response or the Redis result backend. Full provenance is kept in the
# analysis_runs audit table, not handed to clients. ``errors`` is included only
# when present and is already redacted at capture (see feedback.record_failure).
_PUBLIC_RESULT_FIELDS = (
    "task_id",
    "current_status",
    "analysis_spec",       # machine-readable identification (treatment/outcome/...)
    "interpretation",      # the same, in plain language, for human verification
    "statistical_output",
    "business_narrative",
)


def public_result(state: dict) -> dict:
    """Project the final graph state down to the client-facing result."""
    out = {field: state.get(field) for field in _PUBLIC_RESULT_FIELDS}
    if state.get("errors"):
        out["errors"] = state["errors"]
    return out


def progress_meta(status: str, task_id: str) -> dict:
    """Build the Celery PROGRESS meta for one pipeline stage."""
    if status in _STAGE_LABELS:
        label = _STAGE_LABELS[status]
    elif status and ("_failed" in status or status == "failed"):
        # A node failed and the graph is recovering (retrying or giving up).
        label = "retrying"
    else:
        label = "working"
    return {"task_id": task_id, "status": status, "stage": label}


@celery_app.task(name="run_causal_analysis", bind=True)
def run_causal_analysis(
    self,
    task_id: str,
    user_query: str,
    analysis_spec: dict | None = None,
    window: dict | None = None,
) -> dict:
    """Run the full SQL -> R -> evaluate -> review pipeline for one query.

    ``analysis_spec`` optionally pins the causal identification; otherwise the
    SQL agent proposes it. ``window`` optionally restricts the analysis to one
    tumbling batch of orders (order_id in (lo, hi]).
    """
    state = initial_state(
        task_id=task_id,
        user_query=user_query,
        analysis_spec=analysis_spec,
        window=window,
    )
    try:
        # Stream rather than invoke so we can publish each stage as the graph
        # advances; the last value yielded is the final state (identical to what
        # invoke would return). Recursion limit covers the bounded retry loops.
        final_state = None
        for step in compiled_graph.stream(
            state, config={"recursion_limit": 50}, stream_mode="values"
        ):
            final_state = step
            self.update_state(
                state="PROGRESS",
                meta=progress_meta(step.get("current_status", ""), task_id),
            )

        # Best-effort audit trail: never let a persistence problem lose the result.
        try:
            save_run(final_state)
        except Exception:
            logger.exception("Failed to persist provenance for task %s", task_id)

        # Best-effort experiment tracking, for the same reason: a tracking failure
        # must not lose the result. No-op unless MLflow is gated on.
        try:
            log_causal_run(final_state)
        except Exception:
            logger.exception("Failed to log MLflow run for task %s", task_id)

        return public_result(final_state)
    finally:
        # Always delete the extracted rows (customer PII) once the run is over,
        # success or failure — they must not linger on disk. Runs even if the
        # graph raised, since the path is derived from task_id.
        purge_extracted_data(task_id)
