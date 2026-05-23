"""Shared helper for self-correcting retries.

When a node fails it appends a traceback to ``state['errors']`` and the graph
re-enters an earlier node. With a fixed prompt and ``temperature=0`` that retry
would reproduce the identical failure — so we feed the most recent error back
into the prompt, giving the LLM a concrete chance to fix what broke.
"""
from __future__ import annotations

from app.core.state import CausalGraphState

# Cap so a giant traceback can't blow the prompt budget.
_MAX_ERROR_CHARS = 2000


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
