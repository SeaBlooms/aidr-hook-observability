# aidr-obs dashboard in remote mode: captures and Noma logs arrive from the Mac shipper
# (`aidr-obs ship`) over POST /api/push. The tap never runs here.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home aidr && mkdir -p /data && chown aidr /data && chmod 700 /data
WORKDIR /app
COPY --from=build /app /app

# server.py refuses 0.0.0.0 unless AIDR_OBS_CONTAINER=1; publish the port on the host's loopback only.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AIDR_OBS_CONTAINER=1 \
    AIDR_OBS_HOST=0.0.0.0 \
    AIDR_OBS_REMOTE=1 \
    AIDR_OBS_HOME=/data \
    AIDR_OBS_LOG_DIR=/data/noma

USER aidr
VOLUME /data
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/setup', timeout=4)" || exit 1
CMD ["aidr-obs", "serve", "--port", "8770"]
