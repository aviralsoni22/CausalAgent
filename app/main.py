"""FastAPI ingress.

The synchronous front door to an asynchronous platform. It accepts a natural
language question, mints a ``task_id``, enqueues the analysis on Celery, and
returns immediately. Clients poll ``/status/{task_id}`` for the result. The
ingress never runs the graph itself — that is the worker's job — so requests
stay fast and the heavy LLM + R work happens off the request path.
"""
from __future__ import annotations

import logging
import uuid

from celery.result import AsyncResult
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from app.core import config
from app.core.security import rate_limit, require_api_key
from app.models.schemas import AnalysisSpec
from app.worker import celery_app, run_causal_analysis

logger = logging.getLogger(__name__)

app = FastAPI(title="CausalAgent Ingress", version="1.0.0")

# The analysis endpoints carry both guards: authenticate first, then rate-limit
# (a rejected caller shouldn't consume budget). /health stays open for probes.
_GUARDED = [Depends(require_api_key), Depends(rate_limit)]

if not config.INGRESS_API_KEYS:
    logger.warning(
        "Ingress is running WITHOUT API-key auth (INGRESS_API_KEYS is empty). "
        "Fine for local dev; set INGRESS_API_KEYS before exposing this service."
    )

# Fake-storefront demo surface (/sim/emit, /sim/truth, /sim/). State-changing and
# meant for keyless local browser use, so it is mounted only when explicitly
# enabled — never in an exposed deployment. It still gets rate-limiting (but not
# the API key, which would break its own storefront page).
if config.ENABLE_SIM_ROUTES:
    from app.sim.routes import router as sim_router

    app.include_router(sim_router, dependencies=[Depends(rate_limit)])


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


@app.post("/analyze", response_model=AnalyzeResponse, dependencies=_GUARDED)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    task_id = uuid.uuid4().hex
    spec = req.spec.model_dump() if req.spec else None
    # Pass our own task_id as the Celery task id so the CSV file name, the graph
    # state, and the polling key all line up.
    run_causal_analysis.apply_async(args=[task_id, req.query, spec], task_id=task_id)
    return AnalyzeResponse(task_id=task_id, status="queued")


@app.get("/status/{task_id}", response_model=StatusResponse, dependencies=_GUARDED)
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
