# OBS-M02 (Stage 7 smoke-test discovery): this service has imported libs/
# (libs.safe_logging) since Stage 3 — a shared package that lives at the
# repo root, outside services/interop-service/. The old context
# (./services/interop-service, per-service COPY . .) could never see it:
# the image built fine, but the container crashed at startup with
# ModuleNotFoundError, invisible to CI (image build only, never runs the
# container) and to pytest (imports directly from the repo checkout). This
# bug predates Stage 6 — it was never specific to the metrics work. The
# build context is now the repo root (see docker-compose.yml's
# build.context for this service) so libs/ can be copied in explicitly —
# mirroring services/records-service/Dockerfile and the others.
# P5 follow-up, item 3: python:3.12-slim floats to whatever Debian release is
# current (trixie/13 as of this pin, which still carries HIGH-severity
# util-linux/bsdutils/OpenSSL findings). python:3.12-slim-bookworm (Debian
# 12) scanned fully clean (0 CRITICAL/HIGH) at pin time; explicit variant +
# digest so this doesn't silently float again.
FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

WORKDIR /app

COPY services/interop-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/interop-service/ .
COPY libs/ ./libs/

EXPOSE 8075
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8075"]
