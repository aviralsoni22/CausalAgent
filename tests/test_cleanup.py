"""Tests for post-run cleanup of extracted-data artifacts.

The extracted CSV holds raw customer rows (the data Rule 2 keeps from the LLM), so
deleting it after a run is a data-minimisation control, not a nicety. These pin
that the file is actually removed and that a missing file is a no-op — so a
refactor can't quietly turn the purge into a leak.
"""
from __future__ import annotations

from app.core import cleanup


def test_purge_removes_existing_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup.config, "DATA_DIR", str(tmp_path))
    path = cleanup.extracted_csv_path("task-1")
    path.write_text("x\n1\n")
    assert path.exists()

    cleanup.purge_extracted_data("task-1")

    assert not path.exists()


def test_purge_is_idempotent_when_file_absent(tmp_path, monkeypatch):
    # The run may have failed before the SQL step wrote anything; purging a
    # non-existent file must not raise.
    monkeypatch.setattr(cleanup.config, "DATA_DIR", str(tmp_path))
    cleanup.purge_extracted_data("never-existed")  # must be a no-op, no exception
