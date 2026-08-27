# Stage 3: this service now imports libs/ (libs.patient_view_agent,
# libs.safe_logging) — a shared package that lives at the repo root, outside
# services/records-service/. The old context (./services/records-service,
# per-service COPY . .) could never see it. The build context is now the
# repo root (see docker-compose.yml's build.context for this service) so
# libs/ can be copied in explicitly, alongside this service's own files —
# mirroring services/intake-service/Dockerfile and
# services/eligibility-service/Dockerfile.
# P5 follow-up, item 3: python:3.12-slim floats to whatever Debian release is
# current (trixie/13 as of this pin, which still carries HIGH-severity
# util-linux/bsdutils/OpenSSL findings). python:3.12-slim-bookworm (Debian
# 12) scanned fully clean (0 CRITICAL/HIGH) at pin time; explicit variant +
# digest so this doesn't silently float again.
FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

WORKDIR /app

COPY services/records-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/records-service/ .
COPY libs/ ./libs/

# adr/0012 follow-up ("agent draft text encryption"), mirroring
# services/intake-service/Dockerfile's identical copy of
# encrypt_existing_phi.py: records-service owns
# agent_draft_provenance.generated_text end to end (it already has
# libs/phi_crypto, cryptography, and DB connectivity), so `make
# phi-backfill` can run this script via `docker compose exec
# records-service ...` instead of standing up a separate runner.
COPY db/migrations/scripts/encrypt_agent_draft_text.py ./db/migrations/scripts/encrypt_agent_draft_text.py

# roles_config.py resolves <module dir>/../../config/roles.yaml, so the signed
# permission matrix must sit two levels above /app/roles_config.py — i.e.
# /config/roles.yaml. Without it every authorized route would fail at request
# time; the lifespan check refuses to start instead. ROLES_CONFIG_PATH
# overrides the location.
COPY config/roles.yaml /config/roles.yaml

EXPOSE 8073
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8073"]
