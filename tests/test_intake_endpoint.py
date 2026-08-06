"""Acceptance-style test for POST /intake's Stage 3 behavior
(services/intake-service/app.py::create_intake): patient/coverage/consent
persist independently of payer latency, and the endpoint itself never blocks
on the eligibility check.

Drives create_intake() directly with a fake SQLAlchemy Session (add/commit/
refresh only — no real Postgres) and a mocked httpx.post, mirroring the
direct-function-call style already used by test_intake_eligibility.py. This
is the "latency bound" + "patient/coverage/consent persist independently of
payer latency" acceptance test called for by the Stage 3 plan.
"""
import logging
import time

import pytest

from conftest import load_module

app_mod = load_module("services/intake-service/app.py", "intake_app_endpoint")

IntakeRequest = load_module("services/intake-service/schemas.py", "intake_schemas_for_endpoint").IntakeRequest

# Round-11 review: /intake now requires a gateway-forwarded internal token
# (mirrors records-service's _verify_internal_token). Configured once here
# and defaulted onto every call via _create_intake below, so every existing
# test keeps exercising create_intake's actual business logic rather than
# tripping over the new transport-trust check — tests that specifically
# target that check pass their own x_internal_token explicitly.
TEST_TOKEN = "test-internal-token-for-intake-well-over-32-chars"


@pytest.fixture(autouse=True)
def _configured_internal_token(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)


def _create_intake(req, *, db, x_request_id=None, x_internal_token=TEST_TOKEN):
    return app_mod.create_intake(req, db=db, x_request_id=x_request_id, x_internal_token=x_internal_token)


