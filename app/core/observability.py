"""Observability (Phase 4): LangSmith LLM tracing + MLflow run tracking.

One module owns the whole observability story so the rest of the codebase stays
clean. LangSmith captures the LLM *calls*; MLflow records each finished causal
analysis as a comparable *experiment run* (``log_causal_run`` / ``mlflow_payload``,
see ADR-004). Both are env-gated and off by default.

The tracing half:

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
from pathlib import Path
from urllib.parse import urlparse

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


def resolve_tracking_uri(uri: str) -> str:
    """Normalise a local filesystem path into a file:// URI MLflow accepts.

    MLflow rejects a bare local path whose first segment looks like a URI scheme
    — notably a Windows drive letter (``C:\\mlruns`` parses as scheme ``c``). A
    relative or absolute local path (or an explicit ``file:`` URI) is converted
    to an absolute ``file://`` URI; a real remote backend (http(s), postgresql,
    sqlite, databricks, …) is passed through untouched.
    """
    scheme = urlparse(uri).scheme
    if scheme == "file":
        return uri
    # Empty scheme = plain path; a single-letter scheme = Windows drive letter.
    if scheme == "" or len(scheme) == 1:
        return Path(uri).resolve().as_uri()
    return uri


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x  # x==x rejects NaN


def mlflow_payload(state: CausalGraphState) -> dict:
    """Build the MLflow run payload from final state — pure, so it is testable.

    Returns ``{"params", "metrics", "tags", "artifacts"}``. None/NaN values are
    dropped rather than logged as the strings "None"/"nan". Failed runs are still
    loggable (sparse metrics, status tag captures the outcome), because seeing
    failures in the experiment is part of the point.
    """
    spec = state.get("analysis_spec") or {}
    stats = state.get("statistical_output") or {}
    window = state.get("window") or {}

    params: dict[str, str] = {"task_id": state.get("task_id", "unknown")}
    if spec.get("treatment"):
        params["treatment"] = str(spec["treatment"])
    if spec.get("outcome"):
        params["outcome"] = str(spec["outcome"])
    if spec.get("confounders"):
        params["confounders"] = ", ".join(spec["confounders"])
    if stats.get("method"):
        params["method"] = str(stats["method"])
    params["model"] = config.ANTHROPIC_MODEL
    params["windowed"] = str(bool(window))
    if window:
        params["window_lo"] = str(window.get("lo"))
        params["window_hi"] = str(window.get("hi"))

    metrics: dict[str, float] = {}
    for key in ("ate", "p_value", "max_smd", "n_used"):
        if _is_number(stats.get(key)):
            metrics[key] = float(stats[key])
    if isinstance(stats.get("is_significant"), bool):
        metrics["is_significant"] = 1.0 if stats["is_significant"] else 0.0
    if _is_number(state.get("retry_count")):
        metrics["retry_count"] = float(state["retry_count"])

    tags = {
        "task_id": state.get("task_id", "unknown"),
        "status": state.get("current_status", "unknown"),
        "method": str(stats.get("method", "n/a")),
    }
    if isinstance(stats.get("balanced"), bool):
        tags["balanced"] = str(stats["balanced"])

    artifacts: dict[str, str] = {}
    if state.get("sql_query"):
        artifacts["query.sql"] = state["sql_query"]
    if state.get("r_script"):
        artifacts["model.R"] = state["r_script"]
    if state.get("business_narrative"):
        artifacts["narrative.txt"] = state["business_narrative"]

    return {"params": params, "metrics": metrics, "tags": tags, "artifacts": artifacts}


def log_causal_run(state: CausalGraphState) -> bool:
    """Log one finished analysis to MLflow as a comparable run. Best-effort.

    No-op (and no mlflow import) when the gate is off. Returns whether a run was
    logged. The caller wraps this in try/except: a tracking failure must never
    lose the actual result, exactly as with the analysis_runs audit write.
    """
    if not config.mlflow_enabled():
        return False

    import mlflow  # lazy: only paid for when tracking is on

    payload = mlflow_payload(state)
    mlflow.set_tracking_uri(resolve_tracking_uri(config.MLFLOW_TRACKING_URI))
    mlflow.set_experiment(config.MLFLOW_EXPERIMENT)
    with mlflow.start_run(run_name=state.get("task_id")):
        mlflow.set_tags(payload["tags"])
        if payload["params"]:
            mlflow.log_params(payload["params"])
        if payload["metrics"]:
            mlflow.log_metrics(payload["metrics"])
        for filename, content in payload["artifacts"].items():
            mlflow.log_text(content, filename)
    logger.info("Logged MLflow run for task %s", state.get("task_id"))
    return True


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
