"""interop-service configuration. Environment-driven; sensible compose defaults.

This service has no database — it parses inbound HL7 v2 messages into our
internal record shape and returns them. No db.py / models here by design.
"""
import os


class Settings:
    service_name = "interop-service"
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    # guardrail for inbound message size (bytes of the raw HL7 string)
    max_message_bytes = int(os.getenv("MAX_MESSAGE_BYTES", "262144"))

    # Branch 7B: shared secret proving a call arrived through the gateway.
    # Same variable, same semantics as intake/records/eligibility/scheduling.
    internal_service_token = os.getenv("INTERNAL_SERVICE_TOKEN", "")


settings = Settings()
