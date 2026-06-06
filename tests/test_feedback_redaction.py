"""Tests for the PII redaction applied to error text before it is reused.

A failed R run's stderr can echo real data values, and ``retry_hint`` feeds the
last error back into the next LLM prompt (and, when tracing is on, to a hosted
store). ``_redact`` masks the value-shaped tokens that most commonly leak. This
is a security control, so it gets regression coverage: a change that stops
masking emails or long numbers must fail here.
"""
from __future__ import annotations

from app.agents.feedback import _redact, retry_hint


def test_redacts_emails():
    out = _redact("unexpected value 'jane.doe+test@example.co.uk' in row")
    assert "jane.doe" not in out and "example.co.uk" not in out
    assert "[redacted-email]" in out


def test_redacts_long_numbers_but_keeps_short_ones():
    # 4+ digit runs (ids, amounts) are masked; short numbers (row/line counts,
    # return codes) survive so the diagnostic stays useful.
    out = _redact("row 12 had id 4567890 and code 3")
    assert "4567890" not in out
    assert "[redacted-number]" in out
    assert "row 12" in out and "code 3" in out


def test_retry_hint_is_empty_without_prior_failure():
    assert retry_hint({"retry_count": 0, "errors": []}) == ""


def test_retry_hint_redacts_before_reuse():
    state = {
        "retry_count": 1,
        "errors": ["[executor]\nError: bad value 'bob@corp.com' for customer 998877"],
    }
    hint = retry_hint(state)
    # The corrective instruction is present, but the raw PII is not.
    assert "previous attempt FAILED" in hint
    assert "bob@corp.com" not in hint
    assert "998877" not in hint
    assert "[redacted-email]" in hint and "[redacted-number]" in hint
