"""Execution node.

The only bridge between the orchestrator and the isolated R sandbox. It POSTs
the generated R script and the CSV path to the sandbox's ``/execute`` endpoint
over HTTP (Architecture Rule 3 — no local exec/eval, R runs only in the
container) and stashes the raw stdout for the evaluator to parse.
"""
from __future__ import annotations

import traceback

import requests

from app.core import config
from app.core.state import CausalGraphState

# Reused across tasks in a worker process so we get HTTP keep-alive instead of a
# fresh TCP/TLS handshake to the sandbox on every execution.
_session = requests.Session()


def executor_node(state: CausalGraphState) -> dict:
    try:
        payload = {
            "r_script": state["r_script"],
            "data_file_path": state["data_file_path"],
        }
        resp = _session.post(
            f"{config.R_SANDBOX_URL}/execute",
            json=payload,
            timeout=config.R_SANDBOX_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()

        if not body.get("success"):
            raise RuntimeError(
                "R sandbox reported failure "
                f"(returncode={body.get('returncode')}):\n{body.get('stderr', '')}"
            )

        # Hand the raw stdout to the evaluator; it owns JSON parsing.
        return {
            "statistical_output": {
                "raw_stdout": body.get("stdout", ""),
                "stderr": body.get("stderr", ""),
            },
            "current_status": "executed",
        }
    except Exception:
        return {
            "errors": state["errors"] + [f"[executor]\n{traceback.format_exc()}"],
            "retry_count": state["retry_count"] + 1,
            "current_status": "exec_failed",
        }
