"""The review queue is a gate, not a workflow log.

The client rejected a queue that is "a table plus a screen": the approve/reject
decision has to control what the patient can actually see. These tests are
written against that requirement rather than against the code's shape, so the
central assertion is not "the queue records a decision" but "the decision
changes what the patient gets".

The rule under test everywhere below is DEFAULT DENY. Refused content becomes
visible only via an explicit approval; no review, pending, and rejected are all
indistinguishable to the patient.
"""
import pytest

from conftest import load_module

ps = load_module("services/records-service/patient_summary.py", "ps_review_gate")

# The seed's own prose — a visit note the renderer cannot quote cleanly.
PROSE = "Penicillin allergy confirmed. Switched to alternative."


class _Row:
    def __init__(self, id, title, body, kind="note", reference_range=None, day=1):
        from datetime import datetime, timezone

        self.id = id
        self.title = title
        self.body = body
        self.kind = kind
        self.reference_range = reference_range
        self.created_at = datetime(2026, 5, day, tzinfo=timezone.utc)


def _only(items):
    assert len(items) == 1
    return items[0]


# --- the gate itself -------------------------------------------------------


def test_refused_content_is_withheld_when_no_review_exists():
    """The default. Nothing has been decided, so nothing is released."""
    item = _only(ps.render_items([_Row(1, "Visit note", PROSE)]))

    assert item.quote is None
    assert item.refusal_reason
    assert item.released_by_review is False
    assert PROSE not in repr(item)


def test_refused_content_is_withheld_while_the_review_is_pending():
    """Pending is not a weaker form of approved. A queue that showed content
    while a clinician was still deciding would make the decision pointless."""
    item = _only(ps.render_items([_Row(1, "Visit note", PROSE)], approved_record_ids=frozenset()))

    assert item.quote is None
    assert item.released_by_review is False


def test_a_rejected_review_never_becomes_patient_visible():
    """The client's acceptance criterion, stated directly.

    A rejected record simply never appears in the approved set, so the reader
    treats it exactly like one that was never reviewed. There is no code path
    that distinguishes them, which is the point — a rejection cannot decay into
    a disclosure through some other branch.
    """
    item = _only(
        ps.render_items([_Row(1, "Visit note", PROSE)], approved_record_ids=frozenset({999}))
    )

    assert item.quote is None
    assert item.refusal_reason
    assert item.released_by_review is False


def test_an_approved_review_releases_the_reports_own_words():
    item = _only(
        ps.render_items([_Row(1, "Visit note", PROSE)], approved_record_ids=frozenset({1}))
    )

    assert item.quote == PROSE          # verbatim — approval releases, it does not rewrite
    assert item.refusal_reason is None
    assert item.released_by_review is True


def test_approval_is_per_record_not_per_patient():
    """Approving one note must not release every other refused note on the
    chart. A blanket release is the failure mode a per-record gate exists to
    prevent."""
    items = {
        i.record_id: i
        for i in ps.render_items(
            [_Row(1, "Visit note", PROSE), _Row(2, "Visit note", "Discussed at length.")],
            approved_record_ids=frozenset({1}),
        )
    }

    assert items[1].quote == PROSE and items[1].released_by_review is True
    assert items[2].quote is None and items[2].refusal_reason


def test_the_gate_defaults_closed_when_a_caller_forgets_it():
    """render_items' signature defaults to an empty set, so a caller that
    never learned about the gate refuses rather than discloses. Fail-closed by
    construction beats fail-closed by remembering."""
    default = _only(ps.render_items([_Row(1, "Visit note", PROSE)]))
    assert default.quote is None


def test_approval_does_not_turn_prose_into_a_computed_trend():
    """Releasing content is permission to show existing words, not permission
    to start doing arithmetic on them. A released note carries no change."""
    items = ps.render_items(
        [_Row(1, "Visit note", PROSE, day=1), _Row(2, "Visit note", PROSE, day=2)],
        approved_record_ids=frozenset({1, 2}),
    )
    for item in items:
        assert item.change is None


def test_a_quotable_result_never_needs_review():
    """The other half of the client's split: directly-supported facts reach the
    patient without a clinician, because none of them is a clinical judgment.
    If these queued, the queue would fill with things nobody needs to decide."""
    item = _only(ps.render_items([_Row(1, "A1c", "6.2%.", kind="lab_result")]))

    assert item.quote == "6.2%."
    assert item.refusal_reason is None
    assert item.released_by_review is False   # shown on its own merits, not released


# --- the queue logic -------------------------------------------------------

