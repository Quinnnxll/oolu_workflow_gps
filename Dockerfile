# The self-host runner for online web users: the Workflow-GPS shell behind
# a shared access token, ready to sit behind your HTTPS reverse proxy.
#
#   docker build -t workflow-gps .
#   docker run -p 8765:8765 -e WFGPS_WEB_TOKEN=<long random secret> \
#       -v wfgps-data:/data workflow-gps
#
# Leave WFGPS_WEB_TOKEN unset and a one-time token is generated and printed
# in the container log. All state lives under /data — one volume to back up.
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[serve]"

VOLUME ["/data"]
EXPOSE 8765

CMD ["wfgps", "web", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--db", "/data/desktop.db", \
     "--registry", "/data/skills.db", \
     "--seed-starter"]
