# Coverage and Eligibility Status Guide

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL, LEGAL, FINANCIAL, OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Document ID | GUIDE-COVERAGE-ELIG-001 |
| Version | 1.1 |
| Status | Approved for Training Demo |
| Effective Date | August 23, 2026 |
| Owner | Patient Access / Eligibility Operations |
| Review Date | August 23, 2027 |
| Applies To | Eligibility, Benefits, Scheduling, Patient Access |
| Topic | Coverage Status, Runtime Availability, and Benefit Interpretation |

## 1. Purpose

This guide defines how the synthetic training system represents **coverage status**, **runtime/UI state**, and **covered benefits**.

The goal is to prevent staff, patients, and AI-assisted workflows from confusing:

1. a durable coverage determination,
2. a temporary system or display condition, and
3. a benefit determination.

These are separate concepts and must remain separate in application logic, storage, APIs, and user-facing language.

## 2. Core Model

The system uses three distinct concepts:

```text
Coverage status
→ durable eligibility meaning

Runtime/UI category
→ temporary condition describing how or whether a result was obtained

Benefit information
→ service-specific coverage rules, limits, cost sharing, and authorization requirements
```

### Durable coverage statuses

Only these values represent durable coverage status:

```text
active
inactive
unknown
pending
stale
```

### Transient runtime/UI categories

These values are **not** coverage statuses:

```text
simulated
unavailable
```

They describe how the current check is being presented or why a usable current result is not available.

## 3. Durable Coverage Status: `active`

### Meaning

`active` means the authoritative eligibility source reports active coverage for the checked member and date or period.

Safe wording:

> The eligibility check returned an active status.

It does not mean:

```text
Every service is covered.
The claim will be paid.
No prior authorization is required.
The patient's cost is zero.
```

## 4. Durable Coverage Status: `inactive`

### Meaning

`inactive` means the authoritative eligibility source reports that coverage is not active for the checked date or period.

Safe wording:

> The eligibility check returned an inactive status.

The system must not invent a reason for the inactive status.

## 5. Durable Coverage Status: `unknown`

### Meaning

`unknown` means the system does not have enough authoritative evidence to determine whether coverage is active or inactive.

Examples may include:

- No prior eligibility determination exists.
- The available payer response does not establish coverage state.
- Required member or plan information is incomplete.
- Conflicting authoritative information has not been resolved.

Safe wording:

> Current eligibility status is unknown.

The system must not convert `unknown` into `active` or `inactive` by inference.

## 6. Durable Coverage Status: `pending`

### Meaning

`pending` means an eligibility determination has been requested or started, but the workflow has not produced a final coverage determination.

```text
pending
≠ active
pending
≠ inactive
```

Safe wording:

> Eligibility verification is still in progress.

When a final authoritative result arrives, the durable status may transition from `pending` to another permitted status.

## 7. Durable Coverage Status: `stale`

### Meaning

`stale` means a prior coverage determination exists but is older than the configured freshness period for the current workflow.

Example:

```text
previous durable result: active
current durable status: stale
previous_checked_on: 2026-08-01
```

A stale result may preserve historical provenance, but it must not be silently presented as current coverage.

Safe wording:

> A previous eligibility result exists, but it is too old to rely on for this request.

## 8. Durable Status Summary

| Durable Status | Meaning | May Be Treated as Current Active Coverage? |
|---|---|---|
| `active` | Authoritative source reports active coverage | Yes, only for the checked scope/date |
| `inactive` | Authoritative source reports inactive coverage | No |
| `unknown` | Coverage cannot currently be determined | No |
| `pending` | Determination is still in progress | No |
| `stale` | Prior result exists but is too old for current reliance | No |

These are the only values that should populate the durable coverage-status field.

## 9. Transient UI Category: `simulated`

### Meaning

`simulated` indicates that the displayed eligibility result came from the synthetic training path rather than a real payer integration.

It is a **source/runtime category**, not a coverage status.

Correct representation:

```text
coverage_status: active
source_mode: simulated
```

Incorrect representation:

```text
coverage_status: simulated
```

Safe wording:

> Simulated eligibility result: active.

The system must never present a simulated result as confirmation from a real payer.

## 10. Transient UI Category: `unavailable`

### Meaning

`unavailable` indicates that the application could not obtain or display a usable current eligibility response for this request.

It is a **transient runtime/UI condition**, not a durable coverage status.

Possible synthetic causes may include:

- Timeout.
- External service unavailable.
- Invalid provider response.
- Missing configuration.
- Circuit breaker open.
- Temporary network failure.

Correct representation:

```text
coverage_status: unknown
availability: unavailable
```

or, when a prior durable result exists:

```text
coverage_status: stale
availability: unavailable
previous_status: active
```

Incorrect representation:

```text
coverage_status: unavailable
```

Safe wording:

> Eligibility information is currently unavailable.

The UI must not treat `unavailable` as proof of either active or inactive coverage.

## 11. Runtime Category Summary

| Runtime/UI Category | Meaning | Coverage Status? |
|---|---|---|
| `simulated` | Result came from the training/demo source path | No |
| `unavailable` | A usable current result could not be obtained/displayed | No |

Runtime categories may appear alongside a durable status.

Example:

