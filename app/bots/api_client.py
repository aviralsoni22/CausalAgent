"""HTTP client + formatting for chat adapters.

Kept free of any Discord import so the request/poll/format logic is unit-testable
on its own. The adapter talks only to the ingress (``/analyze`` + ``/status``);
it never imports the worker or the pipeline.
"""
from __future__ import annotations

import json

import requests

from app.core import config
from app.sim import effects


def pinned_spec(treatment: str | None) -> dict | None:
    """Build an explicit identification for a known demo treatment.

    Pinning makes a demo run deterministic — the agent estimates exactly this
    treatment with its true confounder, instead of inferring identification from
    the question. Returns None for free-form questions.
    """
    if not treatment or treatment not in effects.TRUE_EFFECTS:
        return None
    return {
        "treatment": treatment,
        "outcome": "total_amount",
        "confounders": [effects.CONFOUNDER[treatment]],
    }


def submit_analysis(
    query: str, spec: dict | None = None, base_url: str | None = None, timeout: float = 10.0
) -> str:
    base = base_url or config.API_BASE_URL
    payload: dict = {"query": query}
    if spec:
        payload["spec"] = spec
    resp = requests.post(f"{base}/analyze", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["task_id"]


def fetch_status(task_id: str, base_url: str | None = None, timeout: float = 10.0) -> dict:
    base = base_url or config.API_BASE_URL
    resp = requests.get(f"{base}/status/{task_id}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def is_terminal(status: dict) -> bool:
    return status.get("state") in ("SUCCESS", "FAILURE")


def stage_label(status: dict) -> str:
    """A human label for the current poll state, preferring the worker's stage."""
    progress = status.get("progress")
    if isinstance(progress, dict) and progress.get("stage"):
        return progress["stage"]
    return {"PENDING": "queued", "STARTED": "working", "PROGRESS": "working"}.get(
        status.get("state", ""), "working"
    )


def _coerce_stat(stat) -> dict:
    """statistical_output may arrive as a dict or a JSON string; normalize it."""
    if isinstance(stat, str):
        try:
            return json.loads(stat)
        except (ValueError, TypeError):
            return {}
    return stat or {}


def summarize_result(result: dict) -> dict:
    """Project a public_result into adapter-ready, display-safe fields."""
    stat = _coerce_stat(result.get("statistical_output"))
    stat_line = None
    if stat:
        head = f"ATE = {stat['ate']}" if stat.get("ate") is not None else ""
        meta = []
        if stat.get("method"):
            meta.append(str(stat["method"]))
        if stat.get("n_used") is not None:
            meta.append(f"n={stat['n_used']}")
        if stat.get("p_value") is not None:
            meta.append(f"p={stat['p_value']}")
        if stat.get("max_smd") is not None:
            meta.append(f"max SMD={stat['max_smd']}")
        stat_line = (f"{head}  ({', '.join(meta)})" if meta else head).strip() or None

    error = None
    if result.get("errors"):
        error = "This run hit an issue — the summary above may be a fallback message."

    return {
        "narrative": result.get("business_narrative"),
        "interpretation": result.get("interpretation"),
        "stat_line": stat_line,
        "error": error,
    }
