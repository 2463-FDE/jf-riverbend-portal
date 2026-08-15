"""scheduling-service configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "scheduling-service"
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # Shared secret proving a call came through the gateway. Defaults to
    # empty and is checked both per-request and at startup — an unset value
    # must fail closed, never be read as "no check needed".
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    # pagination guardrails for list endpoints
    default_page_limit = int(os.getenv("DEFAULT_PAGE_LIMIT", "50"))
    max_page_limit = int(os.getenv("MAX_PAGE_LIMIT", "200"))

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
