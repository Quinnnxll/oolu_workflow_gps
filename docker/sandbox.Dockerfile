# syntax=docker/dockerfile:1
#
# Workflow-GPS sandbox image — the hostile-execution base for LocalDockerBackend.
#
# Build from the REPO ROOT (the entrypoint COPY needs that context):
#     docker build -f docker/sandbox.Dockerfile -t workflow-gps-sandbox:latest .
#
# The image is intentionally minimal: Python + uv + a non-root user + the in-container
# entrypoint. Everything else (dependencies, the result shim, the user script) is
# injected per-run by the backend. The backend runs the container with a read-only
# rootfs, dropped capabilities, no-new-privileges, resource limits, and a tmpfs at
# /sandbox — so this image only has to provide the tools and the writable-path config.

FROM python:3.12-slim

# uv from Astral's official image — no pip bootstrap needed. Pin the minor for
# reproducibility; bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Non-root user. Hostile code must never run as root, even inside the container.
RUN useradd --create-home --uid 10001 sandbox \
    && mkdir -p /opt/wfgps

# Bake the in-container runner (structured exception reporting). Static, so it lives
# in the image rather than being copied per-run.
COPY docker/entrypoint.py /opt/wfgps/entrypoint.py

# CRITICAL: at runtime the rootfs is mounted READ-ONLY and the only writable mount is
# the /sandbox tmpfs. So every tool that needs to write — uv's cache, the temp dir,
# HOME — must point INTO /sandbox, or Phase A installs fail with permission errors.
# The backend creates these subdirs in the tmpfs before the first exec.
ENV HOME=/sandbox \
    UV_CACHE_DIR=/sandbox/.cache/uv \
    TMPDIR=/sandbox/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_PROGRESS=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /sandbox
USER sandbox

# Placeholder command. The backend overrides this with a keep-alive `sleep` and then
# drives Phase A (uv install) and Phase B (entrypoint) through `docker exec`.
CMD ["sleep", "3600"]
