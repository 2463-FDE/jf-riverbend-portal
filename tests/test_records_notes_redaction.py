"""`patients.notes` is withheld from roles that may not read clinical notes.

Client decision (2026-08-14). The field is served under `patients.read`, which
Front Desk and Billing both hold, but it is one free-text column carrying both
clinical and non-clinical content — the seeded data has "PCN allergy noted at
front desk." beside "Prefers morning appts." There is no way to hand over the
scheduling half without the allergy half, so it fails closed.
"""
import pytest
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_notes_redaction")


class _Row:
    def __init__(self, role="staff", is_active=True):
        self.role = role
        self.is_active = is_active


class _Result:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row=_Row(), fail=False):
        self._row = row
        self._fail = fail

    def execute(self, _stmt):
        if self._fail:
            raise SQLAlchemyError("simulated store failure")
        return _Result(self._row)


def _detail(notes="PCN allergy noted at front desk."):
    return app_mod.PatientDetail(id=1042, name="Test Patient", notes=notes)


def test_a_role_that_may_read_notes_keeps_them():
    out = app_mod._redact_clinical_fields(_Db(_Row("clinician")), _detail(), x_actor_id="2")

    assert out.notes == "PCN allergy noted at front desk."
    assert out.notes_withheld is False


def test_a_role_that_may_not_read_notes_has_them_withheld():
    # roi_clerk holds patients.read but not records.read.
    out = app_mod._redact_clinical_fields(_Db(_Row("roi_clerk")), _detail(), x_actor_id="2")

    assert out.notes is None
    assert out.notes_withheld is True


def test_withholding_is_distinguishable_from_genuinely_having_no_notes():
    # The whole point of the flag: a UI must be able to say "withheld" rather
    # than imply the patient has no notes on file.
    withheld = app_mod._redact_clinical_fields(_Db(_Row("roi_clerk")), _detail(), x_actor_id="2")
    empty = app_mod._redact_clinical_fields(_Db(_Row("clinician")), _detail(notes=None), x_actor_id="2")

    assert (withheld.notes, withheld.notes_withheld) == (None, True)
    assert (empty.notes, empty.notes_withheld) == (None, False)


def test_the_rest_of_the_demographics_record_is_untouched():
    # Front desk still needs to do its job — only the notes field is affected.
    out = app_mod._redact_clinical_fields(_Db(_Row("roi_clerk")), _detail(), x_actor_id="2")

    assert out.id == 1042
    assert out.name == "Test Patient"


@pytest.mark.parametrize("row", [None, _Row("clinician", is_active=False), _Row("not-a-real-role")])
def test_withholds_when_the_actor_is_unknown_inactive_or_unrecognised(row):
    out = app_mod._redact_clinical_fields(_Db(row), _detail(), x_actor_id="2")

    assert out.notes is None and out.notes_withheld is True


def test_withholds_when_the_role_cannot_be_read_at_all():
    # Fail closed: an unreadable role must not fall through to disclosure.
    out = app_mod._redact_clinical_fields(_Db(fail=True), _detail(), x_actor_id="2")

    assert out.notes is None and out.notes_withheld is True


def test_withholds_when_there_is_no_actor():
    out = app_mod._redact_clinical_fields(_Db(), _detail(), x_actor_id=None)

    assert out.notes is None and out.notes_withheld is True


# --- the reviewer's explicit ask (PR #33 review [high]) ---------------------
#
# The finding: using broad `records.read` as the notes predicate meant a
# front_desk actor would pass and receive the raw clinical field. That was true
# against the four-role grid, where front_desk still held records.read. The
# signed nine-role grid removes it — so the predicate is now correct AND the
# grid is what makes it correct. Both halves are pinned here, because if either
# regresses the field leaks and nothing else would catch it.


def test_front_desk_does_not_hold_records_read_in_the_signed_grid():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "records-service"))
    import roles_config

    roles_config.reload()
    assert "records.read" not in roles_config.permissions_for("front_desk")
    assert "records.read" not in roles_config.permissions_for("billing")


def test_a_front_desk_actor_with_a_patient_grant_still_gets_notes_withheld():
    # The regression test the reviewer asked for by name. A grant gets you the
    # patient; it does not get you the clinical field.
    out = app_mod._redact_clinical_fields(_Db(_Row("front_desk")), _detail(), x_actor_id="2")

    assert out.notes is None
    assert out.notes_withheld is True


def test_billing_is_withheld_too():
    out = app_mod._redact_clinical_fields(_Db(_Row("billing")), _detail(), x_actor_id="2")

    assert out.notes is None and out.notes_withheld is True


def test_only_the_clinical_roles_receive_the_field():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "records-service"))
    import roles_config

    roles_config.reload()
    receives = {
        r for r in ("front_desk", "clinician", "nursing_ma", "lab", "billing",
                    "roi_clerk", "scheduler", "it_admin", "management")
        if app_mod._redact_clinical_fields(_Db(_Row(r)), _detail(), x_actor_id="2").notes is not None
    }
    assert receives == {"clinician", "nursing_ma"}
