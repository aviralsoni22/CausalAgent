"""FastAPI ingress.

The synchronous front door to an asynchronous platform. It accepts a natural
language question, mints a ``task_id``, enqueues the analysis on Celery, and
returns immediately. Clients poll ``/status/{task_id}`` for the result. The
ingress never runs the graph itself — that is the worker's job — so requests
stay fast and the heavy LLM + R work happens off the request path.
"""
from __future__ import annotations

import uuid

from celery.result import AsyncResult
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.models.schemas import AnalysisSpec
from app.worker import celery_app, run_causal_analysis

app = FastAPI(title="CausalAgent Ingress", version="1.0.0")


class AnalyzeRequest(BaseModel):
    query: str = Field(..., description="The natural-language causal question to analyse.")
    spec: AnalysisSpec | None = Field(
        default=None,
        description=(
            "Optional explicit identification (treatment/outcome/confounders). "
            "Provide it to pin the causal strategy; omit to let the SQL agent "
            "propose one."
        ),
    )


class AnalyzeResponse(BaseModel):
    task_id: str
    status: str


class StatusResponse(BaseModel):
    task_id: str
    state: str
    result: dict | None = None
    # Populated while the task is running: {"stage": "model run", "status": ...}
    # so a polling client can show progress instead of a black-box wait.
    progress: dict | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    task_id = uuid.uuid4().hex
    spec = req.spec.model_dump() if req.spec else None
    # Pass our own task_id as the Celery task id so the CSV file name, the graph
    # state, and the polling key all line up.
    run_causal_analysis.apply_async(args=[task_id, req.query, spec], task_id=task_id)
    return AnalyzeResponse(task_id=task_id, status="queued")


@app.get("/status/{task_id}", response_model=StatusResponse)
def status(task_id: str) -> StatusResponse:
    async_result = AsyncResult(task_id, app=celery_app)
    result = async_result.result if async_result.successful() else None
    # While running, the worker stores the current stage in the task's meta
    # (state == "PROGRESS", info is the meta dict). Surface it so the caller can
    # see where the run is rather than polling a silent STARTED.
    progress = (
        async_result.info
        if async_result.state == "PROGRESS" and isinstance(async_result.info, dict)
        else None
    )
    return StatusResponse(
        task_id=task_id,
        state=async_result.state,
        result=result if isinstance(result, dict) else None,
        progress=progress,
    )
