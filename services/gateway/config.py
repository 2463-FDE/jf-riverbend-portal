"""Gateway configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "gateway"
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # downstream services
    intake_url = os.getenv("INTAKE_URL", "http://intake-service:8071")
    eligibility_url = os.getenv("ELIGIBILITY_URL", "http://eligibility-service:8072")
    records_url = os.getenv("RECORDS_URL", "http://records-service:8073")
    scheduling_url = os.getenv("SCHEDULING_URL", "http://scheduling-service:8074")
    interop_url = os.getenv("INTEROP_URL", "http://interop-service:8075")
    roi_url = os.getenv("ROI_URL", "http://roi-service:8076")

    # Review fix (round, 2026-08-05): shared secret proving a /patients/{id}/view
    # call actually came from this gateway, not a direct caller hitting
    # records-service's published host port (docker-compose.yml) with a
    # spoofed X-Actor-Id. Empty by default — records-service fails closed
    # (denies everyone) until a real value is set in .env on both services.
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    # Idle timeout, refreshed on each authenticated request. Sessions once
    # never expired at all (PR #23 added this TTL), and then defaulted to
    # 28800 — 8h, long enough that a shared clinical workstation stayed
    # signed in across an entire shift, which is what the client raised.
    # Now 15 minutes: the value proposed to them for sign-off, and they may
    # come back with 30. Env-driven either way, so settling on a different
    # number is a config change, not a code change.
    session_timeout_seconds = int(os.getenv("SESSION_TIMEOUT_SECONDS", "900"))

    # The idle TTL above only lapses an ABANDONED session — one used at least
    # once per idle window would live forever, since every read refreshes it.
    # This is a separate absolute cap on total lifetime regardless of
    # activity, enforced at lookup (security.get_session), not just at
    # creation. Now 12h — one shift, so a handover forces a fresh sign-in
    # even on a machine that was never idle. Override with
    # ABSOLUTE_SESSION_TIMEOUT_SECONDS.
    absolute_session_timeout_seconds = int(os.getenv("ABSOLUTE_SESSION_TIMEOUT_SECONDS", "43200"))

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
