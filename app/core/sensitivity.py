"""Sensitivity analysis for unobserved confounding — the E-value.

An observational estimate only identifies the causal effect under "no unobserved
confounding", which is untestable. The **E-value** (VanderWeele & Ding, 2017)
quantifies how strong such an unobserved confounder would have to be: it is the
minimum strength of association — on the risk-ratio scale, with BOTH the
treatment and the outcome, beyond the measured covariates — that could fully
explain away the observed effect. A larger E-value means a more robust finding;
an E-value near 1 means a trivially weak confounder could overturn it.

Computed deterministically here (not by the LLM-generated R) so the math is fixed
and testable. For a continuous outcome we use the standard approximation: convert
the estimate to a standardized mean difference (Cohen's d = estimate / SD of the
outcome), map it to an approximate risk ratio, then apply the E-value formula.
"""
from __future__ import annotations

import math

_Z_95 = 1.959963984540054  # 97.5th percentile of the standard normal


def _rr_from_standardized(d: float) -> float:
    """Approximate risk ratio from a standardized mean difference (Chinn 2000)."""
    return math.exp(0.91 * d)


def _evalue_from_rr(rr: float) -> float:
    """E-value for a risk ratio (symmetric for protective effects)."""
    if rr < 1.0:
        rr = 1.0 / rr
    return rr + math.sqrt(rr * (rr - 1.0))


def e_value(
    ate: float | None, std_error: float | None, outcome_sd: float | None
) -> tuple[float | None, float | None]:
    """E-values for the point estimate and the 95% CI bound nearest the null.

    Returns ``(point, ci_bound)``, each rounded to 2 dp, or ``None`` where the
    inputs are missing or degenerate. The CI-bound E-value is the honest one to
    report: if the confidence interval touches the null it is 1.0, signalling the
    estimate is not robust.
    """
    if ate is None or not outcome_sd or outcome_sd <= 0:
        return None, None

    point = _evalue_from_rr(_rr_from_standardized(ate / outcome_sd))

    ci_evalue: float | None = None
    if std_error is not None and std_error >= 0:
        if ate >= 0:
            bound = max(ate - _Z_95 * std_error, 0.0)  # crosses null -> bound at 0
        else:
            bound = min(ate + _Z_95 * std_error, 0.0)
        ci_evalue = _evalue_from_rr(_rr_from_standardized(bound / outcome_sd))

    return round(point, 2), (round(ci_evalue, 2) if ci_evalue is not None else None)
