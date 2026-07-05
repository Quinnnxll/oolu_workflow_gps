# The self-host runner for online web users: the Workflow-GPS multi-user
# gateway (local accounts), ready to sit behind your HTTPS reverse proxy.
#
#   docker build -t workflow-gps .
#   docker run -p 8765:8765 \
#       -e WFGPS_HOST_SECRET=<long random secret> \
#       -e WFGPS_ADMIN_PASSWORD=<the first admin's password> \
#       -v wfgps-data:/data workflow-gps
#
# Leave WFGPS_ADMIN_PASSWORD unset and a one-time password is generated and
# printed in the container log. All state lives under /data — one volume to
# back up. Sign in at POST /v1/auth/login (or the browser page at /).
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[serve]"

VOLUME ["/data"]
EXPOSE 8765

CMD ["wfgps", "host", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--data", "/data"]
