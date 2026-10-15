# 3d-api — the PrintOps HTTP service (FastAPI + SQLite), built from the 3d-cli repo root:
#   docker compose -p printops -f services/3d-api/compose.yaml up -d --build
# Build context = repo root; Dockerfile.dockerignore keeps it to bin/3d, lib/, fleet/, pyproject.toml.
FROM python:3.12-slim-bookworm

ARG PRINTOPS_VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REPO_ROOT=/app \
    PYTHONPATH=/app/lib \
    XDG_CONFIG_HOME=/home/printops/.config \
    PRINTOPS_DB=/data/printops.db \
    PRINTOPS_TOKENS_FILE=/data/tokens.json \
    PRINTOPS_VERSION=${PRINTOPS_VERSION}

# Runtime deps only: FastAPI tier + pyyaml (lib/printops is stdlib + pyyaml otherwise).
# `websockets`: plain uvicorn ships no WebSocket implementation, so WS /v1/ws would refuse upgrades.
RUN pip install "fastapi>=0.110,<1" "uvicorn>=0.27,<1" "websockets>=12,<16" "pyyaml>=6,<7"

# uid/gid 1000 = `ultra` on the Odroid, so bind-mounted data stays owned by the host user.
RUN groupadd -g 1000 printops && useradd -u 1000 -g 1000 -m printops \
    && mkdir -p /data /backup /home/printops/.config/3d-cli \
    && touch /home/printops/.config/3d-cli/.bootstrapped \
    && chown -R printops:printops /data /backup /home/printops

WORKDIR /app
COPY pyproject.toml /app/pyproject.toml
COPY bin/3d /app/bin/3d
COPY lib/ /app/lib/
# fleet/printers.yaml: printer slots (slot:<printer>/<head>) are materialised from it.
# fleet/policy.yaml (drying classes, later gates/calibrations): printops.policy reads /app/fleet.
COPY fleet/ /app/fleet/
# `3d printops token|backup …` inside the container (docker exec printops-api 3d printops …).
RUN chmod 0755 /app/bin/3d && ln -s /app/bin/3d /usr/local/bin/3d

USER printops
EXPOSE 8765
# Liveness only (/v1/live: API serves + DB opens; never calls Spoolman). Readiness, incl.
# Spoolman and the extra fields, is /v1/health, checked by `3d printops health` / `deploy`.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/v1/live', timeout=8).status == 200 else 1)"]
CMD ["python", "-m", "uvicorn", "--factory", "printops.api.app:app_from_env", "--host", "0.0.0.0", "--port", "8765", "--no-server-header"]
