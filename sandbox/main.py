"""R-execution sandbox API.

A deliberately tiny, isolated service. It accepts an R script plus the CSV
content (base64-encoded), materialises both to disk, runs the script with
``Rscript`` in a subprocess, and returns the captured stdout/stderr. It never
imports any orchestrator code, never sees the LLM, and never holds database
credentials — its only job is to run statistics on a file and hand back text.

The data arrives inline in the request body rather than via a shared volume, so
the sandbox can be deployed on a separate host from the orchestrator with no
shared filesystem.
"""
from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/sandbox/workspace"))
# Hard cap so a runaway / adversarial script cannot pin a worker forever.
EXEC_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_EXEC_TIMEOUT", "120"))
# Upper bound on the decoded CSV size. The data arrives inline in the request
# body, so without this an oversized payload could exhaust memory on a small
# container. Tune per deployment (the free-tier sandbox runs with little RAM).
MAX_CSV_BYTES = int(os.environ.get("SANDBOX_MAX_CSV_BYTES", str(25 * 1024 * 1024)))

app = FastAPI(title="CausalAgent R Sandbox", version="1.0.0")


class ExecuteRequest(BaseModel):
    r_script: str = Field(..., description="The R source code to execute.")
    data_filename: str = Field(
        ..., description="Name to give the CSV inside the sandbox workspace."
    )
    data_content_b64: str = Field(
        ..., description="Base64-encoded CSV content the script will read."
    )


class ExecuteResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    returncode: int


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest) -> ExecuteResponse:
    # Reject oversized payloads before decoding so we never allocate the full
    # decoded buffer for a doomed request. Base64 encodes 3 bytes as 4 chars, so
    # the decoded size is ~3/4 of the string length; compare on that estimate.
    estimated_bytes = len(req.data_content_b64) * 3 // 4
    if estimated_bytes > MAX_CSV_BYTES:
        return ExecuteResponse(
            success=False,
            stdout="",
            stderr=(
                f"Payload too large: ~{estimated_bytes} bytes exceeds the "
                f"{MAX_CSV_BYTES}-byte limit."
            ),
            returncode=-1,
        )

    try:
        csv_bytes = base64.b64decode(req.data_content_b64, validate=True)
    except (binascii.Error, ValueError):
        return ExecuteResponse(
            success=False,
            stdout="",
            stderr="data_content_b64 is not valid base64.",
            returncode=-1,
        )

    # Each request gets its own working directory so concurrent executions can
    # never read, overwrite, or delete each other's files — the caller's
    # filename is not assumed to be unique across requests.
    run_dir = WORKSPACE / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Strip any directory components from the supplied name so the write
        # stays inside the run directory (the sandbox is reachable over the
        # network and the name is untrusted).
        safe_name = Path(req.data_filename).name or "data.csv"
        data_path = run_dir / safe_name
        data_path.write_bytes(csv_bytes)

        # The script reads its data path from an env var so we never have to
        # inject untrusted strings into the R source via templating.
        script_path = run_dir / "script.R"
        script_path.write_text(req.r_script, encoding="utf-8")

        env = dict(os.environ)
        env["DATA_FILE_PATH"] = str(data_path)

        try:
            proc = subprocess.run(
                ["Rscript", "--vanilla", str(script_path)],
                capture_output=True,
                text=True,
                timeout=EXEC_TIMEOUT_SECONDS,
                env=env,
                cwd=str(run_dir),
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                success=False,
                stdout="",
                stderr=f"R execution exceeded {EXEC_TIMEOUT_SECONDS}s timeout.",
                returncode=-1,
            )

        return ExecuteResponse(
            success=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
