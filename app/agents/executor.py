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
import traceback
from pathlib import Path

import requests

from app.core import config
from app.core.state import CausalGraphState

# Reused across tasks in a worker process so we get HTTP keep-alive instead of a
# fresh TCP/TLS handshake to the sandbox on every execution.
_session = requests.Session()


def executor_node(state: CausalGraphState) -> dict:
    try:
        csv_path = Path(state["data_file_path"])
        data_content_b64 = base64.b64encode(csv_path.read_bytes()).decode("ascii")
        payload = {
            "r_script": state["r_script"],
            "data_filename": csv_path.name,
            "data_content_b64": data_content_b64,
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
