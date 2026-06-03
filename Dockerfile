# tao-sentinel container image.
# Multi-stage build: the `builder` stage compiles the package into a wheel; the
# `runtime` stage installs only that wheel (no tests, no dev deps) onto a clean
# python:3.12-slim base, runs as the non-root `sentinel` user, and defaults to
# the read-only web dashboard. STOPSIGNAL is SIGINT because the app handles
# SIGINT cleanly (prints "Stopped.") whereas SIGTERM would hard-kill it.

# ---- builder stage: produce a wheel for tao-sentinel -----------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Copy the sources needed to build the wheel. The .dockerignore keeps tests,
# .git, build artifacts, etc. out of the context.
COPY pyproject.toml README.md LICENSE ./
COPY tao_sentinel ./tao_sentinel

# Build the project (and its runtime dependencies) into wheels under /wheels.
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime stage: install only the wheel ---------------------------------
FROM python:3.12-slim AS runtime

# Don't write .pyc files and keep stdout/stderr unbuffered so container logs
# appear in real time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create a non-root user (uid 1000) to run the app.
RUN groupadd --gid 1000 sentinel \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin sentinel

WORKDIR /app

# Install the prebuilt wheel(s) from the builder stage. Installing tao-sentinel
# from the wheel pulls in its declared runtime dependencies (also present as
# wheels) without copying any source tree, tests, or dev tooling into the image.
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels tao-sentinel \
    && rm -rf /wheels

# Data directory for the JSON state file (matches the example config's
# state_path: /data/state.json). Owned by the runtime user so the app can write
# it even when no named volume is mounted.
RUN mkdir -p /data && chown sentinel:sentinel /data

USER sentinel

# The dashboard listens here; published via the compose `proxy` service, not
# directly.
EXPOSE 8787

# Healthcheck hits the JSON status endpoint. curl is not in python:3.12-slim, so
# use the bundled Python + urllib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/status', timeout=4).status == 200 else 1)"

# The app handles SIGINT cleanly; SIGTERM would hard-kill it mid-poll.
STOPSIGNAL SIGINT

# Exec form so signals reach the process directly (PID 1).
ENTRYPOINT ["tao-sentinel"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
