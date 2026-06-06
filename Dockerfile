# Application image for the api, worker, and consumer roles. One image, three
# entrypoints (the compose/PaaS service chooses the command). The R toolchain is
# deliberately absent — R only ever runs in the separate sandbox image (Rule 3),
# so this stays small and holds no execution surface for untrusted code.
FROM python:3.13-slim

# uv for fast, locked installs (matches the local toolchain).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install dependencies first (their own layer) so source edits don't re-resolve.
# --no-dev drops pytest; the project itself has no build-system, so only deps are
# installed and the source is put on PYTHONPATH below (mirrors pytest's config).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

# Default role is the API ingress; worker/consumer override `command` in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
