"""intake-service configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "intake-service"
    port = int(os.getenv("PORT", "8071"))
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # downstream eligibility verification: Stage 3 enqueues an async job on
    # eligibility-service instead of blocking /intake on the payer round-trip
    # (RIV-088 / RIV-141). This timeout only bounds the fast enqueue call
    # itself, never the payer check.
    eligibility_url = os.getenv("ELIGIBILITY_URL", "http://eligibility-service:8072")
    eligibility_job_enqueue_timeout_seconds = float(
        os.getenv("ELIGIBILITY_JOB_ENQUEUE_TIMEOUT_SECONDS", "3")
    )

    # payer settings kept for parity with the legacy module; the real X12 270/271
    # round-trip is owned by eligibility-service.
    payer_api_url = os.getenv("PAYER_API_URL", "https://edi.example.com/v1/eligibility")
    payer_api_key = os.getenv("PAYER_API_KEY", "")

    # Review fix (round 11, 2026-08-05): the gateway already gates /intake with
    # a real staff session (require_session), but intake-service itself has no
    # auth of its own and is directly reachable on its published host port
    # (docker-compose.yml) — bypassing that session check entirely. Same
    # shared secret already used to gate records-service's patient-view route
    # (services/records-service/config.py); empty by default so the route
    # fails closed (denies everyone) until a real value is set in .env.
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
