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

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # Stage 1 resilience (D4 / RIV-088 / RIV-141): bounded + retried + breaker-
    # guarded payer call, plus a Redis-backed last-known-good cache fallback.
    payer_timeout_seconds = float(os.getenv("ELIGIBILITY_PAYER_TIMEOUT_SECONDS", "5"))
    payer_max_retries = int(os.getenv("ELIGIBILITY_PAYER_MAX_RETRIES", "2"))
    breaker_failure_threshold = int(os.getenv("ELIGIBILITY_BREAKER_FAILURE_THRESHOLD", "5"))
    breaker_reset_timeout_seconds = float(os.getenv("ELIGIBILITY_BREAKER_RESET_SECONDS", "30"))
    cache_fresh_ttl_seconds = int(os.getenv("ELIGIBILITY_CACHE_FRESH_TTL_SECONDS", "300"))
    cache_stale_ttl_seconds = int(os.getenv("ELIGIBILITY_CACHE_STALE_TTL_SECONDS", "3600"))


settings = Settings()
