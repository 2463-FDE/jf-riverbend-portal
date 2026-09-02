"""eligibility-service configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "eligibility-service"
    port = int(os.getenv("PORT", "8072"))
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    # Clearinghouse / payer REST shim that fronts the X12 270/271 exchange.
    payer_api_url = os.getenv("PAYER_API_URL", "https://edi.example.com/v1/eligibility")
    payer_api_key = os.getenv("PAYER_API_KEY", "")
    payer_name = os.getenv("PAYER_NAME", "edi.example.com")

    # W10 Final Stage 1 (RIV-088/141 follow-up): explicit mode, replacing the
    # old inference of "PAYER_API_KEY is blank => simulate". 'simulation'
    # (the default, and the only mode this training environment ever runs)
    # makes zero outbound payer calls, ever — see check.py::check. 'live'
    # requires a real endpoint and credential; payer_mode.validate() rejects
    # a missing or placeholder one before any network access is attempted.
    payer_integration_mode = os.getenv("PAYER_INTEGRATION_MODE", "simulation").strip().lower()

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # W10 Metrics Stage 4: this service's only Postgres access — durable
    # Bedrock usage accounting (bedrock_usage.py) into the same
    # bedrock_usage_events table records-service already writes. Same
    # defaults/variable names as every other service's identical block
    # (e.g. records-service/config.py) for one shared docker-compose.yml
    # environment block to satisfy.
    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # Shared secret proving a call came through the gateway. Defaults to
    # empty and is checked both per-request and at startup — an unset value
    # must fail closed, never be read as "no check needed".
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    # Stage 1 resilience (D4 / RIV-088 / RIV-141): bounded + retried + breaker-
    # guarded payer call, plus a Redis-backed last-known-good cache fallback.
    payer_timeout_seconds = float(os.getenv("ELIGIBILITY_PAYER_TIMEOUT_SECONDS", "5"))
    payer_max_retries = int(os.getenv("ELIGIBILITY_PAYER_MAX_RETRIES", "2"))
    breaker_failure_threshold = int(os.getenv("ELIGIBILITY_BREAKER_FAILURE_THRESHOLD", "5"))
    breaker_reset_timeout_seconds = float(os.getenv("ELIGIBILITY_BREAKER_RESET_SECONDS", "30"))
    cache_fresh_ttl_seconds = int(os.getenv("ELIGIBILITY_CACHE_FRESH_TTL_SECONDS", "300"))
    cache_stale_ttl_seconds = int(os.getenv("ELIGIBILITY_CACHE_STALE_TTL_SECONDS", "3600"))

    # Stage 3: Redis-backed eligibility job lifecycle (async /intake path,
    # RIV-088 / RIV-141). See jobs.py for the state machine and worker.py for
    # the in-process consumer.
    job_max_retries = int(os.getenv("ELIGIBILITY_JOB_MAX_RETRIES", "3"))
    job_max_manual_retries = int(os.getenv("ELIGIBILITY_JOB_MAX_MANUAL_RETRIES", "1"))
    job_status_ttl_seconds = int(os.getenv("ELIGIBILITY_JOB_STATUS_TTL_SECONDS", "3600"))
    job_lease_seconds = int(os.getenv("ELIGIBILITY_JOB_LEASE_SECONDS", "30"))
    worker_poll_interval_seconds = float(os.getenv("ELIGIBILITY_WORKER_POLL_INTERVAL_SECONDS", "0.5"))
    worker_reclaim_interval_seconds = float(os.getenv("ELIGIBILITY_WORKER_RECLAIM_INTERVAL_SECONDS", "15"))


settings = Settings()
