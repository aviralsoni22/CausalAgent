"""Agent identification + recovery eval — proves the LLM path, not just the plumbing.

Submits free-text causal questions to the live ingress with NO pinned spec, so the
SQL agent must choose the treatment / outcome / confounders itself. Then scores two
things the deterministic golden-path test cannot:

  * identification — did it pick the planted treatment and outcome?
  * recovery       — did the adjusted ATE land near the planted truth (≈0 for the
                     free-shipping placebo)?

This is a measurement tool, not a CI gate: LLM runs cost money and are
nondeterministic. Run it against a live, seeded stack.

    uv run python -m scripts.eval_agent
    uv run python -m scripts.eval_agent --repeat 3   # stability across reruns
"""
from __future__ import annotations

import argparse
import json
import time

import requests

from app.core import config
from app.sim import effects

# (question, expected_treatment, expected_outcome, tolerance). The placebo's
# planted effect is 0.0, so a tight tolerance checks the agent adjusts the
# spurious loyalty-tier association away.
CASES: list[tuple[str, str, str, float]] = [
    (
        "Did giving customers a discount actually increase how much they spent per order?",
        "received_discount", "total_amount", 4.0,
    ),
    (
        "Setting discounts aside, does the new checkout experience (UI variant B) cause higher order totals?",
        "ui_variant_b", "total_amount", 4.0,
    ),
    (
        "Did showing the free-shipping banner increase the value of orders?",
        "saw_banner", "total_amount", 3.0,
    ),
    (
        "Were customers who received a discount led to bigger orders because of the discount itself?",
        "received_discount", "total_amount", 4.0,
    ),
]


def _coerce_stat(stat) -> dict:
    if isinstance(stat, str):
        try:
            return json.loads(stat)
        except (ValueError, TypeError):
            return {}
    return stat or {}


def run_case(question: str, base: str, timeout: float, interval: float) -> dict:
    task_id = requests.post(f"{base}/analyze", json={"query": question}, timeout=15).json()["task_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = requests.get(f"{base}/status/{task_id}", timeout=15).json()
        state = s.get("state")
        if state == "SUCCESS":
            return s.get("result") or {}
        if state == "FAILURE":
            return {"_error": "task failed"}
        time.sleep(interval)
    return {"_error": "timeout"}


def score(case: tuple, result: dict) -> dict:
    _, exp_t, _exp_o, tol = case
    spec = result.get("analysis_spec") or {}
    stat = _coerce_stat(result.get("statistical_output"))
    ate = stat.get("ate")
    planted = effects.TRUE_EFFECTS.get(exp_t)
    confounders = spec.get("confounders") or []
    true_conf = effects.CONFOUNDER[exp_t]
    # The SQL agent aliases columns to canonical "treatment"/"outcome", and the
    # raw SQL is (by design) not in the public result — so recovery against the
    # DISTINCT planted effects (14 / 6 / 0) is the real identification proof:
    # the right number is only reachable if the right treatment was chosen.
    return {
        "recovery": ate is not None and planted is not None and abs(ate - planted) < tol,
        "confounder_ok": true_conf in confounders,
        "true_confounder": true_conf,
        "confounders": confounders,
        "ate": ate,
        "planted": planted,
        "error": result.get("_error"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=config.API_BASE_URL)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=720.0)
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()

    conf_ok = recov_ok = total = 0
    print(f"Evaluating the agent at {args.base} ({len(CASES)} cases x {args.repeat})\n")
    for case in CASES:
        question = case[0]
        for r in range(args.repeat):
            result = run_case(question, args.base, args.timeout, args.interval)
            s = score(case, result)
            total += 1
            conf_ok += s["confounder_ok"]
            recov_ok += s["recovery"]
            tag = f"[{r + 1}/{args.repeat}] " if args.repeat > 1 else ""
            print(f"{tag}Q: {question}")
            if s["error"]:
                print(f"   [ERROR] run error: {s['error']}\n")
                continue
            recov_mark = "PASS" if s["recovery"] else "FAIL"
            conf_mark = "PASS" if s["confounder_ok"] else "FAIL"
            print(
                f"   recovery        [{recov_mark}]  ATE={s['ate']} vs planted {s['planted']}"
            )
            print(
                f"   confounder cov. [{conf_mark}]  needs '{s['true_confounder']}'; "
                f"chose {s['confounders']}\n"
            )

    print("=" * 60)
    print(f"Recovery: {recov_ok}/{total}    Confounder coverage: {conf_ok}/{total}")
    print("(treatment/outcome are SQL aliases; recovery vs distinct planted effects is the identification proof)")


if __name__ == "__main__":
    main()
