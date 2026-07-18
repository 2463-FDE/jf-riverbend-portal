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
import time

from conftest import load_module

app_mod = load_module("services/intake-service/app.py", "intake_app_endpoint")

IntakeRequest = load_module("services/intake-service/schemas.py", "intake_schemas_for_endpoint").IntakeRequest


class _FakeSession:
    def __init__(self):
        self.added = []
        self.commit_count = 0
        self._next_id = 1

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commit_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    def refresh(self, obj):
        pass

    def rollback(self):
        pass


def _request(**overrides):
    payload = {
        "demographics": {"name": "Jane Roe", "dob": "1990-01-01"},
        "insurance": {"payer_name": "Aetna", "member_id": "MEM1"},
        "consents": ["npp_ack", "treatment_consent"],
    }
    payload.update(overrides)
    return IntakeRequest(**payload)


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
    result = app_mod.create_intake(_request(), db=db, x_request_id=None)
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
    result = app_mod.create_intake(_request(), db=db, x_request_id=None)

    # The registration itself succeeded regardless of the eligibility hop.
    assert result.patient_id == 1
    assert result.eligibility_status == "unknown"
    assert result.eligibility_job_id is None
    # Patient, coverage, and both consents were all committed.
    table_names = {type(obj).__tablename__ for obj in db.added}
    assert table_names == {"patients", "insurance_coverages", "consents"}
    assert db.commit_count >= 4  # patient + coverage + 2 consents, each its own commit


def test_intake_without_insurance_never_calls_eligibility_service(monkeypatch):
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not be called when no insurance is supplied")

    monkeypatch.setattr(app_mod.httpx, "post", _post)

    db = _FakeSession()
    result = app_mod.create_intake(_request(insurance=None), db=db, x_request_id=None)

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
    result = app_mod.create_intake(_request(), db=db, x_request_id=None)

    dumped = result.model_dump_json()
    assert "Jane Roe" not in dumped
    assert "1990-01-01" not in dumped
