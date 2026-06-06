"""Reviewer node.

Translates the cold statistical output (ATE, p-value, significance) into a
concise, non-technical narrative a business stakeholder can act on. This is the
terminal happy-path node.
"""
from __future__ import annotations

from app.agents.feedback import record_failure
from app.core.llm import get_llm
from app.core.observability import run_config
from app.core.state import CausalGraphState
from app.models.schemas import BusinessNarrative

_SYSTEM_PROMPT = """You are a data-savvy business analyst. Given a stakeholder's \
original question and the statistical result of a model, explain the finding in \
2-4 sentences for a non-technical audience.

Guidance:
- The ATE is the estimated average effect of the treatment on the outcome,
  after adjusting for the covariates (the `method` tells you how it was
  estimated: matching, covariate adjustment, or unadjusted).
- If the result is NOT statistically significant (is_significant = false), say \
plainly that the data does not provide strong evidence of an effect; do not \
overstate it.
- Mention the direction and rough magnitude of the effect, and the p-value in \
plain terms (e.g. "statistically significant at the 5% level").
- CRITICAL HONESTY: this is an observational estimate. State that it relies on \
the assumption of no important unobserved confounders, and therefore reflects \
an adjusted association / estimated effect — NOT definitive proof of causation. \
If method is "unadjusted_lm", warn that no confounders were controlled for, so \
the result is especially likely to be confounded.
- BALANCE: if matching was used (method "psm_matchit_lm") and balanced is false, \
warn that the treatment and control groups remain imbalanced on the measured \
covariates, so this estimate is unreliable and should be treated with caution. \
If balanced is true, you may note the groups were well matched on observed \
covariates.
- Open by restating, in plain words, HOW the question was interpreted (the \
provided interpretation line) so the reader can confirm we answered what they \
asked. Do not contradict it.
- No code, no jargon dumps."""


def build_interpretation(spec: dict, stats: dict) -> str:
    """Plain-language statement of the identification actually used.

    Deterministic (not LLM-generated) so it is a trustworthy artefact the user
    can check against their intent: which treatment, which outcome, which
    confounders, over how many observations.
    """
    treatment = spec.get("treatment") or "the treatment"
    outcome = spec.get("outcome") or "the outcome"
    confounders = spec.get("confounders") or []
    sentence = f"Measured the effect of '{treatment}' on '{outcome}'"
    sentence += (
        f", adjusting for {', '.join(confounders)}."
        if confounders
        else ", with no confounders adjusted for."
    )
    n_used = stats.get("n_used")
    if n_used is not None:
        sentence += f" Based on {n_used} observations."
    return sentence


def reviewer_node(state: CausalGraphState) -> dict:
    try:
        stats = state.get("statistical_output") or {}
        interpretation = build_interpretation(state.get("analysis_spec") or {}, stats)
        human = (
            f"Original question:\n{state['user_query']}\n\n"
            f"How the question was interpreted:\n{interpretation}\n\n"
            f"Statistical result:\n"
            f"- ATE (treatment coefficient): {stats.get('ate')}\n"
            f"- p_value: {stats.get('p_value')}\n"
            f"- is_significant (p <= 0.05): {stats.get('is_significant')}\n"
            f"- estimation method: {stats.get('method', 'unknown')}\n"
            f"- sample size used: {stats.get('n_used', 'unknown')}\n"
            f"- max standardised mean diff (balance): {stats.get('max_smd', 'n/a')}\n"
            f"- balanced (smd < 0.1): {stats.get('balanced', 'n/a')}"
        )
        llm = get_llm().with_structured_output(BusinessNarrative)
        result: BusinessNarrative = llm.invoke(
            [("system", _SYSTEM_PROMPT), ("human", human)],
            config=run_config(state, "reviewer"),
        )
        return {
            "business_narrative": result.business_narrative,
            "interpretation": interpretation,
            "current_status": "completed",
        }
    except Exception:
        return record_failure(state, "reviewer", "review_failed")
