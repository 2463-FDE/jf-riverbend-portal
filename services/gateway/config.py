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
    # creation. Override with ABSOLUTE_SESSION_TIMEOUT_SECONDS.
    #
    # 8h at the client's direction (2026-08-13), down from the 12h first
    # proposed: they want a fresh sign-in at shift handover, and would rather
    # anchor the cap to one shift and let the 15-minute idle do the routine
    # work. Revisit only with data on how often an active clinician actually
    # hits the cap mid-shift.
    #
    # For the record, since the reasoning behind that instruction was based on
    # a comparison that doesn't hold: the pre-existing 8h figure was the IDLE
    # timeout, not a maximum. Because every authenticated request refreshed
    # it, a continuously-used session had no upper bound at all before this
    # cap existed — so neither 12h nor 8h lengthens anything. Both shorten an
    # unbounded lifetime. 8h is simply the more conservative of the two.
    absolute_session_timeout_seconds = int(os.getenv("ABSOLUTE_SESSION_TIMEOUT_SECONDS", "28800"))

    # MFA rollout (w8-planner-2). How long a password-verified, not-yet-
    # completed challenge (security.create_mfa_challenge) stays valid — short
    # and single-purpose, unlike the session TTLs above. 5 minutes is enough
    # to read a code off an authenticator app without being long enough to
    # matter if a caller never returns to finish it.
    # `or "300"`, not the bare os.getenv default: docker-compose's env_file
    # passes a blank .env line through as an empty string, not "unset" —
    # int(os.getenv(..., "300")) would crash on int("") since the env var
    # IS set, just to nothing. Caught by bringing this service up against a
    # real compose stack; .env.example now ships a real default for this
    # var too, but this is the fix that actually closes the bug, not just
    # the template.
    mfa_challenge_timeout_seconds = int(os.getenv("MFA_CHALLENGE_TIMEOUT_SECONDS") or "300")

    # W9.3 — the same variable eligibility-service's own config.py reads
    # (shared via docker-compose's env_file: .env on every service).
    payer_api_key = os.getenv("PAYER_API_KEY", "")

    # W10 Final Stage 1: the same explicit, shared PAYER_INTEGRATION_MODE
    # eligibility-service's config.py reads (its own payer_mode.py is the
    # authoritative enforcement — see check.py::check). The gateway reads it
    # too so the coverage-verify route below can short-circuit locally
    # ("Synthetic training — no payer contacted") without a wasted round trip
    # to eligibility-service, replacing the old inference from a blank
    # PAYER_API_KEY.
    payer_integration_mode = os.getenv("PAYER_INTEGRATION_MODE", "simulation").strip().lower()

    # Demo-readiness slice: internal (compose-network) URLs for the local
    # observability POC's own readiness paths — only reachable when that
    # stack is actually started (`docker compose --profile observability up`,
    # profiles: ["observability"] in docker-compose.yml). No `:?` guard and
    # no compose wiring needed: these are read-only checks against sensible
    # compose-network defaults, and /observability/status treats an
    # unreachable dependency as a normal "unavailable" outcome, not a
    # configuration error.
    grafana_url = os.getenv("GRAFANA_URL", "http://grafana:3000")
    prometheus_url = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
    loki_url = os.getenv("LOKI_URL", "http://loki:3100")
    tempo_url = os.getenv("TEMPO_URL", "http://tempo:3200")

    # The browser-facing counterpart to grafana_url above — the host-published
    # port (docker-compose.yml's grafana service), for the dashboard links
    # /observability/status hands back to a presenter's own browser rather
    # than the compose-internal address used for the server-side health check.
    grafana_public_url = os.getenv("GRAFANA_PUBLIC_URL", "http://localhost:3000")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
