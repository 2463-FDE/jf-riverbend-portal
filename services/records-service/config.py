"""records-service configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "records-service"
    port = int(os.getenv("PORT", "8073"))
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # Review fix (round, 2026-08-05): GET /patients/{id}/view must verify this
    # request actually came from the gateway, not a caller hitting this
    # service's published host port (docker-compose.yml) directly with a
    # spoofed X-Actor-Id. Empty by default so the route fails closed (denies
    # everyone) until a real shared value is set in .env on both services —
    # an unset/mismatched token must never be treated as "no check configured".
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
