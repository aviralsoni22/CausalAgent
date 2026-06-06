# R-execution sandbox: isolated container that runs untrusted, LLM-generated
# R scripts. Kept deliberately separate from the orchestrator so a compromised
# script cannot reach the LLM, the database credentials, or the broker.
FROM python:3.12-slim

# --- System + R toolchain -------------------------------------------------
# r-base gives us Rscript; build-essential + gfortran let R compile source
# packages; libblas/liblapack are required to LINK compiled stats packages like
# MatchIt (without them the build links against -lblas/-llapack and fails).
# iptables installs the egress lockdown at startup; util-linux provides setpriv,
# used to drop privileges before launching the API (see entrypoint.sh).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        r-base \
        build-essential \
        gfortran \
        libblas-dev \
        liblapack-dev \
        iptables \
        util-linux \
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
COPY entrypoint.sh /sandbox/entrypoint.sh
# Strip any CRLF (the repo may be checked out on Windows) so the shebang is valid,
# then make it executable.
RUN sed -i 's/\r$//' /sandbox/entrypoint.sh && chmod +x /sandbox/entrypoint.sh

# Workspace where each request's .R script and CSV are materialised. At runtime
# this path is a writable tmpfs (see docker-compose) so the rest of the rootfs
# can be mounted read-only; mode 1777 lets the unprivileged service user write.
RUN mkdir -p /sandbox/workspace && chmod 1777 /sandbox/workspace

# Unprivileged account the API actually runs as. The entrypoint starts as root
# only to install the firewall rule, then drops to this uid via setpriv.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandbox

EXPOSE 8001

# The entrypoint locks down egress and drops privileges, then execs the CMD.
ENTRYPOINT ["/sandbox/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
