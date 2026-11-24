FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN useradd --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app /opt/upstream /data \
 && python -m venv /opt/venv \
 && chown -R app:app /opt/upstream /opt/venv /data

COPY app/ /app/
WORKDIR /app
USER app

ENV DATA_DIR=/data \
    UPSTREAM_DIR=/opt/upstream \
    UPSTREAM_PYTHON=/opt/venv/bin/python \
    PORT=8080
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
