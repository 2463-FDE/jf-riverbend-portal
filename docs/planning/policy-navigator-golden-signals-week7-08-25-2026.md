# Policy navigator golden signals — Week 7 (2026-08-25)

**Status:** working-tree evidence; not yet reviewed or merged
**Scope:** the smallest real metric path for one existing safety-critical
component (`libs.policy_navigator.runtime.run_policy_navigator`), plus a
dashboard specification and one alert that reference it. No monitoring
platform, exporter, or new dependency is introduced.

## The gap this closes

Tracing (`libs/tracing`) and grounded-summary controls
(`libs/agent_provenance`) already exist, but nothing in this repo counted
*how often* the policy navigator's own safety net actually engaged — a
dashboard or alert written against an uncounted event would be inert by
construction. This closes that specific gap for one component, not
observability generally.

## The metric

`policy_navigator_termination_total` — a counter, incremented exactly once
per `run_policy_navigator` call, labelled by:

- `termination_reason`: `answered` | `no_evidence` | `citation_invalid` | `provider_error`
- `provenance_label`: `real` | `fixture` | `fallback`

Emitted via `libs.metrics.record_counter` (`libs/metrics/counters.py`) as one
structured, safe log line:

```
metric emitted: metric=policy_navigator_termination_total value=1 provenance_label=fallback termination_reason=citation_invalid
```

This is a real, produced signal today — any log-based metrics system already
available in this environment (CloudWatch Logs Insights, a self-hosted
ELK/Loki stack, or `grep`/`wc -l` against `docker compose logs
records-service` during a demo) can count these lines right now. No new
infrastructure is stood up to make that true.

Why this metric: `termination_reason` is the exact, already-defined outcome
space of a safety-critical component (the citation-validation safety net
described in `libs/policy_navigator/runtime.py`'s own module docstring) — its
error rate (`citation_invalid` + `provider_error` as a fraction of total) is
a genuine golden signal (errors), not a vanity count.

## Dashboard specification

A minimal, vendor-neutral panel grid. Each panel's `query` describes what to
count from the log line shape above — written generically (not tied to a
specific log-query language) since no query engine is deployed in this repo
to validate one syntax against; adapt directly to whichever the target log
store speaks (CloudWatch Logs Insights, Loki LogQL, etc.).

```json
{
  "dashboard": "policy-navigator-golden-signals",
  "metric": "policy_navigator_termination_total",
  "panels": [
    {
      "title": "Termination reason breakdown (rate)",
      "type": "stacked-bar",
      "group_by": "termination_reason",
      "window": "5m",
      "query": "count by termination_reason where metric = policy_navigator_termination_total"
    },
    {
      "title": "Error rate (citation_invalid + provider_error) / total",
      "type": "line",
      "window": "5m",
      "query": "ratio of (termination_reason in [citation_invalid, provider_error]) to (metric = policy_navigator_termination_total)"
    },
    {
      "title": "Fallback rate by provenance_label",
      "type": "line",
      "group_by": "provenance_label",
      "window": "15m",
      "query": "count by provenance_label where metric = policy_navigator_termination_total and provenance_label = fallback"
    }
  ]
}
```

## Alert

```json
{
  "alert": "policy-navigator-error-rate-high",
  "metric": "policy_navigator_termination_total",
  "condition": "ratio of (termination_reason in [citation_invalid, provider_error]) to (metric = policy_navigator_termination_total) over 15m",
  "threshold": 0.20,
  "comparison": "greater_than",
  "for": "15m",
  "severity": "warning",
  "description": "More than 20% of policy navigator turns over 15 minutes ended in citation_invalid or provider_error. citation_invalid at volume suggests the model is drifting from retrieved evidence; provider_error at volume suggests a Bedrock availability problem. Neither is a normal steady-state rate for this safety-critical path."
}
```

This alert references the exact metric name and label values
`run_policy_navigator` actually emits (verified by
`tests/test_policy_navigator_runtime.py::test_a_hallucinated_citation_increments_the_golden_signal_counter`)
— not a name invented for the dashboard alone.

## Explicitly out of scope

- No monitoring platform (Prometheus/Grafana/CloudWatch/etc.) is deployed or
  configured by this change.
- No metrics beyond this one component's termination outcomes are added.
- No alert-routing/paging integration is implemented — this is the
  specification, not a wired notification channel.
- Extending this pattern to other agent runtimes (summary agent,
  eligibility agent) is a separate, later decision, not scoped here.
