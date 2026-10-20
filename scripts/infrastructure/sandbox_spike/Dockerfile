# S0 spike image (runbook items a, b, c, e, f, i). Build context is THIS directory only:
#   gcloud builds submit scripts/infrastructure/sandbox_spike --tag=<IMAGE>
# (named `Dockerfile` because `gcloud builds submit --tag` only builds a file of that name).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/pw NODE_PATH=/opt/docx/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir hypercorn quart "httpx[http2]" google-auth requests playwright \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw

# The docx version of docx_generator/package-lock.json (8.5.0); that file is outside this build context.
RUN mkdir -p /opt/docx && cd /opt/docx && npm install --no-audit --no-fund docx@8.5.0 && chmod -R a+rX /opt/docx

# The engine uid the spike's children run as. The server stays root (like the indexer) so it can switch uid.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin spikeengine

COPY . /srv
WORKDIR /srv
CMD ["python", "supervisor.py"]
