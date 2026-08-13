# The build context is the repo root (not ./services/gateway) so this image
# can COPY config/roles.yaml — the RBAC permission source that roles_config.py
# loads at runtime. It lives outside services/gateway/, so the old
# per-service context could never see it: the image built fine, /healthz and
# /login passed, and then the first request to any require_permission route
# raised FileNotFoundError and returned 500. Local pytest never caught it
# because tests run from the repo root, where the relative path resolves.
#
# Same pattern intake-service, eligibility-service and records-service already
# use to reach libs/ — see their Dockerfiles and docker-compose.yml.
FROM python:3.12-slim

WORKDIR /app

COPY services/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/gateway/ .

# roles_config.py resolves this as <module dir>/../../config/roles.yaml, so it
# must sit two levels above /app/roles_config.py — i.e. /config/roles.yaml.
# ROLES_CONFIG_PATH overrides the location if a deployment needs it elsewhere.
COPY config/roles.yaml /config/roles.yaml

EXPOSE 8070
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8070"]