rq = load_module("services/records-service/review_queue.py", "review_queue_pure")


def test_a_decision_requires_an_identified_clinician():
    """An anonymous approval would make the accounting worthless — the whole
    point is that a named person took responsibility."""
    with pytest.raises(ValueError, match="identified clinician"):
        rq.decide(None, review_id=1, state=rq.APPROVED, actor_id=None)


@pytest.mark.parametrize("bad", ["pending", "maybe", "", "APPROVED"])
def test_only_approve_or_reject_are_decisions(bad):
    """`pending` is rejected too: "deciding" something back to pending would
    strip its decider and reopen content that was already ruled on."""
    with pytest.raises(ValueError, match="state must be one of"):
        rq.decide(None, review_id=1, state=bad, actor_id=7)


# --- who may decide, per the signed role grid -------------------------------

rc = load_module("services/gateway/roles_config.py", "roles_review_gate")

# The release action has its own permission. See the route comment in
# records-service for why neither records.write nor read+write was a gate:
# `lab` holds write without read, and the deprecated `staff` role holds both.
_REVIEW_PERMISSIONS = ("summary_review.decide", "records.read")


@pytest.mark.parametrize("role", ["clinician", "nursing_ma"])
def test_clinical_roles_may_decide_a_review(role):
    assert all(p in rc.permissions_for(role) for p in _REVIEW_PERMISSIONS)


def test_the_deprecated_staff_role_cannot_decide_a_review():
    """Adversarial review of #40 — the finding my first fix did not cover.

    Requiring records.read + records.write kept `lab` out but not `staff`,
    which holds both — and every seeded account is still on `staff`. So
    billing, ROI clerks, the IT admin and the front desk could all have
    released withheld clinical notes to a patient.

    "The grid is right, the migration is outstanding" is a fair description of
    RBAC in general and the wrong call here: this feature introduces the
    disclosure capability, so it must not ship reachable by twelve
    non-clinical accounts while containment waits on a roster signature weeks
    away. summary_review.decide is held only by clinical roles, so the gate is
    closed for every existing account.
    """
    staff = rc.permissions_for("staff")
    assert "records.read" in staff and "records.write" in staff, "premise: staff holds both"
    assert "summary_review.decide" not in staff
    assert not all(p in staff for p in _REVIEW_PERMISSIONS)


def test_the_lab_role_cannot_reach_the_review_queue():
    """Adversarial review of #41, B2 — the hole this suite originally had.

    `lab` holds records.write but NOT records.read, and config/roles.yaml
    records why: the client revised their own earlier answer because, with no
    separate results category in the schema, letting lab read prior results
    would mean handing them the whole chart.

    The queue shows the full text of withheld clinical notes and offers a
    button that releases them to a patient. Gating it on records.write alone
    therefore granted `lab` exactly the chart access that decision refused —
    through a side door, and with release power attached.

    The original parametrised list below omitted `lab` entirely, so it read as
    thorough while leaving the one role that could actually exploit the gap
    untested. That is the failure this test exists to prevent recurring.
    """
    lab = rc.permissions_for("lab")
    assert "records.write" in lab, "premise: lab does hold write"
    assert "records.read" not in lab, "premise: lab does NOT hold read"
    assert not all(p in lab for p in _REVIEW_PERMISSIONS), "so lab must fail the review gate"


@pytest.mark.parametrize(
    "role",
    ["front_desk", "billing", "roi_clerk", "scheduler", "it_admin", "patient", "lab", "staff",
     "management"],
)
def test_non_clinical_roles_may_not_decide_a_review(role):
    """Releasing withheld chart content to a patient is a clinical decision.

    This is asserted here rather than end to end because it CANNOT be shown
    end to end yet: every seeded account is still on the deprecated `staff`
    role, which retains the full permission set, so `frontdesk` currently
    reaches the queue in a live stack. The grid is right; the account
    population is what is outstanding (the cycle's branch 9). When that
    migration lands, this expectation becomes demonstrable against real
    accounts — and this test is what says it was always the intent.
    """
    assert not all(p in rc.permissions_for(role) for p in _REVIEW_PERMISSIONS)


def test_only_clinical_roles_hold_the_release_permission():
    """The whole grid, in one assertion, so a future grant is deliberate.

    Adding summary_review.decide to any other role means someone chose to let
    that role disclose withheld clinical content to patients. This test is what
    makes that a decision rather than an accident.
    """
    holders = {r for r in rc.roles() if "summary_review.decide" in rc.permissions_for(r)}
    assert holders == {"clinician", "nursing_ma"}