class _FakeQueryResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeSession:
    def __init__(self, existing_patients=None):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self.lock_calls = []
        self._next_id = 1
        # Week 2-3 catch-up: rows _find_match_candidates should "find" via
        # db.execute(select(Patient)...). Ignores the actual query — this
        # fake just returns whatever the test configured, since we're
        # testing app.py's branching logic, not real SQL.
        self.existing_patients = existing_patients or []

    def add(self, obj):
        self.added.append(obj)

    def _assign_ids(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    def flush(self):
        # Round-3 review fix: _create_patient_with_links flushes to obtain
        # the new patient's id before adding its link rows, all inside one
        # transaction committed once — this mirrors that without ending it.
        self._assign_ids()

    def commit(self):
        self.commit_count += 1
        self._assign_ids()

    def refresh(self, obj):
        pass

    def rollback(self):
        self.rollback_count += 1

    def execute(self, _stmt, _params=None):
        # Week 2-3 catch-up round-8 fix: create_intake also issues a
        # pg_advisory_xact_lock statement before the match-key select (see
        # _acquire_match_key_lock) — recorded here so tests can assert it
        # happened, then falls through to the same fake patient rows for the
        # actual match-key select.
        self.lock_calls.append(_params)
        return _FakeQueryResult(self.existing_patients)


class _RaisingFakeSession(_FakeSession):
    """A fake session whose flush() or commit() raises a SQLAlchemyError
    carrying a PHI-shaped message — simulating what a real DBAPIError's
    str() embeds (the failed statement's bound parameters) when the engine
    isn't configured with hide_parameters=True. See the PR #20 round-6 fix:
    the live reproduction against a real Postgres confirmed a genuine
    insert failure's str(e) contains name/dob/ssn/address/... verbatim.

    Round-13 review: _create_patient/_create_patient_with_links/
    _create_coverage/_record_consents now flush (never commit) — only
    create_intake's single final commit is durable. raise_on picks which
    call fails: "flush" (default) to test one of those helpers' own error
    handling in isolation, "commit" to test create_intake's single
    top-level commit that finalizes the whole patient+coverage+consent
    group together, "execute" to test the round-8 advisory-lock/round-20
    match-key-select phase (both go through db.execute()).
    """

    def __init__(self, message, raise_on="flush"):
        super().__init__()
        self._message = message
        self._raise_on = raise_on

    def flush(self):
        if self._raise_on == "flush":
            raise app_mod.SQLAlchemyError(self._message)
        super().flush()

    def commit(self):
        if self._raise_on == "commit":
            raise app_mod.SQLAlchemyError(self._message)
        super().commit()

    def execute(self, _stmt, _params=None):
        if self._raise_on == "execute":
            raise app_mod.SQLAlchemyError(self._message)
        return super().execute(_stmt, _params)


class _ConsentFailingSession(_FakeSession):
    """Round-12 review: lets patient (and coverage, if any) flush normally,
    then raises on the Nth consent flush specifically — proving a consent
    write failure is no longer swallowed regardless of which consent in the
    list fails. Round-13 review: consents are flushed, not committed, until
    create_intake's single final commit — so this now overrides flush(),
    not commit()."""

    def __init__(self, fail_at_consent_index, **kwargs):
        super().__init__(**kwargs)
        self._fail_at_consent_index = fail_at_consent_index
        self._consent_flush_count = 0

    def flush(self):
        if self.added and type(self.added[-1]).__tablename__ == "consents":
            index = self._consent_flush_count
            self._consent_flush_count += 1
            if index == self._fail_at_consent_index:
                raise app_mod.SQLAlchemyError("simulated consent write failure")
        super().flush()


def _request(**overrides):
    payload = {
        "demographics": {"name": "Jane Roe", "dob": "1990-01-01"},
        "insurance": {"payer_name": "Aetna", "member_id": "MEM1"},
        "consents": ["npp_ack", "treatment_consent"],
    }
    payload.update(overrides)
    return IntakeRequest(**payload)


class _FakePatientRow:
    """Stand-in for an existing patients row, as read by
    _find_match_candidates (only .id/.ssn/.dob are touched)."""

    def __init__(self, id, ssn, dob):
        self.id = id
        self.ssn = ssn
        self.dob = dob


# --- Round-11 review: intake-service's internal-token gate ------------------
#
# The gateway already requires a valid staff session before it will forward
# to /intake (services/gateway/app.py::proxy_intake), but intake-service
# itself had no way to tell a genuine gateway-forwarded call apart from a
# caller hitting its own published host port (docker-compose.yml, port 8071)
# directly — bypassing that session check and turning the duplicate-
# detection response into an unauthenticated patient/SSN-existence oracle.
# These mirror tests/test_records_patient_view_route.py's token coverage.


def test_missing_internal_token_is_rejected_before_any_work(monkeypatch):
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession()

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(_request(), db=db, x_internal_token=None)

    assert exc_info.value.status_code == 401
    assert db.commit_count == 0
    assert db.added == []


def test_wrong_internal_token_is_rejected(monkeypatch):
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession()

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(_request(), db=db, x_internal_token="not-the-real-token")

    assert exc_info.value.status_code == 401
    assert db.added == []


def test_unconfigured_internal_token_fails_closed_even_with_matching_empty_values(monkeypatch):
    # If INTERNAL_SERVICE_TOKEN is unset on both services, an empty configured
    # value must NOT compare equal to an empty header — that would silently
    # reopen the exact bypass being fixed.
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession()

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(_request(), db=db, x_internal_token="")

    assert exc_info.value.status_code == 401
    assert db.added == []


def test_short_placeholder_internal_token_is_rejected_even_on_exact_match(monkeypatch):
    # A short, human-typed stand-in (e.g. literally "changeme") must fail
    # closed even if both sides somehow agree on it — matches
    # records-service's identical _MIN_INTERNAL_TOKEN_LENGTH floor.
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession(existing_patients=[_FakePatientRow(id=42, ssn="111-22-3333", dob="1990-01-01")])

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(
            _request(demographics={"name": "Jane Roe", "ssn": "111-22-3333", "dob": "1990-01-01"}),
            db=db, x_internal_token="changeme",
        )

    assert exc_info.value.status_code == 401
    assert db.added == []


def test_valid_internal_token_preserves_existing_behavior(monkeypatch):
    # Sanity check: the gate itself doesn't change create_intake's actual
    # business logic once it passes — a genuine gateway-forwarded call still
    # behaves exactly as every other test in this file already proves.
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession()

    result = _create_intake(_request(), db=db)

    assert result.patient_id == 1


def test_intake_returns_201_shape_promptly_when_eligibility_service_is_slow(monkeypatch):
    # Simulate eligibility-service's own enqueue endpoint being slow-ish (but
    # still within the bounded timeout) — /intake as a whole must not spin
    # for it the way the old inline payer call did (RIV-088: "~4-5s").
    def _slow_post(url, *, json, headers, timeout):
        time.sleep(0.05)

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"job_id": "job-abc", "status": "queued"}

        return _Resp()

    monkeypatch.setattr(app_mod.httpx, "post", _slow_post)

    db = _FakeSession()
    started = time.time()
    result = _create_intake(_request(), db=db, x_request_id=None)
    wall_clock = time.time() - started

    assert result.patient_id == 1
    assert result.eligibility_status == "pending"
    assert result.eligibility_job_id == "job-abc"
    assert result.eligibility["status"] == "pending"  # backward-compat field preserved
    # Nowhere near the old "~4-5s" RIV-088 spin, let alone RIV-141's ~20 min.
    assert wall_clock < 1.0
    assert result.elapsed_seconds < 1.0


