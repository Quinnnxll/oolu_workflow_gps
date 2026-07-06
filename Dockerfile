# The self-host runner for online web users: the OoLu multi-user
# gateway (local accounts), ready to sit behind your HTTPS reverse proxy.
#
#   docker build -t oolu .
#   docker run -p 8765:8765 \
#       -e OOLU_HOST_SECRET=<long random secret> \
#       -e OOLU_ADMIN_PASSWORD=<the first admin's password> \
#       -v oolu-data:/data oolu
#
# Leave OOLU_ADMIN_PASSWORD unset and a one-time password is generated and
# printed in the container log. All state lives under /data — one volume to
# back up. Sign in at POST /v1/auth/login (or the browser page at /).
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[serve]"

VOLUME ["/data"]
EXPOSE 8765

CMD ["oolu", "host", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--data", "/data"]
