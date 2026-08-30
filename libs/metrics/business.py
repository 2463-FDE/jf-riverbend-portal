"""Real Prometheus business-outcome counters (W10 Final Stage 6, sub-slices
2-4). Defined here, not inline in each service's app.py, so the underlying
Counter objects are created exactly once per process — `conftest.py`'s
`load_module` re-execs a fresh copy of app.py per test file, and a second
`Counter(...)` registration of the same metric name in Prometheus's default
registry raises `DuplicateTimeseriesError`. Importing this module is a
normal, cached Python import (never re-exec'd), so every fresh app.py copy
shares the SAME counter objects — exactly like libs/metrics/http.py's own
request-count/latency/in-flight metrics already do.

Every label is a bounded BUSINESS OUTCOME category, never patient/
request/reason content.
"""
from prometheus_client import Counter

# outcome in {"success", "rejected", "retry", "failure"} — "retry" covers
# both the ordinary already-fulfilled 409 and its IntegrityError backstop
# (see services/roi-service/app.py::fulfill_roi_request).
ROI_FULFILLMENT_OUTCOMES = Counter(
    "roi_fulfillment_outcomes_total", "ROI fulfillment attempts by business outcome", ["outcome"],
)

# outcome in {"success", "conflict", "retry", "failure"} — "conflict" covers
# both a genuinely taken slot and a reused idempotency_key for different
# booking details (see services/scheduling-service/app.py::create_appointment).
SCHEDULING_BOOKING_OUTCOMES = Counter(
    "scheduling_booking_outcomes_total", "Appointment booking attempts by business outcome", ["outcome"],
)

# No labels: this route's own identity IS the label (see
# services/records-service/app.py::get_patient_records). DEBT D8 (the N+1
# read pattern this counter was originally named for) was batched in Stage
# 7 sub-slice 4 after live smoke evidence proved the route is still called
# by the current frontend — the counter's name/identity is kept unchanged
# so any dashboard/alert referencing it stays valid; it now measures calls
# to the batched chart-assembly path.
RECORDS_LEGACY_N_PLUS_ONE_CHART_READS = Counter(
    "records_legacy_chart_n_plus_one_total", "Reads of the (now-batched) legacy chart-assembly path, formerly N+1 (DEBT D8)",
)