def test_patient_coverage_and_consent_persist_even_if_eligibility_enqueue_fails(monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("eligibility-service unreachable")

    monkeypatch.setattr(app_mod.httpx, "post", _raise)

    db = _FakeSession()
    result = _create_intake(_request(), db=db, x_request_id=None)

    # The registration itself succeeded regardless of the eligibility hop.
    assert result.patient_id == 1
    assert result.eligibility_status == "unknown"
    assert result.eligibility_job_id is None
    # Patient, coverage, and both consents were all persisted.
    table_names = {type(obj).__tablename__ for obj in db.added}
    assert table_names == {"patients", "insurance_coverages", "consents"}
    # Round-13 review: patient + coverage + both consents now land in one
    # atomic commit, not four independent ones.
    assert db.commit_count == 1


def test_intake_without_insurance_never_calls_eligibility_service(monkeypatch):
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not be called when no insurance is supplied")

    monkeypatch.setattr(app_mod.httpx, "post", _post)

    db = _FakeSession()
    result = _create_intake(_request(insurance=None), db=db, x_request_id=None)

    assert calls["n"] == 0
    assert result.eligibility is None
    assert result.eligibility_status is None
    assert result.eligibility_job_id is None


def test_response_never_leaks_the_raw_request_body_pattern(monkeypatch):
    # Backward-compat/PHI sanity: the response model must never carry the
    # full intake payload back to the caller, regardless of what's logged.
    def _post(url, *, json, headers, timeout):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"job_id": "job-1", "status": "queued"}

        return _Resp()

    monkeypatch.setattr(app_mod.httpx, "post", _post)

    db = _FakeSession()
    result = _create_intake(_request(), db=db, x_request_id=None)

    dumped = result.model_dump_json()
    assert "Jane Roe" not in dumped
    assert "1990-01-01" not in dumped


def _mock_eligibility_post(monkeypatch):
    def _post(url, *, json, headers, timeout):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"job_id": "job-1", "status": "queued"}

        return _Resp()

    monkeypatch.setattr(app_mod.httpx, "post", _post)


def _intake_summary_dict(caplog):
    """Extract and parse the JSON payload of the 'POST /intake summary=' line."""
    import json as _json

    for record in caplog.records:
        msg = record.getMessage()
        if msg.startswith("POST /intake summary="):
            return _json.loads(msg[len("POST /intake summary="):])
    raise AssertionError("no 'POST /intake summary=' log line was emitted")


