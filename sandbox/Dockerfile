# R-execution sandbox: isolated container that runs untrusted, LLM-generated
# R scripts. Kept deliberately separate from the orchestrator so a compromised
# script cannot reach the LLM, the database credentials, or the broker.
FROM python:3.12-slim

# --- System + R toolchain -------------------------------------------------
# r-base gives us Rscript; build-essential + gfortran let R compile source
# packages; libblas/liblapack are required to LINK compiled stats packages like
# MatchIt (without them the build links against -lblas/-llapack and fails).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        r-base \
        build-essential \
        gfortran \
        libblas-dev \
        liblapack-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- R packages -----------------------------------------------------------
# MatchIt powers propensity-score matching in the R-stats agent. Verify it
# actually loads and FAIL the build if not — install.packages() only warns on
# failure, which previously let a broken image (missing MatchIt) ship silently.
RUN Rscript -e "install.packages('MatchIt', repos='https://cloud.r-project.org'); if (!requireNamespace('MatchIt', quietly=TRUE)) quit(status=1)"

# --- Python API layer -----------------------------------------------------
WORKDIR /sandbox
COPY requirements.txt /sandbox/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /sandbox/main.py

# Workspace where each request's .R script and CSV are materialised.
RUN mkdir -p /sandbox/workspace

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
