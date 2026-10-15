# syntax=docker/dockerfile:1.7
# Images for Neura services. Build from the repository root:
#   docker build -f deploy/docker/Dockerfile --target neura-control -t neura-control .
#   docker build -f deploy/docker/Dockerfile --target neura-agent -t neura-agent .
#   docker build -f deploy/docker/Dockerfile --target neura-agent-gpu -t neura-agent-gpu .
#   docker build -f deploy/docker/Dockerfile --target neura-console -t neura-console .

ARG PYTHON_IMAGE=python:3.13-slim

# Behind a TLS-intercepting proxy, pass its CA bundle as a build secret:
#   docker build --secret id=ca_bundle,src=/path/to/ca.pem ...
# The bundle is used only during the build and is not copied into any image layer.

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.11.32
SHELL ["/bin/sh", "-eu", "-c"]
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir --root-user-action=ignore "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first, so source changes do not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
COPY packages/neura-contracts/pyproject.toml packages/neura-contracts/README.md packages/neura-contracts/
COPY packages/neura-proto/pyproject.toml packages/neura-proto/README.md packages/neura-proto/
COPY packages/neura-control/pyproject.toml packages/neura-control/README.md packages/neura-control/
COPY packages/neura-agent/pyproject.toml packages/neura-agent/README.md packages/neura-agent/
COPY packages/neura-runtime/pyproject.toml packages/neura-runtime/README.md packages/neura-runtime/
COPY packages/neura-server/pyproject.toml packages/neura-server/README.md packages/neura-server/
COPY packages/neura-sim/pyproject.toml packages/neura-sim/README.md packages/neura-sim/
COPY packages/neura-sdk/pyproject.toml packages/neura-sdk/README.md packages/neura-sdk/
COPY packages/neura-adapters/pyproject.toml packages/neura-adapters/README.md packages/neura-adapters/
COPY packages/neura-bench/pyproject.toml packages/neura-bench/README.md packages/neura-bench/
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --all-packages --no-install-workspace
COPY packages packages
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --all-packages --no-editable

# The same environment plus PyTorch (with its CUDA libraries) and LeRobot: the models group.
FROM build AS build-models
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --group models --all-packages --no-editable

FROM ${PYTHON_IMAGE} AS runtime-base
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /app --shell /usr/sbin/nologin neura \
    && mkdir -p /var/lib/neura && chown neura:neura /var/lib/neura
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

FROM runtime-base AS runtime
COPY --from=build /app/.venv /app/.venv
USER neura

FROM runtime-base AS runtime-models
COPY --from=build-models /app/.venv /app/.venv
USER neura

FROM runtime AS neura-control
COPY --chown=neura:neura api /app/api
ENV NEURA_CONTROL_HOST=0.0.0.0 \
    NEURA_CONTROL_OPENAPI_PATH=/app/api/openapi.yaml
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0)"]
CMD ["neura-control"]

FROM runtime AS neura-agent
ENV NEURA_AGENT_LOCAL_HOST=0.0.0.0
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2); sys.exit(0)"]
CMD ["neura-agent"]

# Site agent for GPU servers: its policy workers load real models (PyTorch, LeRobot) on the GPUs
# the agent may use. Run with the NVIDIA container toolkit (`--gpus all`).
FROM runtime-models AS neura-agent-gpu
ENV NEURA_AGENT_LOCAL_HOST=0.0.0.0
EXPOSE 8090 50051
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2); sys.exit(0)"]
CMD ["neura-agent"]

FROM runtime AS neura-gateway
# Standalone gateway (without a site agent): mount a configuration at this path.
ENV NEURA_GATEWAY_CONFIG=/etc/neura/site.yaml
EXPOSE 50051 8091
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8091/healthz', timeout=2); sys.exit(0)"]
CMD ["neura-gateway"]

FROM runtime AS neura-sim-robots
WORKDIR /srv/neura
CMD ["neura-sim-robots"]

# --- web console --------------------------------------------------------------------------------
FROM node:22.20-alpine AS console-build
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    corepack enable && corepack prepare pnpm@10.28.0 --activate
WORKDIR /src/console
COPY console/package.json console/pnpm-lock.yaml ./
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    pnpm install --frozen-lockfile
COPY console ./
COPY api /src/api
RUN pnpm run build

FROM nginx:1.27-alpine AS neura-console
COPY deploy/console/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=console-build /src/console/dist /usr/share/nginx/html
ENV NEURA_API_UPSTREAM=http://control-plane:8080 \
    NEURA_CSP_CONNECT_EXTRA=""
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD ["wget", "-qO-", "http://127.0.0.1:8080/healthz"]