```text
coverage_status: active
source_mode: simulated
availability: available
```

or:

```text
coverage_status: stale
source_mode: real
availability: unavailable
```

## 12. State Separation Rule

Application code should keep these concepts in separate fields.

Recommended shape:

```text
coverage_status:
  active | inactive | unknown | pending | stale

source_mode:
  real | simulated

availability:
  available | unavailable
```

Do not overload one field with all possible display states.

This separation prevents UI conditions from corrupting durable coverage history.

## 13. Covered Benefits

Covered benefits describe whether a plan includes a specific service or service category under particular conditions.

Benefit information may include:

- Covered service categories.
- Exclusions.
- Copayments or coinsurance.
- Deductibles.
- Visit limits.
- Network requirements.
- Referral requirements.
- Prior authorization requirements.
- Effective dates.

The system must not invent benefit information that is not present in an approved source or payer response.

## 14. Eligibility Does Not Guarantee Payment

Even when durable status is `active`, final payment may depend on:

- The actual service performed.
- Provider network status.
- Prior authorization.
- Referral requirements.
- Medical-necessity rules.
- Coding.
- Benefit limitations.
- Deductibles and cost sharing.
- Claim-processing rules.

Therefore:

```text
coverage_status = active
≠ guaranteed benefit coverage
≠ guaranteed claim payment
```

## 15. Prior Authorization

Eligibility and prior authorization are separate questions.

Example:

```text
coverage_status: active
service: potentially covered
prior_authorization: required
```

If prior-authorization status is unknown:

```text
unknown
→ do not assume
→ verify
```

## 16. When to Contact the Payer

The patient or staff should contact the payer, or use an approved payer-verification workflow, when:

- Durable coverage status is `unknown`.
- Durable coverage status is `stale` and a fresh result is required.
- Durable coverage status remains `pending` beyond the expected workflow.
- Runtime availability is `unavailable`.
- Benefit information is missing.
- A specific service cannot be confirmed as covered.
- Prior-authorization requirements are unclear.
- Network status is unclear.
- Cost-sharing information is incomplete.
- Coverage dates appear inconsistent.
- Approved sources conflict.
- A definitive payer-specific answer is required.

## 17. Patient-Facing Language

Preferred:

> Your eligibility check returned active.

Avoid:

> Your insurance covers everything.

Preferred:

> Current eligibility status is unknown.

Avoid:

> Your insurance is probably active.

Preferred:

> Eligibility information is temporarily unavailable.

Avoid:

> Your eligibility status is unavailable.

Preferred:

> Simulated eligibility result: active.

Avoid:

> Eligibility status: simulated.

Preferred:

> The available information does not confirm whether this specific service is covered.

Avoid:

> It is probably covered.

## 18. AI and Agentic Assistance

If an AI or agent explains eligibility or coverage:

- It must preserve the exact durable coverage status returned by trusted application logic.
- It must distinguish durable status from transient runtime/UI categories.
- It must not describe `simulated` or `unavailable` as eligibility statuses.
- It must distinguish real from simulated source mode.
- It must distinguish eligibility from benefits.
- It must not invent covered services.
- It must not promise claim payment.
- It must not invent prior-authorization requirements.
- It must not invent patient cost.
- It should use approved policy or benefit sources for explanations.
- It should direct the user to payer verification when authoritative information is missing.

The model explains evidence; trusted systems determine and store coverage state.

## 19. Conflicting Information

If approved sources conflict:

```text
conflict detected
→ coverage_status = unknown
→ do not choose
→ do not average
→ do not guess
→ contact payer or route for staff review
```

The model must not resolve payer conflicts using confidence, similarity score, or general model knowledge.

## 20. Freshness and Provenance

The system should retain:

- Durable coverage status.
- Previous authoritative status when useful.
- Date and time checked.
- Source.
- Source mode (`real` or `simulated`).
- Runtime availability.
- Freshness timestamp or age.
- Benefit category when applicable.
- Request or correlation identifier.

A temporary outage must not overwrite a previously known durable coverage state with `unavailable`.

## 21. Fail-Closed Rule

If the system cannot determine current coverage:

```text
coverage_status = unknown
→ do not assume active
```

If a prior result has exceeded the freshness period:

```text
coverage_status = stale
→ require refresh or staff review
```

If the payer/service cannot currently be reached:

```text
availability = unavailable
→ preserve durable status/history
→ do not convert status to unavailable
```

If the system cannot verify a benefit statement:

```text
unknown benefit
→ do not claim coverage
→ verify with payer or staff workflow
```

## 22. Logging and Privacy

Logs and traces should contain only privacy-safe operational metadata.

Permitted metadata may include:

- Durable coverage status.
- Source mode.
- Availability category.
- Check timestamp.
- Correlation identifier.
- Error category.

Logs and traces must not contain:

- Credentials.
- Payer API secrets.
- Raw provider errors.
- Full payer responses.
- Unnecessary patient information.
- Raw prompts or retrieved private text.

## 23. Training Limitation

**This is a fictional Coverage and Eligibility Status Guide created for a synthetic software demonstration. It is not an insurance coverage determination, benefit guarantee, legal or financial advice, and must not be used to verify real insurance benefits or make decisions for real patients.**
