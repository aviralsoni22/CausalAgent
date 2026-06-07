"""Deterministic recovery of the multi-treatment planted effects.

Extends the golden-path gate (``test_pipeline_deterministic``) to the simulation
treatments: the real executor + evaluator run hand-written, known-correct R (no
LLM) over the seeded mart and must recover each planted ATE — and, for the
``saw_banner`` placebo, must adjust the spurious association back to ~0.

Requires the stack up and the DB seeded:
    docker compose up -d --build
    uv run python -m scripts.seed_db
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pandas as pd
import pytest
import requests
from sqlalchemy import text

from app.agents.evaluator import evaluator_node
from app.agents.executor import executor_node
from app.core import config
from app.core.db import get_engine
from app.core.graph import initial_state
from app.sim import effects

# (treatment, confounder, is_categorical_confounder, tolerance)
_CASES = [
    ("received_discount", "age", False, 4.0),
    ("ui_variant_b", "region", True, 4.0),
    ("saw_banner", "loyalty_tier", True, 2.5),  # placebo: planted 0.0
]


@pytest.fixture(scope="module")
def services_up():
    try:
        requests.get(f"{config.R_SANDBOX_URL}/health", timeout=3).raise_for_status()
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Stack not available ({exc}). Run docker compose up + seed_db.")


def _extract_csv(treatment: str, confounder: str) -> tuple[str, str]:
    sql = text(
        f"SELECT o.total_amount, o.{treatment}, c.{confounder} "
        "FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
    )
    with get_engine().connect().execution_options(postgresql_readonly=True) as conn:
        df = pd.read_sql(sql, conn)
    assert not df.empty, "DB returned no rows — did seed_db run?"
    task_id = f"pytest_{uuid.uuid4().hex}"
    path = Path(config.DATA_DIR) / f"{task_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return task_id, str(path)


def _r_script(treatment: str, confounder: str, categorical: bool) -> str:
    factor_line = f'df${confounder} <- factor(df${confounder})\n' if categorical else ""
    return rf"""
data_path <- Sys.getenv("DATA_FILE_PATH")
df <- read.csv(data_path, stringsAsFactors = FALSE, na.strings = "")
df <- na.omit(df[, c("total_amount", "{treatment}", "{confounder}")])
df${treatment} <- as.integer(df${treatment})
{factor_line}model <- lm(total_amount ~ {treatment} + {confounder}, data = df)
co <- summary(model)$coefficients
row <- co["{treatment}", ]
osd <- sd(df$total_amount, na.rm = TRUE)
cat(sprintf('{{"p_value": %.10f, "ate": %.10f, "std_error": %.10f, "outcome_sd": %.10f, "method": "%s", "n_used": %d}}',
            row["Pr(>|t|)"], row["Estimate"], row["Std. Error"], osd, "covariate_adjusted_lm", nrow(df)),
    "\n", sep = "")
"""


@pytest.mark.parametrize("treatment,confounder,categorical,tol", _CASES)
def test_pipeline_recovers_planted_effect(services_up, treatment, confounder, categorical, tol):
    task_id, path = _extract_csv(treatment, confounder)
    try:
        state = initial_state(task_id, f"Effect of {treatment} on total_amount?")
        state["data_file_path"] = path
        state["r_script"] = _r_script(treatment, confounder, categorical)

        state.update(executor_node(state))
        assert state["current_status"] == "executed", state.get("errors")
        state.update(evaluator_node(state))
        assert state["current_status"] == "evaluated", state.get("errors")

        stats = state["statistical_output"]
        planted = effects.TRUE_EFFECTS[treatment]
        assert abs(stats["ate"] - planted) < tol, (treatment, stats["ate"], planted, stats)
        # E-value is reported; ~1 for the planted-zero placebo, >1 for real effects.
        assert stats["e_value"] is not None and stats["e_value"] >= 1.0, stats
    finally:
        Path(path).unlink(missing_ok=True)
