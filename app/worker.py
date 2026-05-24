"""Celery worker: the async execution layer.

The FastAPI ingress generates a ``task_id`` and enqueues ``run_causal_analysis``
onto Redis. This worker picks it up and drives the compiled LangGraph
orchestrator to completion, then returns the final state as the task result.
"""
from __future__ import annotations

import logging

from celery import Celery

from app.core import config
from app.core.graph import compiled_graph, initial_state
from app.core.persistence import save_run

logger = logging.getLogger(__name__)

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
    # Recursion limit comfortably covers the bounded retry loops.
    final_state = compiled_graph.invoke(state, config={"recursion_limit": 50})

    # Best-effort audit trail: never let a persistence problem lose the result.
    try:
        save_run(final_state)
    except Exception:
        logger.exception("Failed to persist provenance for task %s", task_id)

    return dict(final_state)
