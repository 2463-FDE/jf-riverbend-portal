# Training-simulation staff roster — what each row is for

`staff_roster_SYNTHETIC.csv` is the training-simulation staff directory this
exercise runs against. **The people are fictional by design** — Riverbend is a
simulation, so there is no other roster behind this one, and a mapping report
built from it is the intended basis for review and sign-off.

It carries the five columns the client specified — name, function, department,
clinic, status — and nothing else. No usernames: a real HR export wouldn't
carry them, so the mapping has to reconcile roster names against
`users.full_name`, which is the interesting part and the part that produces
the "unmatched" column the client signs off on.

## Why these rows

The roster is shaped to exercise every case the migration has to handle, so
the dry run is a real test rather than a happy path. The seeded accounts in
`db/seed/seed.sql` already contained most of the hard cases; the roster is
built around them.

| Case | Roster side | Account side | Expected dry-run outcome |
|---|---|---|---|
| Clean map, one person one account | Maya Okonkwo, Tom Reyes, Dana White, Karen Cole, Anil Patel, Anita Nguyen, Rosa Delgado, Jin Park | `mokonkwo`, `billing1`, `roiclerk`, `nurse_kc`, `drpatel`, `drnguyen`, `rdelgado`, `jpark` | Migrate to `management`, `billing`, `roi_clerk`, `nursing_ma`, `clinician` ×2, `front_desk` ×2 |
| **Shared login** — several people, one account | Rosa Delgado, Jin Park, Priya Raman, Marcus Bell all do Patient Registration at Front Office | `frontdesk` — full name is "Front Desk (Riverbend Main)", not a person | **Cannot migrate.** Split into named accounts first. This is the MFA prerequisite: a shared login cannot hold a second factor |
| **Shared login**, second instance | Aisha Kone, Diego Marquez, both Laboratory Technicians | `labtech` — "Lab Intake", not a person | Same: split before migrating |
| **Account with no owner** | *no row* — nobody in the roster is Helix Support | `itadmin` — "Helix Support" | **Disable, do not migrate.** This is the departed contractor the client named explicitly (Helix Digital Partners authored every ADR) |
| **Person left, account still live** | Sandra Lee, status `terminated` | `drlee` — "Dr. Sandra Lee" | **Disable, do not migrate.** Distinct from the case above: here we know who it was, and that they've gone |
| **Person with no account** | Nadia Osei, Scheduling Coordinator | *none* | Nothing to migrate. Report as a roster row needing an account — and note no `scheduler` account exists anywhere today |
| **Function that maps to no role** | Grace Liang, Volunteer Coordinator, Community Outreach | *none* | **Unmapped.** If such a person ever has an account, deny by default with the supervisor-contact screen |
| **On leave** | Yusuf Demir, status `leave` | *none* | Genuinely undecided — the client specified `active` and `terminated` handling but not `leave`. Surface as a question rather than guessing |
| **Real IT/Admin, replacing the contractor** | Owen Fitzgerald, Systems Administrator | *none* | Needs an `it_admin` account created. Note the contrast with `itadmin` above: the function stays, the contractor's account still goes |

## Two things the roster deliberately does not resolve

**Name matching is imperfect on purpose.** `users.full_name` carries suffixes
the roster doesn't — `"Maya Okonkwo (COO)"`, `"Karen Cole, RN"`, `"Dr. Anil
Patel"`. The mapping has to normalise before it can match, and every
non-match must land in the report rather than being silently dropped. A
migration that quietly skips an account it couldn't parse is the failure this
whole dry run exists to prevent.

**`leave` has no defined handling.** The client set the rule for active and
terminated staff. Yusuf Demir exists so that gap shows up in the report and
gets asked about, instead of being decided here.

## Regenerating or extending

Edit the CSV directly — it represents a file that arrives from outside the
system, so there is no generator. If you add a row, add its case to the table
above, and keep the `# SYNTHETIC` header intact.
