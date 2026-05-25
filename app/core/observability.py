"""Observability (Phase 4): LangSmith LLM tracing.

One module owns the whole tracing story so the rest of the codebase stays clean:

- ``configure_tracing()`` translates our single env-gated switch
  (``config.tracing_enabled()``) into the native ``LANGCHAIN_*`` environment
  variables that LangChain's runtime reads. It is called once at each process
  entry point (the Celery worker, the CLI runner). When the gate is off it
  *explicitly* forces tracing off, so a stray ambient ``LANGCHAIN_TRACING_V2``
  in the environment can never silently start shipping prompts off-box.

- ``run_config(state, node)`` builds the per-invocation LangChain run config that
  tags every LLM call with its ``task_id``, the node that issued it, and the
  retry attempt number. That tagging is the point of this whole phase: it makes
  the otherwise-opaque self-correcting retry loop (``feedback.py``) visible —
  you can see in LangSmith whether attempt #2 actually differed from attempt #1.
  The config is harmless when tracing is off (LangChain simply ignores it), so
  callers pass it unconditionally and never branch on whether tracing is active.
"""
from __future__ import annotations

import logging
import os

from app.core import config
from app.core.state import CausalGraphState

logger = logging.getLogger(__name__)

_configured = False


def configure_tracing() -> bool:
    """Wire (or explicitly disable) LangSmith tracing for this process.

    Idempotent: safe to call from multiple entry points. Returns whether tracing
    ended up active, mostly so callers can log it.
    """
    global _configured

    if config.tracing_enabled():
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = config.LANGSMITH_API_KEY
        os.environ["LANGCHAIN_PROJECT"] = config.LANGSMITH_PROJECT
        os.environ["LANGCHAIN_ENDPOINT"] = config.LANGSMITH_ENDPOINT
        if not _configured:
            logger.info(
                "LangSmith tracing ENABLED (project=%s, endpoint=%s)",
                config.LANGSMITH_PROJECT,
                config.LANGSMITH_ENDPOINT,
            )
        _configured = True
        return True

    # Gate is off: force tracing off rather than merely leaving it unset, so an
    # ambient LANGCHAIN_TRACING_V2 from the surrounding environment cannot leak
    # prompts to a hosted store without an explicit opt-in here.
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    if not _configured and config.LANGSMITH_TRACING and not config.LANGSMITH_API_KEY:
        # A common foot-gun: switched on but no key. Say so loudly once.
        logger.warning(
            "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is empty; tracing stays OFF."
        )
    _configured = True
    return False


def run_config(state: CausalGraphState, node: str) -> dict:
    """LangChain run config tagging this LLM call with task / node / attempt.

    ``attempt`` is the count of prior failures of this node (from the per-node
    retry budget), i.e. the 0-based index of the current try — so a value of 1
    means "this is the first self-correcting retry."
    """
    task_id = state.get("task_id", "unknown")
    attempt = (state.get("retries") or {}).get(node, 0)
    return {
        "run_name": f"{node}",
        "tags": [node, f"task:{task_id}", f"attempt:{attempt}"],
        "metadata": {
            "task_id": task_id,
            "node": node,
            "attempt": attempt,
            "model": config.ANTHROPIC_MODEL,
        },
    }