def test_intake_request_log_line_is_phi_redacted(monkeypatch, caplog):
    # D1 (Week 1 catch-up fix, revised twice after PR review): the front desk
    # still gets a log line recording that a registration happened, but it is
    # built from a narrow allowlist (_intake_log_summary), not any form of
    # the request body.
    #   Round 1 finding: redact()-on-the-whole-body still leaked
    #     insurance.member_id/group_number (not in the blocklist).
    #   Round 2 finding: even the first allowlist attempt still logged
    #     consents/has_dob/has_ssn/.../insurance_plan_type — health/payment-
    #     derived metadata that, combined with the immediately-following
    #     patient_id log line, could be correlated back to a specific
    #     patient. This test locks in the exact surviving key set
    #     (_INTAKE_LOG_SUMMARY_KEYS) so a future addition fails loudly
    #     instead of silently drifting back into a leak.
    _mock_eligibility_post(monkeypatch)

    db = _FakeSession()
    caplog.set_level(logging.INFO, logger=app_mod.log.name)
    _create_intake(
        _request(
            demographics={
                "name": "Jane Roe",
                "first_name": "Jane",
                "last_name": "Roe",
                "dob": "1990-01-01",
                "ssn": "111-22-3333",
                "address": "1 Test Way",
                "city": "Riverbend",
                "state": "CA",
                "zip_code": "90211",
                "phone": "555-0100",
                "email": "jane@example.test",
                "notes": "chief complaint text",
            },
            insurance={
                "payer_name": "Aetna",
                "member_id": "AET-SECRET-123456",
                "group_number": "GRP-9987",
                "plan_type": "PPO",
            },
        ),
        db=db,
        x_request_id=None,
    )

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for leaked in (
        "Jane Roe",
        "Jane",
        "Roe",
        "1990-01-01",
        "111-22-3333",
        "1 Test Way",
        "Riverbend",
        "555-0100",
        "jane@example.test",
        "chief complaint text",
        "Aetna",
        "AET-SECRET-123456",  # round-1 review finding
        "GRP-9987",  # round-1 review finding
        "PPO",  # round-2 review finding: plan type is payment-derived
        "npp_ack",  # round-2 review finding: consent names, not just values
        "treatment_consent",
    ):
        assert leaked not in log_text, f"{leaked!r} leaked into the intake log line"

    summary = _intake_summary_dict(caplog)
    # Exact key set, not just "these are present" — an addition here must
    # fail this test rather than silently ship, per the round-2 review ask.
    assert set(summary.keys()) == app_mod._INTAKE_LOG_SUMMARY_KEYS
    assert summary["created_via"] == "self_service"
    assert isinstance(summary["correlation_id"], str) and summary["correlation_id"]


def test_intake_log_summary_excludes_plan_type_and_consent_detail(monkeypatch, caplog):
    # Round-2 + round-4 review's regression ask: post an intake with a
    # sensitive plan type (Medicaid) and extra (optional) consents, assert
    # none of that — including a bare consent count, per round 4 — reaches
    # the log.
    _mock_eligibility_post(monkeypatch)

    db = _FakeSession()
    caplog.set_level(logging.INFO, logger=app_mod.log.name)
    _create_intake(
        _request(
            insurance={
                "payer_name": "State Medicaid Office",
                "member_id": "MEDI-000111",
                "group_number": None,
                "plan_type": "Medicaid",
            },
            consents=["npp_ack", "treatment_consent", "financial_agreement"],
        ),
        db=db,
        x_request_id=None,
    )

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for leaked in ("Medicaid", "State Medicaid Office", "npp_ack", "treatment_consent", "financial_agreement"):
        assert leaked not in log_text, f"{leaked!r} leaked into the intake log line"

    summary = _intake_summary_dict(caplog)
    assert set(summary.keys()) == app_mod._INTAKE_LOG_SUMMARY_KEYS
    assert "consent_count" not in summary  # round-4 review: even a bare count leaks a signal


def test_hostile_created_via_never_reaches_the_log(monkeypatch, caplog):
    # Round-3 review: Demographics.created_via is client-controlled, not an
    # enum. A caller putting PHI-shaped text there must never see it come
    # back out through the intake log line — schemas.py normalizes anything
    # outside {self_service, front_desk} to "unknown" before this endpoint
    # (or its log summary) ever sees it.
    _mock_eligibility_post(monkeypatch)

    db = _FakeSession()
    caplog.set_level(logging.INFO, logger=app_mod.log.name)
    _create_intake(
        _request(
            demographics={
                "name": "Jane Roe",
                "created_via": "patient is Jane Roe, SSN 111-22-3333",
            }
        ),
        db=db,
        x_request_id=None,
    )

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "111-22-3333" not in log_text
    assert "Jane Roe" not in log_text

    summary = _intake_summary_dict(caplog)
    assert set(summary.keys()) == app_mod._INTAKE_LOG_SUMMARY_KEYS
    assert summary["created_via"] == "unknown"


