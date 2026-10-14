# syntax=docker/dockerfile:1.7
# Images for Neura services. Build from the repository root:
#   docker build -f deploy/docker/Dockerfile --target neura-control -t neura-control .
#   docker build -f deploy/docker/Dockerfile --target neura-agent -t neura-agent .

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
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --all-packages --no-install-workspace
COPY packages packages
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    uv sync --frozen --no-dev --all-packages --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /app --shell /usr/sbin/nologin neura
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
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
