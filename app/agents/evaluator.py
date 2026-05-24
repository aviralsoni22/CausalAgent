"""Evaluator node.

Parses the strict JSON the R script printed to stdout, normalises it into the
state's ``statistical_output`` contract, and applies the significance rule:
a result is significant only when p_value <= 0.05.
"""
from __future__ import annotations

import json
import re

from app.agents.feedback import record_failure
from app.core.state import CausalGraphState

_SIGNIFICANCE_THRESHOLD = 0.05
# Largest acceptable standardised mean difference after matching.
_BALANCE_THRESHOLD = 0.1
_JSON_OBJ = re.compile(r"\{.*\}")


def _extract_json(stdout: str) -> dict:
    """Pull the JSON object out of R's stdout.

    The contract asks R to print exactly one JSON line, but we scan from the
    last line backwards so incidental warnings printed earlier do not break us.
    """
    for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
        match = _JSON_OBJ.search(line)
        if match:
            return json.loads(match.group(0))
    raise ValueError(f"No JSON object found in R stdout:\n{stdout!r}")


def evaluator_node(state: CausalGraphState) -> dict:
    try:
        raw = (state.get("statistical_output") or {}).get("raw_stdout", "")
        parsed = _extract_json(raw)

        p_value = float(parsed["p_value"])
        ate = float(parsed["ate"])
        is_significant = p_value <= _SIGNIFICANCE_THRESHOLD

        output = {
            "p_value": p_value,
            "ate": ate,
            "is_significant": is_significant,
        }
        # Preserve estimation provenance when the R script reported it.
        if "method" in parsed:
            output["method"] = parsed["method"]
        if "n_used" in parsed:
            output["n_used"] = parsed["n_used"]

        # Post-match covariate balance. A common rule of thumb: a standardised
        # mean difference under 0.1 indicates good balance. Balance is only
        # meaningful when a matching method ran (max_smd is a real number); when
        # max_smd is null — no matching, e.g. a covariate-adjusted lm — balance
        # is N/A (None), NOT False, so we don't imply a non-matched estimate is
        # "poorly balanced".
        if "max_smd" in parsed:
            max_smd = parsed["max_smd"]
            output["max_smd"] = max_smd
            output["balanced"] = (
                float(max_smd) < _BALANCE_THRESHOLD if max_smd is not None else None
            )

        return {
            "statistical_output": output,
            "current_status": "evaluated",
        }
    except Exception:
        return record_failure(state, "evaluator", "eval_failed")
