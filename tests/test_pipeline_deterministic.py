"""Deterministic end-to-end validation of the non-LLM machinery.

This is the golden-path gate. It exercises everything except the LLM:
DB read -> CSV save -> sandbox HTTP -> Rscript -> (MatchIt | lm) -> executor ->
evaluator. The seed data plants a true discount effect of ~14 USD on
``total_amount``; if the pipeline is wired correctly the recovered ATE must land
near 14 and be strongly significant. A wrong-but-plausible number here is
exactly the silent failure mode we care about catching.

Requires the docker-compose stack up and the DB seeded:
    docker compose up -d --build
    uv run python -m scripts.seed_db
    uv run pytest -v
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

TRUE_ATE = 14.0
ATE_TOLERANCE = 4.0  # generous; recovery is unbiased with randomized treatment

# Pull a flat, model-ready frame exactly as the SQL agent is meant to.
_EXTRACT_SQL = """
SELECT o.total_amount, o.received_discount, c.age, c.region
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
"""

# Hand-written, known-correct R for the covariate-adjusted path.
_R_COVARIATE_ADJUSTED = r"""
data_path <- Sys.getenv("DATA_FILE_PATH")
df <- read.csv(data_path, stringsAsFactors = FALSE, na.strings = "")
df <- na.omit(df[, c("total_amount", "received_discount", "age", "region")])
df$received_discount <- as.integer(df$received_discount)
df$region <- factor(df$region)
model <- lm(total_amount ~ received_discount + age + region, data = df)
co <- summary(model)$coefficients
row <- co["received_discount", ]
cat(sprintf('{"p_value": %.10f, "ate": %.10f, "method": "%s", "n_used": %d}',
            row["Pr(>|t|)"], row["Estimate"], "covariate_adjusted_lm", nrow(df)),
    "\n", sep = "")
"""

# Hand-written, known-correct R for the MatchIt propensity-score path. Uses the
# numeric covariate only to keep matching well-behaved.
_R_MATCHIT = r"""
library(MatchIt)
data_path <- Sys.getenv("DATA_FILE_PATH")
df <- read.csv(data_path, stringsAsFactors = FALSE, na.strings = "")
df <- na.omit(df[, c("total_amount", "received_discount", "age")])
df$received_discount <- as.integer(df$received_discount)
m <- matchit(received_discount ~ age, data = df, method = "nearest")
md <- match.data(m)
model <- lm(total_amount ~ received_discount + age, data = md)
co <- summary(model)$coefficients
row <- co["received_discount", ]
s <- summary(m)$sum.matched
max_smd <- max(abs(s[, "Std. Mean Diff."]), na.rm = TRUE)
cat(sprintf('{"p_value": %.10f, "ate": %.10f, "method": "%s", "n_used": %d, "max_smd": %.6f}',
            row["Pr(>|t|)"], row["Estimate"], "psm_matchit_lm", nrow(md), max_smd),
    "\n", sep = "")
"""


@pytest.fixture(scope="module")
def services_up():
    """Skip the module cleanly if the stack isn't running."""
    try:
        requests.get(f"{config.R_SANDBOX_URL}/health", timeout=3).raise_for_status()
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Stack not available ({exc}). Run docker compose up + seed_db.")


@pytest.fixture
def seeded_csv(services_up):
    """Extract the seeded data to a CSV the sandbox can read; clean up after."""
    engine = get_engine()
    with engine.connect().execution_options(postgresql_readonly=True) as conn:
        df = pd.read_sql(text(_EXTRACT_SQL), conn)
    assert not df.empty, "DB returned no rows — did seed_db run?"

    task_id = f"pytest_{uuid.uuid4().hex}"
    path = Path(config.DATA_DIR) / f"{task_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    yield task_id, str(path)
    path.unlink(missing_ok=True)


def _run(task_id: str, data_file_path: str, r_script: str) -> dict:
    """Drive the real executor + evaluator nodes and return the stats dict."""
    state = initial_state(task_id, "Did receiving a discount raise order total?")
    state["data_file_path"] = data_file_path
    state["r_script"] = r_script

    state.update(executor_node(state))
    assert state["current_status"] == "executed", state.get("errors")

    state.update(evaluator_node(state))
    assert state["current_status"] == "evaluated", state.get("errors")
    return state["statistical_output"]


def test_covariate_adjusted_recovers_true_ate(seeded_csv):
    task_id, path = seeded_csv
    stats = _run(task_id, path, _R_COVARIATE_ADJUSTED)

    assert stats["method"] == "covariate_adjusted_lm"
    assert stats["n_used"] == 3000
    assert abs(stats["ate"] - TRUE_ATE) < ATE_TOLERANCE, stats
    assert stats["p_value"] < 0.05 and stats["is_significant"] is True, stats


def test_matchit_recovers_true_ate(seeded_csv):
    task_id, path = seeded_csv
    stats = _run(task_id, path, _R_MATCHIT)

    assert stats["method"] == "psm_matchit_lm"
    assert abs(stats["ate"] - TRUE_ATE) < ATE_TOLERANCE, stats
    assert stats["p_value"] < 0.05 and stats["is_significant"] is True, stats
    # Balance must be reported and good (treatment is randomized on age).
    assert stats["max_smd"] is not None and stats["max_smd"] < 0.1, stats
    assert stats["balanced"] is True, stats
