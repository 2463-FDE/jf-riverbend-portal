# Stage 3: this service now imports libs/ (libs.patient_view_agent,
# libs.safe_logging) — a shared package that lives at the repo root, outside
# services/records-service/. The old context (./services/records-service,
# per-service COPY . .) could never see it. The build context is now the
# repo root (see docker-compose.yml's build.context for this service) so
# libs/ can be copied in explicitly, alongside this service's own files —
# mirroring services/intake-service/Dockerfile and
# services/eligibility-service/Dockerfile.
FROM python:3.12-slim

WORKDIR /app

COPY services/records-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/records-service/ .
COPY libs/ ./libs/

# roles_config.py resolves <module dir>/../../config/roles.yaml, so the signed
# permission matrix must sit two levels above /app/roles_config.py — i.e.
# /config/roles.yaml. Without it every authorized route would fail at request
# time; the lifespan check refuses to start instead. ROLES_CONFIG_PATH
# overrides the location.
COPY config/roles.yaml /config/roles.yaml

EXPOSE 8073
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8073"]