def test_hostile_x_request_id_header_never_reaches_the_log(monkeypatch, caplog):
    # Round-4 review: intake-service is exposed directly on the host
    # (docker-compose.yml, port 8071) and correlation_id was taken verbatim
    # from the caller-supplied X-Request-Id header — a caller could put
    # PHI-shaped text there and have it persisted in the log, bypassing the
    # allowlist entirely. _safe_correlation_id must reject anything that
    # isn't UUID-shaped and generate a fresh one instead.
    _mock_eligibility_post(monkeypatch)
    hostile_header = "patient is Jane Roe, SSN 123-45-6789"

    db = _FakeSession()
    caplog.set_level(logging.INFO, logger=app_mod.log.name)
    _create_intake(_request(), db=db, x_request_id=hostile_header)

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert hostile_header not in log_text
    assert "123-45-6789" not in log_text
    assert "Jane Roe" not in log_text

    summary = _intake_summary_dict(caplog)
    assert set(summary.keys()) == app_mod._INTAKE_LOG_SUMMARY_KEYS
    assert summary["correlation_id"] != hostile_header
    assert app_mod._CORRELATION_ID_PATTERN.fullmatch(summary["correlation_id"])


def test_legitimate_uuid_x_request_id_header_is_preserved(monkeypatch, caplog):
    # The fix for the above must not break legitimate distributed tracing: a
    # caller supplying a real UUID-shaped X-Request-Id should see that exact
    # value carried through as correlation_id, not silently replaced.
    _mock_eligibility_post(monkeypatch)
    real_uuid = "5f0f4b7e-2c3a-4d5e-8f9a-1b2c3d4e5f6a"

    db = _FakeSession()
    caplog.set_level(logging.INFO, logger=app_mod.log.name)
    _create_intake(_request(), db=db, x_request_id=real_uuid)

    summary = _intake_summary_dict(caplog)
    assert summary["correlation_id"] == real_uuid


def test_patient_insert_failure_never_logs_phi(caplog):
    # PR #20 round-6 review: _create_patient's error handler used to log
    # str(e) directly. A real SQLAlchemyError's string form embeds the
    # failed statement's bound parameters (verified live against a real
    # Postgres instance missing migration 011's columns), so this asserts
    # only the exception TYPE name reaches the log, never the PHI-shaped
    # message text itself.
    phi_shaped_message = (
        "[parameters: {'name': 'Jane Roe', 'ssn': '111-22-3333', "
        "'dob': '1990-01-01', 'address': '1 Test Way'}]"
    )
    db = _RaisingFakeSession(phi_shaped_message)
    caplog.set_level(logging.ERROR, logger=app_mod.log.name)

    demo = app_mod.Demographics(name="Jane Roe", ssn="111-22-3333", dob="1990-01-01")
    with pytest.raises(app_mod.HTTPException) as exc_info:
        app_mod._create_patient(db, demo)
    assert exc_info.value.status_code == 503

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Jane Roe" not in log_text
    assert "111-22-3333" not in log_text
    assert "1 Test Way" not in log_text
    assert "SQLAlchemyError" in log_text  # the type name is expected/safe


def test_coverage_insert_failure_never_logs_member_id(caplog):
    # Same fix, coverage path: member_id/group_number must never reach the
    # log via a raw exception string either.
    phi_shaped_message = (
        "[parameters: {'member_id': 'AET-SECRET-123456', 'group_number': 'GRP-9987'}]"
    )
    db = _RaisingFakeSession(phi_shaped_message)
    caplog.set_level(logging.ERROR, logger=app_mod.log.name)

    ins = app_mod.Insurance(payer_name="Aetna", member_id="AET-SECRET-123456", group_number="GRP-9987")
    with pytest.raises(app_mod.HTTPException) as exc_info:
        app_mod._create_coverage(db, patient_id=1, ins=ins)
    assert exc_info.value.status_code == 503

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "AET-SECRET-123456" not in log_text
    assert "GRP-9987" not in log_text
    assert "SQLAlchemyError" in log_text


# --- Round-12 review: consent write failures must not report a false 201 ---


