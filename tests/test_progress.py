"""Tests for the wait-time progress labels.

The worker publishes a friendly stage label per pipeline step so a polling client
sees "model run" instead of a silent STARTED. The label mapping is a pure
function, pinned here; the streaming that drives it needs a live Celery + LLM and
is left to integration.
"""
from __future__ import annotations

import pytest

from app.worker import progress_meta


@pytest.mark.parametrize(
    "status,stage",
    [
        ("pending", "queued"),
        ("sql_done", "data extracted"),
        ("r_generated", "model written"),
        ("executed", "model run"),
        ("evaluated", "results computed"),
        ("completed", "finalizing"),
    ],
)
def test_known_stages_map_to_friendly_labels(status, stage):
    meta = progress_meta(status, "task-1")
    assert meta == {"task_id": "task-1", "status": status, "stage": stage}


def test_failure_statuses_show_retrying():
    assert progress_meta("sql_failed", "t")["stage"] == "retrying"
    assert progress_meta("exec_failed_transient", "t")["stage"] == "retrying"
    assert progress_meta("failed", "t")["stage"] == "retrying"


def test_unknown_status_falls_back_to_working():
    assert progress_meta("something_else", "t")["stage"] == "working"
    assert progress_meta("", "t")["stage"] == "working"
