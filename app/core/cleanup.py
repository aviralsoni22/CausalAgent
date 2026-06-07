"""Post-run cleanup of extracted-data artifacts.

The SQL agent writes the extracted rows to ``data/<slug>__<task_id>.csv`` so the
executor can ship them to the sandbox. Those rows are the sensitive asset — the
very data Rule 2 keeps out of the LLM — and once a run ends they have no further
use. We delete the file rather than leave customer PII sitting at rest on the
orchestrator's disk indefinitely (data minimisation: the cheapest data to protect
is the data you no longer keep).

The filename carries a human-readable slug of the question so a file in ``data/``
can be matched to its analysis at a glance, with the ``task_id`` kept as the
stable suffix. ``extracted_csv_path`` is the single source of truth for the writer
(sql_agent); cleanup deletes by globbing the ``task_id`` suffix, so it purges the
file regardless of the slug (it is called with only the task_id).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.core import config

logger = logging.getLogger(__name__)

_SLUG_MAX = 50


def _slugify(text: str | None) -> str:
    """A safe, bounded filename slug from the (untrusted) question.

    Lower-cases, collapses any run of non-alphanumerics to a single hyphen, and
    trims — so an injection-laden question yields only ``[a-z0-9-]`` and can never
    be a path-traversal or shell vector in the filename.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:_SLUG_MAX].rstrip("-") or "query"


def extracted_csv_path(task_id: str, query: str | None = None) -> Path:
    """Canonical path of a task's extracted-data CSV.

    With ``query`` (the writer's case) the name is ``<slug>__<task_id>.csv`` so the
    file is self-describing; without it (purge / tests) it falls back to the bare
    ``<task_id>.csv`` — purge globs the suffix, so either form is found.
    """
    stem = f"{_slugify(query)}__{task_id}" if query else task_id
    return Path(config.DATA_DIR) / f"{stem}.csv"


def purge_extracted_data(task_id: str) -> None:
    """Delete a task's extracted CSV(s). Best-effort and idempotent.

    Globs ``*<task_id>.csv`` so it deletes the slugged name without needing the
    question. Computed from ``task_id`` (not graph state) so it still runs when the
    graph raised before returning a final state. A missing file is fine. A cleanup
    failure must never mask the analysis result, so it only logs.
    """
    found = False
    for path in Path(config.DATA_DIR).glob(f"*{task_id}.csv"):
        found = True
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to purge extracted data file %s", path)
    if found:
        logger.info("Purged extracted data file(s) for task %s", task_id)