def test_first_consent_write_failure_returns_503_not_201(monkeypatch):
    _mock_eligibility_post(monkeypatch)
    db = _ConsentFailingSession(fail_at_consent_index=0)

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(_request(), db=db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.status_code != 201
    # Round-13 review: the patient/coverage flushed ahead of this consent
    # are never committed either — nothing durable is left for a retry to
    # collide with (the exact-match 409 this would otherwise trip).
    assert db.commit_count == 0


def test_second_consent_write_failure_also_returns_503_not_201(monkeypatch):
    # The first consent (npp_ack) persists fine; the second (treatment_consent)
    # fails — the caller must still see a failure, not a 201 reporting only
    # the first consent as if the registration were legally complete.
    _mock_eligibility_post(monkeypatch)
    db = _ConsentFailingSession(fail_at_consent_index=1)

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(_request(), db=db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.status_code != 201
    # Round-13 review: the patient, coverage, and first consent flushed
    # ahead of this one are rolled back together with it — none of them
    # end up committed on their own.
    assert db.commit_count == 0


# --- Week 2-3 catch-up: adr/0004/RIV-160 match-key lookup -------------------


def test_no_ssn_means_no_match_lookup_and_unaffected_behavior(monkeypatch):
    # Without an ssn there is no reliable key (adr/0004 doesn't propose
    # matching on name/dob alone) — intake must behave exactly as before
    # this feature existed, even with existing candidate rows present.
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession(existing_patients=[_FakePatientRow(id=42, ssn="111223333", dob="1990-01-01")])

    result = _create_intake(_request(), db=db, x_request_id=None)  # default demographics has no ssn

    assert result.patient_id != 42
    assert result.possible_duplicate_match is False
    assert db.lock_calls == []  # no ssn -> no reliable key -> lock never acquired


def test_exact_match_always_blocks_with_409(monkeypatch):
    # Round-10 review (2026-08-05): there is no override anymore, period —
    # an exact SSN+DOB match always blocks, for every caller.
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession(existing_patients=[_FakePatientRow(id=42, ssn="111-22-3333", dob="1990-01-01")])

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(
            _request(demographics={"name": "Jane Roe", "ssn": "111-22-3333", "dob": "1990-01-01"}),
            db=db, x_request_id=None,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["confidence"] == "exact"
    # PR #20 round-8 review: no candidate patient_id in the response — this
    # endpoint has no auth dependency, so returning real ids would let an
    # unauthenticated caller enumerate patients via ssn/dob probing.
    assert "candidates" not in exc_info.value.detail
    # Nothing was persisted — the block happens before any create/commit.
    assert db.commit_count == 0
    assert db.added == []


def test_exact_match_ignores_smuggled_override_and_confirmed_by(monkeypatch):
    # Round-10 review: duplicate_override/confirmed_by (and the older
    # link_to_existing) no longer exist on IntakeRequest at all — not just
    # restricted to "create_new". Even a caller who sends them anyway in the
    # raw request body gets them silently dropped (no model_config
    # extra="forbid" here, so unknown fields are ignored, not rejected) and
    # the exact match still blocks. Proves there is no bypass left, smuggled
    # or otherwise — the explicit regression case the review asked for.
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession(existing_patients=[_FakePatientRow(id=42, ssn="111-22-3333", dob="1990-01-01")])

    req = _request(
        demographics={"name": "Jane Roe", "ssn": "111-22-3333", "dob": "1990-01-01"},
        duplicate_override="create_new",
        confirmed_by="dr.smith",
    )
    assert not hasattr(req, "duplicate_override")
    assert not hasattr(req, "confirmed_by")

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(req, db=db, x_request_id=None)

    assert exc_info.value.status_code == 409
    assert db.commit_count == 0
    assert db.added == []  # no new patient row appears


def test_partial_match_never_blocks_and_returns_possible_duplicate_flag(monkeypatch):
    # Same ssn, different dob — adr/0004's own worked Maria Gonzalez example
    # (three rows, one ssn, one differing dob).
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession(existing_patients=[_FakePatientRow(id=42, ssn="111-22-3333", dob="1971-02-03")])

    result = _create_intake(
        _request(demographics={"name": "M. Gonzalez", "ssn": "111-22-3333", "dob": "1971-03-02"}),
        db=db, x_request_id=None,
    )

    assert result.patient_id != 42
    # PR #20 round-8 review: a boolean flag only — never the candidate
    # patient_id, which this unauthenticated endpoint must not disclose.
    assert result.possible_duplicate_match is True


def test_partial_match_link_confirmed_by_never_comes_from_the_request_body(monkeypatch):
    # Round-10 review's second requested case: prove confirmed_by on any
    # written patient_links row is never taken from the caller. There is no
    # authenticated session anywhere in this service (a known, documented
    # gap), so the honest equivalent is that it's always None — even if a
    # caller tries to smuggle one in, it's silently dropped (no such field
    # exists on IntakeRequest anymore) and has zero effect on the link row.
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession(existing_patients=[_FakePatientRow(id=42, ssn="111-22-3333", dob="1971-02-03")])

    result = _create_intake(
        _request(
            demographics={"name": "M. Gonzalez", "ssn": "111-22-3333", "dob": "1971-03-02"},
            confirmed_by="dr.smith",  # not a real field anymore — silently ignored
        ),
        db=db, x_request_id=None,
    )

    assert result.possible_duplicate_match is True
    links = [obj for obj in db.added if type(obj).__tablename__ == "patient_links"]
    assert len(links) == 1
    assert links[0].confirmed is False
    assert links[0].confirmed_by is None


def test_partial_match_does_not_succeed_if_link_write_fails(monkeypatch):
    # Same failure mode, partial-match branch (the review's second requested
    # case). Round-13 review: the patient+link write only flushes now — the
    # failure that matters is create_intake's single top-level commit, which
    # is what finalizes (or, here, fails to finalize) the whole group.
    _mock_eligibility_post(monkeypatch)
    db = _RaisingFakeSession("simulated intake commit failure", raise_on="commit")
    db.existing_patients = [_FakePatientRow(id=42, ssn="111-22-3333", dob="1971-02-03")]

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(
            _request(demographics={"name": "M. Gonzalez", "ssn": "111-22-3333", "dob": "1971-03-02"}),
            db=db, x_request_id=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.status_code != 201
    assert db.commit_count == 0

    links = [obj for obj in db.added if type(obj).__tablename__ == "patient_links"]
    assert len(links) == 1
    assert links[0].confidence == "partial"
    assert links[0].confirmed is False
    assert links[0].confirmed_by is None
    assert links[0].basis == "ssn_match_dob_differs"


def test_exact_match_lock_is_scoped_to_normalized_ssn_not_raw_input(monkeypatch):
    # "412-55-9981" and "412559981" must serialize against each other —
    # the lock key has to be the normalized form, same as the match lookup
    # itself, or the two representations would race past each other.
    _mock_eligibility_post(monkeypatch)
    db = _FakeSession()

    _create_intake(
        _request(demographics={"name": "Jane Roe", "ssn": "412-55-9981", "dob": "1990-01-01"}),
        db=db, x_request_id=None,
    )

    assert db.lock_calls[0] == {"key": "412559981"}


def test_db_failure_during_lock_or_match_select_returns_503_with_rollback(monkeypatch):
    # Round-20 review (2026-08-06): _acquire_match_key_lock and
    # _find_match_candidates both issue real statements via db.execute() —
    # an advisory-lock acquisition, then a SELECT — but previously ran
    # outside any SQLAlchemyError handler. A DB timeout or statement
    # failure there used to surface as an unhandled 500 instead of this
    # service's rollback-then-503 convention. Needs an ssn so
    # _acquire_match_key_lock doesn't take its no-reliable-key early return
    # (which never calls db.execute() at all).
    _mock_eligibility_post(monkeypatch)
    db = _RaisingFakeSession("simulated lock/select failure", raise_on="execute")

    with pytest.raises(app_mod.HTTPException) as exc_info:
        _create_intake(
            _request(demographics={"name": "Jane Roe", "ssn": "111-22-3333", "dob": "1990-01-01"}),
            db=db, x_request_id=None,
        )

    assert exc_info.value.status_code == 503
    assert db.rollback_count == 1
    assert db.commit_count == 0
    assert db.added == []
