# Multi-stage build: a builder stage compiles the venv and installs the
# package, and the runtime stage copies only the finished venv and source
# across. This keeps compilers, pip's cache and the git checkout's cruft out
# of the image that actually ships -- nobody running this in prod needs a
# C compiler along for the ride.

# ---- builder ----------------------------------------------------------
FROM python:3.14-slim AS builder

WORKDIR /app

RUN python -m venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

# requirements.txt first, source second: Docker's layer cache only
# invalidates the (slow) dependency install when the lock file actually
# changes, not every time a docstring gets a better joke.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml .
COPY newsbot ./newsbot
RUN pip install --no-cache-dir --no-deps .

# ---- runtime ------------------------------------------------------------
FROM python:3.14-slim AS runtime

# uid 10001 is fixed (not "the next free uid") so the bind-mounted ./data
# on the host can be chown'd once, in the runbook, and keep working across
# rebuilds -- see docs/deploy.md.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin newsbot \
    && mkdir /data \
    && chown 10001:10001 /data

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY newsbot ./newsbot

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# The Debian base image's sqlite3 has FTS5 compiled in, and so does the
# owner's Homebrew Python on macOS -- but "usually true" is exactly the
# kind of assumption that survives right up until it doesn't. Fail the
# build here, loudly, rather than fail a `/news search` at 9:05 a.m.
RUN python -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(a)')"

USER newsbot

# There's no port to poll (the gateway connection is outbound-only), so
# health is judged by a heartbeat file the running process rewrites every
# 60s -- see newsbot/healthcheck.py for the actual staleness logic and why
# it exists.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -m newsbot.healthcheck

CMD ["python", "-m", "newsbot"]
