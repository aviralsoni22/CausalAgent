"""Shared helper for self-correcting retries.

When a node fails it appends a traceback to ``state['errors']`` and the graph
re-enters an earlier node. With a fixed prompt and ``temperature=0`` that retry
would reproduce the identical failure — so we feed the most recent error back
into the prompt, giving the LLM a concrete chance to fix what broke.
"""
from __future__ import annotations

import logging
import re
import sys
import traceback

from app.core import config
from app.core.state import CausalGraphState

logger = logging.getLogger(__name__)

# HTTP statuses no amount of regenerating will fix: auth, permission, a malformed
# request, content-policy refusal. 429 and 5xx are transient — the Anthropic SDK
# already retries those with backoff — so they are deliberately NOT here.
_PERMANENT_STATUS = {400, 401, 403, 404, 422}

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


def _iter_causes(exc: BaseException | None):
    """Walk the exception's cause/context chain (langchain may wrap the SDK error)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _is_permanent_llm_error(exc: BaseException) -> bool:
    """True for errors regenerating can't fix (auth/permission/bad-request/policy).

    Keys off the HTTP status code on the exception (or anything it wraps), so it
    works whether or not the anthropic exception types are importable.
    """
    return any(getattr(e, "status_code", None) in _PERMANENT_STATUS for e in _iter_causes(exc))


def _request_id(exc: BaseException) -> str | None:
    """First Anthropic request id in the chain, for correlating with provider logs."""
    for e in _iter_causes(exc):
        rid = getattr(e, "request_id", None)
        if rid:
            return rid
    return None


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
    # Only a live exception (error_detail is None) can be classified; sandbox
    # failures pass error_detail explicitly and are always treated as retryable.
    exc = sys.exc_info()[1] if error_detail is None else None
    # Redact here, at the point of capture, so every downstream sink (LLM prompt,
    # /status, audit DB) only ever sees the sanitised text.
    redacted = _redact(f"[{node}]\n{detail}")
    retries = state.get("retries") or {}
    node_count = retries.get(node, 0) + 1

    if exc is not None:
        request_id = _request_id(exc)
        if _is_permanent_llm_error(exc):
            # Regenerating cannot fix a bad key / malformed request / policy
            # refusal — exhaust this node now so the graph routes straight to the
            # terminal fallback instead of burning the whole retry budget (and the
            # quota) on a call that will fail identically every time.
            node_count = config.MAX_RETRIES
            status = "fatal_llm_error"
            logger.error(
                "Permanent LLM error in %s (request_id=%s): %s — failing fast",
                node, request_id, type(exc).__name__,
            )
        else:
            logger.warning(
                "Node %s failed (request_id=%s): %s",
                node, request_id, type(exc).__name__,
            )

    return {
        "errors": state["errors"] + [redacted],
        "retry_count": state.get("retry_count", 0) + 1,
        "retries": {**retries, node: node_count},
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
