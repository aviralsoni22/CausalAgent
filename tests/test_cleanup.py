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


def test_self_describing_name_is_purged_by_task_id(tmp_path, monkeypatch):
    # The writer's name carries a question slug; purge (which only has the
    # task_id) must still find and delete it via the suffix glob.
    monkeypatch.setattr(cleanup.config, "DATA_DIR", str(tmp_path))
    path = cleanup.extracted_csv_path("abc123", "Did giving a discount raise order total?")
    assert path.name == "did-giving-a-discount-raise-order-total__abc123.csv"
    path.write_text("x\n1\n")

    cleanup.purge_extracted_data("abc123")  # no query — must still remove it

    assert not path.exists()


def test_slug_sanitises_adversarial_question(tmp_path, monkeypatch):
    # An injection-laden question must yield only [a-z0-9-] in the filename.
    monkeypatch.setattr(cleanup.config, "DATA_DIR", str(tmp_path))
    path = cleanup.extracted_csv_path("t9", "DROP TABLE orders; ../../etc/passwd")
    assert path.name == "drop-table-orders-etc-passwd__t9.csv"
    assert "/" not in path.name and ".." not in path.name
