"""R-execution sandbox API.

A deliberately tiny, isolated service. It accepts an R script plus a path to a
CSV, materialises the script to disk, runs it with ``Rscript`` in a subprocess,
and returns the captured stdout/stderr. It never imports any orchestrator code,
never sees the LLM, and never holds database credentials — its only job is to
run statistics on a file and hand back text.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

# Directory that is expected to be a shared volume with the orchestrator, so a
# CSV written by the SQL agent is visible here. Configurable for K8s.
DATA_DIR = Path(os.environ.get("SANDBOX_DATA_DIR", "/sandbox/data"))
WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/sandbox/workspace"))
# Hard cap so a runaway / adversarial script cannot pin a worker forever.
EXEC_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_EXEC_TIMEOUT", "120"))

app = FastAPI(title="CausalAgent R Sandbox", version="1.0.0")


class ExecuteRequest(BaseModel):
    r_script: str = Field(..., description="The R source code to execute.")
    data_file_path: str = Field(
        ..., description="Path to the CSV the script should read."
    )


class ExecuteResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    returncode: int


def _resolve_data_path(data_file_path: str) -> Path:
    """Make the orchestrator's CSV path usable inside this container.

    The orchestrator and the sandbox may not share an identical filesystem
    layout (host worker vs. containerised sandbox). If the path as given does
    not exist, fall back to looking up the file's basename inside the shared
    DATA_DIR volume.
    """
    candidate = Path(data_file_path)
    if candidate.is_file():
        return candidate
    # The orchestrator may run on Windows and send a path with backslashes; on
    # Linux Path(...).name would not split those, so normalise separators when
    # extracting the basename for the shared-volume lookup.
    basename = data_file_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    fallback = DATA_DIR / basename
    return fallback


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest) -> ExecuteResponse:
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    resolved = _resolve_data_path(req.data_file_path)
    if not resolved.is_file():
        return ExecuteResponse(
            success=False,
            stdout="",
            stderr=f"Data file not found: {req.data_file_path} (looked in {resolved})",
            returncode=-1,
        )

    # The script reads its data path from an env var so we never have to inject
    # untrusted strings into the R source via templating.
    script_path = WORKSPACE / f"{uuid.uuid4().hex}.R"
    script_path.write_text(req.r_script, encoding="utf-8")

    env = dict(os.environ)
    env["DATA_FILE_PATH"] = str(resolved)

    try:
        proc = subprocess.run(
            ["Rscript", "--vanilla", str(script_path)],
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT_SECONDS,
            env=env,
            cwd=str(WORKSPACE),
        )
    except subprocess.TimeoutExpired:
        return ExecuteResponse(
            success=False,
            stdout="",
            stderr=f"R execution exceeded {EXEC_TIMEOUT_SECONDS}s timeout.",
            returncode=-1,
        )
    finally:
        script_path.unlink(missing_ok=True)

    return ExecuteResponse(
        success=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )
