# Multi-stage build: dependencies are installed with uv in a throwaway
# "builder" stage, then only the finished virtual environment is copied into
# the runtime image.  The runtime image therefore never contains uv, the
# uv cache, or build tooling — only Python, ffmpeg, the venv, and the app.

# ── Stage 1: build the virtual environment ────────────────────────────────────
FROM python:3.14-slim-bookworm AS builder

# uv is a fast Python package installer; pin the major/minor version so
# builds stay reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# UV_COMPILE_BYTECODE: pre-compile .pyc files so container startup is faster.
# UV_LINK_MODE=copy: copy files instead of hardlinking (hardlinks don't work
#   across the cache mount below).
# UV_PYTHON_DOWNLOADS=never: always use the image's own Python, never download
#   a separate interpreter.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies from the lockfile only — the app code is copied later,
# so editing app code never invalidates this (slow) layer.  The cache mount
# keeps downloaded wheels between builds.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm

# ffmpeg is needed to encode the generated speech to MP3.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Run as a fixed-ID unprivileged user (UID/GID 1000 matches the default first
# user on most Linux hosts, so bind-mounted files keep sane ownership).
RUN groupadd --gid 1000 voxer \
    && useradd --uid 1000 --gid 1000 --create-home voxer

WORKDIR /app

# The virtual environment built in stage 1.
COPY --from=builder --chown=voxer:voxer /app/.venv /app/.venv

# Application code and bundled assets.  Voice styles and emote sounds are
# baked into the image so an image pulled from a registry works without a
# checkout of this repository; docker-compose.yml shows how to override them
# with bind mounts if you want to customize.
COPY --chown=voxer:voxer voxer/ voxer/
COPY --chown=voxer:voxer main.py ./
COPY --chown=voxer:voxer voices/ voices/
COPY --chown=voxer:voxer emotes/ emotes/

# /data holds runtime state (voice assignments, OAuth tokens, audio); /home/voxer/.cache is the mountpoint for the
# tts-cache volume — creating it voxer-owned here means Docker copies that
# ownership into the freshly created named volume, so UID 1000 can write the
# Supertonic model cache.
RUN mkdir -p /data /home/voxer/.cache && chown voxer:voxer /data /home/voxer/.cache

# Put the venv first on PATH so "python" resolves to the venv interpreter.
ENV PATH=/app/.venv/bin:$PATH

USER voxer

# 8080: overlay web server.  4343: one-time OAuth callback server used on
# first start to authorize the bot account.
EXPOSE 8080 4343

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)"]

# Run the venv's python directly as PID 1 (no uv wrapper in the runtime image).
CMD ["python", "main.py"]
