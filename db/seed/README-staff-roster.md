# Training-simulation staff roster — what each row is for

`staff_roster_SYNTHETIC.csv` is the **client's** staff roster, received
2026-08-19 (prepared by them 13 Aug 2026, amended 20 Aug to add Grace Kim). The
people are fictional by design — Riverbend is a simulation, so there is no
other roster behind this one, and a mapping report built from it is the
intended basis for review and sign-off.

⚠️ **It replaced a roster we invented.** The earlier cast — Marcus Bell, Yusuf
Demir, Aisha Kone, Diego Marquez, Nadia Osei, Owen Fitzgerald, Grace Liang —
was built to exercise every case the migration must handle. The client's roster
is **realistic rather than comprehensive**: nobody on it is on leave, and every
function maps to a role. Two cases therefore have no example in the data any
more, and the tests for them use fixtures instead (see
`tests/test_roster_dry_run.py`). Do not plan against the old cast; git history
explains any earlier report.

It carries the five columns the client specified — name, function, department,
clinic, status — plus `proposed_role`, **their** proposal for each person. That
column is a cross-check, never the answer: the mapping derives its own role
from function, validated against `config/roles.yaml`, and the report flags any
disagreement. That catches a stale `FUNCTION_TO_ROLE` table and a client-side
typo in the same pass. Silence there means every role the report proposes is
one the client already wrote down.

No usernames: a real HR export would not carry them, so the mapping has to
reconcile roster names against `users.full_name`, which is the interesting part
and the part that produces the column the client signs.

## Why these rows

The roster is the client's, so its shape is theirs rather than ours. What each
case produces:

| Case | Roster side | Account side | Dry-run outcome |
|---|---|---|---|
| Clean map, one person one account | Maya Okonkwo, Rosa Delgado, Jin Park, Anil Patel, Anita Nguyen, Sandra Lee, **Grace Kim**, Karen Cole, Tom Reyes | `mokonkwo`, `rdelgado`, `jpark`, `drpatel`, `drnguyen`, `drlee`, `drkim`, `nurse_kc`, `billing1` | **Migrate** — nine accounts onto their roles |
| **Duplicate accounts for one person** | Dana White | `roiclerk` and the least-privilege demo account `dwhite` both normalize to Dana White | **Cannot migrate automatically.** Both accounts enter the human-decision bucket until the canonical credential is chosen |
| **Shared login** — several people, one account | four front-desk staff share it; only three are named | `frontdesk` — "Front Desk (Riverbend Main)", not a person | **Cannot migrate.** Split into named accounts first. Blocked: the fourth staffer is named nowhere |
| **Shared login**, second instance | Ben Osei, Priya Raman | `labtech` — "Lab Intake" | Same: split before migrating |
| **Account with no owner** | *no row* — nobody is Helix Support | `itadmin` | **Disable, do not migrate.** The departed contractor |
| **Person with no account** | eight people, incl. the first `scheduler` and `it_admin` anywhere | *none* | Report as needing an account, with the role their function maps to |
| **Temporary placement** | Sofia Marin, `temp_ends_2026-09-30` | *none* | Provision **with an expiry**. Same role as a permanent registrar; the temporary part is the expiry, not a weaker role |
| **Departed, may still hold an account** | Marcus Hale, Erin Castillo | *none found* | **Departures checked** — reported as no-live-account-found rather than dropped, because the client asked precisely this |
| **Renames** | Tom Reyes on `billing1` | generic username | Migrate, and rename during migration so the audit log names a human. Dana White's rename is deferred until the duplicate-account decision above is resolved |

### Two cases the client's roster does not contain

**On leave** and **function maps to no role** have no example in this data. Both
rules still exist and are still enforced — they are covered by fixtures in
`tests/test_roster_dry_run.py` rather than by rows here. If the client's
deny-by-default copy needs a live demonstration, it now comes from accounts
that are not on the roster at all, which is their own stated rule.

### Three things the roster cannot answer

Raised with the client 2026-08-20, unresolved at the time of writing:

1. **The fourth front-desk staffer is unnamed.** Splitting `frontdesk` into
   named accounts is blocked until they name them.
2. **The header count does not reconcile.** Twenty people are identifiable,
   plus the unnamed float; the client's header says 22.
3. **Scale.** They describe "on the order of a thousand accounts" on the
   `staff` role. The seeded database has 14, so the report demonstrates the
   mechanism, not the volume.

## Name matching is the hard part, and it bit us

`users.full_name` carries decoration the roster does not, in both directions:
`"Maya Okonkwo (COO)"`, `"Karen Cole, RN"`, `"Dr. Anil Patel"` on the account
side; `"Anil Patel MD"`, `"Grace Iwu MA"` on the client's side. The mapping
normalises both before comparing, and every non-match lands in the report
rather than being silently dropped — a migration that quietly skips an account
it could not parse is the failure this dry run exists to prevent.

⚠️ **The comma mattered.** `normalise_name` stripped `", RN"` but not a bare
`" MD"`. The client writes credentials without a comma, so **five of the six
clinical accounts** — `drkim`, `drpatel`, `drnguyen`, `drlee`, `nurse_kc` —
matched nobody and were reported as having no identified owner. The report
would have recommended disabling four working clinicians, in its *safe* column.
Fixed 2026-08-20 with an anchored, enumerated credential list, so a real
surname like "Bright" or "Reyes" is never mistaken for a credential.

The lesson worth keeping: on this report a false "no owner" is more dangerous
than a false "migrate", because it reads as the cautious answer.

## Regenerating or extending

Edit the CSV directly — it arrives from outside the system, so there is no
generator. Keep the provenance header intact: it records that these rows are
the client's rather than ours, which is what makes the report signable.

If the client sends a revision, replace the rows and update the case table
above. Do **not** adjust `FUNCTION_TO_ROLE` to make a row map without checking
`config/roles.yaml` first — `role_for_function` validates against the grid and
returns nothing for a role the grid does not define, deliberately: a proposal
the enforcement layer would fail closed on is worse than no proposal.
