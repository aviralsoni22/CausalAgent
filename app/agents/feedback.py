"""Shared helper for self-correcting retries.

When a node fails it appends a traceback to ``state['errors']`` and the graph
re-enters an earlier node. With a fixed prompt and ``temperature=0`` that retry
would reproduce the identical failure — so we feed the most recent error back
into the prompt, giving the LLM a concrete chance to fix what broke.
"""
from __future__ import annotations

import re
import traceback

from app.core.state import CausalGraphState

# Cap so a giant traceback can't blow the prompt budget.
_MAX_ERROR_CHARS = 2000

# A failed R run's stderr can echo actual data values (e.g. "unexpected value
# 'jane@example.com' in row 12"). That text fans out to every error sink — the
# next LLM prompt (retry_hint), the /status API response, the analysis_runs audit
# table, and any trace — so redaction happens at CAPTURE (record_failure), not at
# each point of use. Sanitising once at the source means a value can't leak by a
# path we forgot to wrap. Best-effort: masks the value-shaped tokens that most
# commonly leak.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# 4+ digit runs: ids, amounts, phone numbers. Short numbers (R line/row counts,
# return codes) are left alone so the diagnostic stays useful.
_LONGNUM_RE = re.compile(r"\d{4,}")


def _redact(text: str) -> str:
    text = _EMAIL_RE.sub("[redacted-email]", text)
    return _LONGNUM_RE.sub("[redacted-number]", text)


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
    # Redact here, at the point of capture, so every downstream sink (LLM prompt,
    # /status, audit DB) only ever sees the sanitised text.
    redacted = _redact(f"[{node}]\n{detail}")
    retries = state.get("retries") or {}
    return {
        "errors": state["errors"] + [redacted],
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
    # Errors are already redacted at capture (record_failure); _redact here is an
    # idempotent safety net in case an error reaches state by some other path.
    last = _redact(errors[-1][-_MAX_ERROR_CHARS:])
    return (
        "\n\nIMPORTANT: your previous attempt FAILED with the error below. "
        "Diagnose it and produce a corrected version that avoids it:\n"
        f"-----\n{last}\n-----"
    )
