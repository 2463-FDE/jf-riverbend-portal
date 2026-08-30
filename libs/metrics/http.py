"""Real, scrapeable Prometheus HTTP request metrics (W10 Final Stage 6).

Request count, latency, and in-flight work — the four services this stage
names (gateway, records, scheduling, ROI) each call `install_http_metrics`
once, near app creation. Every label is drawn from a small, bounded set:
`service` (one fixed string per process), `method` (an HTTP verb),
`route` (the MATCHED ROUTE TEMPLATE, e.g. "/patients/{patient_id}/records"
— never the raw resolved path, which could carry a patient/user id), and
`status_class` ("2xx"/"4xx"/"5xx"/...). No label here is ever request
content, a patient/user id, or a correlation id — see
tests/test_http_metrics.py's static label-name assertion.

This is a SEPARATE, real Prometheus registry — distinct from
`libs.metrics.record_counter`'s structured-log-line counters (still used
unchanged by libs/policy_navigator's golden-signal counter). Stage 6
requires an actually scrapeable `/metrics` endpoint, which a log line
cannot provide.
"""
from time import monotonic

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests handled", ["service", "method", "route", "status_class"],
)
REQUEST_LATENCY_SECONDS = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds",
    ["service", "method", "route", "status_class"],
)
REQUESTS_IN_FLIGHT = Gauge(
    "http_requests_in_flight", "HTTP requests currently being handled", ["service"],
)

# A request whose path matched no registered route (a 404 probe) gets this
# fixed label instead of the raw path — bounds cardinality against an
# attacker scanning arbitrary paths, and never leaks what they tried.
_UNMATCHED_ROUTE = "unmatched"


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else _UNMATCHED_ROUTE


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def _dispatch_for(service: str):
    async def dispatch(request: Request, call_next):
        REQUESTS_IN_FLIGHT.labels(service=service).inc()
        started = monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception:
                route = _route_template(request)
                REQUEST_LATENCY_SECONDS.labels(
                    service=service, method=request.method, route=route, status_class="5xx",
                ).observe(monotonic() - started)
                REQUEST_COUNT.labels(service=service, method=request.method, route=route, status_class="5xx").inc()
                raise
        finally:
            REQUESTS_IN_FLIGHT.labels(service=service).dec()

        route = _route_template(request)
        status_class = _status_class(response.status_code)
        REQUEST_LATENCY_SECONDS.labels(
            service=service, method=request.method, route=route, status_class=status_class,
        ).observe(monotonic() - started)
        REQUEST_COUNT.labels(service=service, method=request.method, route=route, status_class=status_class).inc()
        return response

    return dispatch


def install_http_metrics(app, service: str) -> None:
    """Registers the request-count/latency/in-flight middleware on `app`,
    labeled by `service`. Does NOT register a `/metrics` route — each
    service does that itself (see `metrics_response`), at whatever point
    in its own file its internal-token dependency is already defined, so
    `/metrics` can carry the exact same `Depends(_verify_internal_token)`
    every other non-healthcheck route already does."""
    app.add_middleware(BaseHTTPMiddleware, dispatch=_dispatch_for(service))


def metrics_response() -> Response:
    """The `/metrics` scrape response body — call this from a route each
    service registers itself, e.g.:

        @app.get("/metrics", dependencies=[Depends(_verify_internal_token)])
        def metrics():
            return metrics_response()
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
