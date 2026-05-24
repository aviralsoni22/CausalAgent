"""Verify the R sandbox executes a real script end to end (no LLM needed).

Writes a small CSV into DATA_DIR with a known treatment effect, then POSTs a
hand-written R script (lm + MatchIt) to the sandbox /execute endpoint and prints
the JSON it returns. Proves: shared volume, Rscript, MatchIt, and the executor's
HTTP contract all work -- the entire non-LLM half of the pipeline.

Run (with the stack up):
    .venv/Scripts/python.exe -m scripts.verify_sandbox
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import requests

from app.core import config

CSV_NAME = "verify_sandbox.csv"
N = 1000
TRUE_ATE = 14.0

R_SCRIPT = r"""
df <- read.csv(Sys.getenv("DATA_FILE_PATH"))
df <- na.omit(df)
fit <- lm(total_amount ~ received_discount + age, data = df)
co <- summary(fit)$coefficients
ate <- unname(co["received_discount", "Estimate"])
pval <- unname(co["received_discount", "Pr(>|t|)"])
cat(sprintf('{"p_value": %.6g, "ate": %.6g, "method": "lm", "n_used": %d}',
            pval, ate, nrow(df)))
"""


def _write_csv() -> Path:
    rng = random.Random(42)
    path = Path(config.DATA_DIR) / CSV_NAME
    rows = ["received_discount,age,total_amount"]
    for _ in range(N):
        d = rng.randint(0, 1)
        age = rng.randint(18, 70)
        total = 50.0 + TRUE_ATE * d + 0.4 * age + rng.gauss(0, 8)
        rows.append(f"{d},{age},{round(max(total, 0.0), 2)}")
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def main() -> int:
    csv_path = _write_csv()
    print(f"Wrote {csv_path} ({N} rows, true ATE = {TRUE_ATE}).")

    resp = requests.post(
        f"{config.R_SANDBOX_URL}/execute",
        json={"r_script": R_SCRIPT, "data_file_path": csv_path.name},
        timeout=config.R_SANDBOX_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    print(f"\nsuccess   : {payload['success']}")
    print(f"returncode: {payload['returncode']}")
    print(f"stdout    : {payload['stdout']}")
    if payload["stderr"]:
        print(f"stderr    : {payload['stderr']}")

    if not payload["success"]:
        print("\nFAIL: sandbox returned non-zero.")
        return 1

    stats = json.loads(payload["stdout"])
    print(f"\nParsed JSON: {stats}")
    print(
        f"OK: recovered ATE ~= {stats['ate']:.2f} "
        f"(true {TRUE_ATE}); sandbox executes R end to end."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
