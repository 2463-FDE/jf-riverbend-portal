# Stage 3 (RIV-088/RIV-141 fix): this service now imports libs/ (libs.tracing)
# — a shared package that lives at the repo root, outside
# services/intake-service/. The old context (./services/intake-service,
# per-service COPY . .) could never see it. The build context is now the
# repo root (see docker-compose.yml's build.context for this service) so
# libs/ can be copied in explicitly, alongside this service's own files. See
# adr/0005-eligibility-agent-runtime-and-resilience.md for the full writeup.
FROM python:3.12-slim

WORKDIR /app

COPY services/intake-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/intake-service/ .
COPY libs/ ./libs/

EXPOSE 8071
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8071"]
