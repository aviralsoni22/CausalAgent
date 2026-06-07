"""Tests for the E-value sensitivity computation."""
from __future__ import annotations

import math

from app.core.sensitivity import e_value


def test_missing_inputs_return_none():
    assert e_value(None, 1.0, 8.0) == (None, None)
    assert e_value(14.0, 1.0, None) == (None, None)
    assert e_value(14.0, 1.0, 0.0) == (None, None)  # degenerate SD


def test_point_evalue_matches_vanderweele_formula():
    # d = 14/28 = 0.5 -> RR = exp(0.91*0.5); E = RR + sqrt(RR*(RR-1)).
    rr = math.exp(0.91 * 0.5)
    expected = round(rr + math.sqrt(rr * (rr - 1.0)), 2)
    point, _ = e_value(14.0, 1.0, 28.0)
    assert point == expected
    assert point > 1.0


def test_larger_effect_gives_larger_evalue():
    small, _ = e_value(5.0, 1.0, 28.0)
    large, _ = e_value(20.0, 1.0, 28.0)
    assert large > small


def test_ci_crossing_null_yields_evalue_one():
    # Wide SE so the 95% CI includes 0 -> CI-bound E-value collapses to 1.0,
    # signalling a non-robust result, while the point estimate stays > 1.
    point, ci = e_value(2.0, 5.0, 28.0)
    assert ci == 1.0
    assert point > 1.0


def test_protective_effect_is_symmetric():
    pos, _ = e_value(10.0, 1.0, 28.0)
    neg, _ = e_value(-10.0, 1.0, 28.0)
    assert pos == neg  # E-value is symmetric for harmful/protective effects
