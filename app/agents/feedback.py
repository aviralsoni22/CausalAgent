"""Shared helper for self-correcting retries.

When a node fails it appends a traceback to ``state['errors']`` and the graph
re-enters an earlier node. With a fixed prompt and ``temperature=0`` that retry
would reproduce the identical failure — so we feed the most recent error back
into the prompt, giving the LLM a concrete chance to fix what broke.
"""
from __future__ import annotations

import traceback

from app.core.state import CausalGraphState

# Cap so a giant traceback can't blow the prompt budget.
_MAX_ERROR_CHARS = 2000


def record_failure(
    state: CausalGraphState,
    node: str,
    status: str,
    error_detail: str | None = None,
) -> dict:
    """Build the state update for a failed node.

    Appends a labelled error, bumps the total ``retry_count``, and increments
    this node's own entry in ``retries`` so every node has an independent retry
    budget (see ``CausalGraphState.retries``). Pass ``error_detail`` for
    failures that are not live exceptions (e.g. the sandbox reporting a non-zero
    R exit); otherwise the current traceback is captured.
    """
    detail = error_detail if error_detail is not None else traceback.format_exc()
    retries = state.get("retries") or {}
    return {
        "errors": state["errors"] + [f"[{node}]\n{detail}"],
        "retry_count": state.get("retry_count", 0) + 1,
        "retries": {**retries, node: retries.get(node, 0) + 1},
        "current_status": status,
    }


def retry_hint(state: CausalGraphState) -> str:
    """Return a corrective instruction built from the last error, or ''."""
    if state.get("retry_count", 0) <= 0:
        return ""
    errors = state.get("errors") or []
    if not errors:
        return ""
    last = errors[-1][-_MAX_ERROR_CHARS:]
    return (
        "\n\nIMPORTANT: your previous attempt FAILED with the error below. "
        "Diagnose it and produce a corrected version that avoids it:\n"
        f"-----\n{last}\n-----"
    )
