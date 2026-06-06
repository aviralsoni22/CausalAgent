"""Post-run cleanup of extracted-data artifacts.

The SQL agent writes the extracted rows to ``data/{task_id}.csv`` so the executor
can ship them to the sandbox. Those rows are the sensitive asset — the very data
Rule 2 keeps out of the LLM — and once a run ends they have no further use. We
delete the file rather than leave customer PII sitting at rest on the
orchestrator's disk indefinitely (data minimisation: the cheapest data to protect
is the data you no longer keep).

``extracted_csv_path`` is the single source of truth for that filename, shared by
the writer (sql_agent) and the cleanup so the two can never drift apart.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core import config

logger = logging.getLogger(__name__)


def extracted_csv_path(task_id: str) -> Path:
    """Canonical path of a task's extracted-data CSV."""
    return Path(config.DATA_DIR) / f"{task_id}.csv"


def purge_extracted_data(task_id: str) -> None:
    """Delete a task's extracted CSV. Best-effort and idempotent.

    Computed from ``task_id`` (not from graph state) so it still runs when the
    graph raised before returning a final state. A missing file — already gone,
    or the run failed before the SQL step wrote one — is fine. A cleanup failure
    must never mask or replace the analysis result, so it only logs.
    """
    path = extracted_csv_path(task_id)
    try:
        path.unlink(missing_ok=True)
        logger.info("Purged extracted data file for task %s", task_id)
    except OSError:
        logger.exception("Failed to purge extracted data file %s", path)
