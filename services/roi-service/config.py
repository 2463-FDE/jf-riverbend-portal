"""roi-service configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "roi-service"
    port = int(os.getenv("PORT", "8076"))
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # Branch 7B: shared secret proving a call arrived through the gateway.
    # Same variable, same semantics as intake/records/eligibility/scheduling.
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
