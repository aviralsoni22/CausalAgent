"""Security regression tests for the R sandbox's containment.

The sandbox runs untrusted, LLM-generated R right next to the extracted CSV —
the very data Rule 2 keeps out of the LLM. Credential isolation is not enough;
the process must also be unable to *send* that data anywhere. These tests prove
the egress lockdown holds and that a normal script still runs under the hardened
(read-only rootfs, dropped-caps, non-root) container, so a regression in either
direction is caught.

Requires the sandbox up:
    docker compose up -d --build r_sandbox
"""
from __future__ import annotations

import base64

import pytest
import requests

from app.core import config

# A 1-column CSV; the isolation tests don't model anything, they just need a file.
_CSV_B64 = base64.b64encode(b"x\n1\n").decode("ascii")


@pytest.fixture(scope="module")
def sandbox_up():
    try:
        requests.get(f"{config.R_SANDBOX_URL}/health", timeout=3).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Sandbox not available ({exc}). Run docker compose up -d r_sandbox.")


def _execute(r_script: str) -> dict:
    resp = requests.post(
        f"{config.R_SANDBOX_URL}/execute",
        json={"r_script": r_script, "data_filename": "data.csv", "data_content_b64": _CSV_B64},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def test_sandbox_runs_a_normal_script(sandbox_up):
    """The hardening must not break ordinary execution."""
    body = _execute('cat(sprintf(\'{"ok": %d}\', 1L), "\\n", sep = "")')
    assert body["success"], body
    assert '{"ok": 1}' in body["stdout"], body


def test_sandbox_denies_network_egress(sandbox_up):
    """An R script must not be able to reach the network and exfiltrate data.

    The script tries to open an outbound connection and reports REACHABLE or
    BLOCKED rather than erroring, so we distinguish "egress denied" from any
    unrelated failure. With the OUTPUT-DROP firewall in place the connection
    cannot be established, so the sandbox must report BLOCKED.
    """
    egress_probe = r"""
options(timeout = 5)
reached <- tryCatch({
    con <- url("http://example.com", open = "rb")
    on.exit(close(con), add = TRUE)
    readBin(con, "raw", n = 1)
    TRUE
}, error = function(e) FALSE, warning = function(w) FALSE)
cat(if (reached) "REACHABLE" else "BLOCKED", "\n", sep = "")
"""
    body = _execute(egress_probe)
    assert body["success"], body
    assert "REACHABLE" not in body["stdout"], (
        "R reached the network — egress lockdown is NOT in effect:\n" + body["stdout"]
    )
    assert "BLOCKED" in body["stdout"], body
