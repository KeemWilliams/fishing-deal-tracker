# Fishing Gear Deal Tracker — plain HTTP fetch path only.
#
# Do NOT run `scrapling install` or add Playwright/browser binaries here.
# requirements.txt pins scrapling[fetchers] but the pipeline is designed to
# run without any browser install (architecture doc: HTTP-only path, stealth
# escalation is a config flag decided later by an owner, not a build-time
# default). Adding browser binaries would roughly triple image size for a
# capability this image must never use.
#
# Base image version: matches the local dev interpreter (3.12) rather than
# the workstation's system Python (3.14) — psycopg[binary] and scrapling's
# compiled dependencies (lxml et al.) do not yet ship 3.14 manylinux wheels,
# which would force a from-source compile in the image. pyproject.toml also
# declares requires-python = ">=3.12". Bump this once 3.14 wheel coverage is
# confirmed for every pin in requirements.txt.
FROM python:3.12-slim AS builder

WORKDIR /app

# Build deps only in this stage; nothing here ships in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime

# Non-root runtime user.
RUN groupadd --system fpt && useradd --system --gid fpt --home-dir /app --shell /usr/sbin/nologin fpt

WORKDIR /app

COPY --from=builder /install /usr/local

COPY run.py ./
COPY fpt ./fpt
COPY config ./config
COPY db ./db
COPY deploy ./deploy

# Snapshot dir must be writable by the non-root user (fpt/fetch/snapshots.py
# defaults FPT_SNAPSHOT_DIR to /var/lib/fpt/snapshots).
RUN mkdir -p /var/lib/fpt/snapshots && chown -R fpt:fpt /var/lib/fpt /app

USER fpt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FPT_SNAPSHOT_DIR=/var/lib/fpt/snapshots

ENTRYPOINT ["python", "run.py"]
CMD ["tick"]
