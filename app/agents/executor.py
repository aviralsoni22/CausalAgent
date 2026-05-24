"""Execution node.

The only bridge between the orchestrator and the isolated R sandbox. It POSTs
the generated R script and the CSV *content* to the sandbox's ``/execute``
endpoint over HTTP (Architecture Rule 3 — no local exec/eval, R runs only in the
container) and stashes the raw stdout for the evaluator to parse.

The CSV is sent inline rather than by path: the worker and the sandbox do not
share a filesystem when deployed separately (e.g. worker and sandbox on
different hosts), so the data travels in the request body. This is plain Python,
not the LLM, so Rule 2 (rows never enter LLM context) is unaffected.
"""
from __future__ import annotations

import base64
from pathlib import Path

import requests

from app.agents.feedback import record_failure
from app.core import config
from app.core.state import CausalGraphState

# Reused across tasks in a worker process so we get HTTP keep-alive instead of a
# fresh TCP/TLS handshake to the sandbox on every execution.
_session = requests.Session()


def executor_node(state: CausalGraphState) -> dict:
    # Read and encode the extracted CSV. A failure here (missing/unreadable
    # file) is not a script problem, but regenerating R cannot fix it either, so
    # treat it as transient and let the executor's bounded budget end the run.
    try:
        csv_path = Path(state["data_file_path"])
        data_content_b64 = base64.b64encode(csv_path.read_bytes()).decode("ascii")
    except Exception as exc:
        return record_failure(
            state, "executor", "exec_failed_transient",
            error_detail=f"could not read extracted CSV: {exc!r}",
        )

    payload = {
        "r_script": state["r_script"],
        "data_filename": csv_path.name,
        "data_content_b64": data_content_b64,
    }

    # Transport-level failure (sandbox unreachable, timeout, 5xx, non-JSON body)
    # is infrastructure, not the script — retry the call, do not regenerate R.
    try:
        resp = _session.post(
            f"{config.R_SANDBOX_URL}/execute",
            json=payload,
            timeout=config.R_SANDBOX_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return record_failure(
            state, "executor", "exec_failed_transient",
            error_detail=f"sandbox call failed: {exc!r}",
        )

    # The sandbox ran Rscript and it exited non-zero: a bad script. Route back
    # to the R agent, surfacing stderr so the retry hint can correct it.
    if not body.get("success"):
        detail = (
            f"R sandbox returned non-zero (returncode={body.get('returncode')}).\n"
            f"stderr:\n{body.get('stderr', '')}"
        )
        return record_failure(state, "executor", "exec_failed_script", error_detail=detail)

    # Hand the raw stdout to the evaluator; it owns JSON parsing.
    return {
        "statistical_output": {
            "raw_stdout": body.get("stdout", ""),
            "stderr": body.get("stderr", ""),
        },
        "current_status": "executed",
    }
