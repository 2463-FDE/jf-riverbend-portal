"""services/records-service/bedrock_usage.py — durable Bedrock chat usage
accounting (migration 037, W10 Final Stage 5 sub-slice 3). Fast DB-less
(SQLite) coverage of persist()/usage_for()'s own contract; real Postgres
append-only/idempotency enforcement is proved in
tests/integration/test_bedrock_usage_events_migration.py.
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module
from libs.bedrock_pricing import RATES

bedrock_usage = load_module("services/records-service/bedrock_usage.py", "bedrock_usage_mod")
BedrockUsageEvent = bedrock_usage.BedrockUsageEvent
UsageEvent = bedrock_usage.UsageEvent


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    BedrockUsageEvent.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_persist_writes_one_row_per_turn(db):
    events = [
        UsageEvent(provider="bedrock", model_id="model-x", use_case="summary_agent_chat",
                  sequence=1, input_tokens=100, output_tokens=20),
        UsageEvent(provider="bedrock", model_id="model-x", use_case="summary_agent_chat",
                  sequence=2, input_tokens=110, output_tokens=25),
    ]
    bedrock_usage.persist(db, "corr-1", events)
    db.commit()

    rows = bedrock_usage.usage_for(db, model_id="model-x")
    assert len(rows) == 2
    assert {r.idempotency_key for r in rows} == {"corr-1:1", "corr-1:2"}
    assert rows[0].input_tokens == 100 and rows[0].output_tokens == 20


def test_persist_of_an_empty_event_list_is_a_no_op(db):
    bedrock_usage.persist(db, "corr-empty", [])
    db.commit()

    assert bedrock_usage.usage_for(db) == []


def test_persist_is_idempotent_for_a_repeated_turn(db):
    """Make retries/double recording deterministic: a retried write of the
    SAME correlation_id+turn must be a no-op, never a duplicate row."""
    event = UsageEvent(provider="bedrock", model_id="model-x", use_case="summary_agent_chat",
                       sequence=1, input_tokens=100, output_tokens=20)
    bedrock_usage.persist(db, "corr-retry", [event])
    db.commit()
    bedrock_usage.persist(db, "corr-retry", [event])
    db.commit()

    rows = bedrock_usage.usage_for(db, model_id="model-x")
    assert len(rows) == 1


def test_persist_reraises_a_non_duplicate_integrity_violation(db):
    """Review fix BU-ERR-SWALLOW: only the exact idempotency_key UNIQUE
    violation is a no-op — a NOT NULL violation (model_id missing here) is
    a real bug and must propagate, never be silently treated like a
    harmless retry."""
    bad_event = UsageEvent(provider="bedrock", model_id=None, use_case="summary_agent_chat", sequence=1)

    with pytest.raises(IntegrityError):
        bedrock_usage.persist(db, "corr-bad", [bad_event])


def test_usage_for_filters_by_model_id_and_use_case(db):
    bedrock_usage.persist(db, "corr-a", [
        UsageEvent(provider="bedrock", model_id="model-x", use_case="summary_agent_chat", sequence=1),
    ])
    bedrock_usage.persist(db, "corr-b", [
        UsageEvent(provider="bedrock", model_id="model-y", use_case="policy_navigator_chat", sequence=1),
    ])
    db.commit()

    assert len(bedrock_usage.usage_for(db, model_id="model-x")) == 1
    assert len(bedrock_usage.usage_for(db, use_case="policy_navigator_chat")) == 1
    assert len(bedrock_usage.usage_for(db, model_id="model-x", use_case="policy_navigator_chat")) == 0


# --- W10 Metrics Stage 4: versioned cost computation on persist -----------


def test_persist_populates_cost_and_rate_version_for_a_known_model(db):
    model_id = next(iter(RATES))
    entry = RATES[model_id]
    event = UsageEvent(provider="bedrock", model_id=model_id, use_case="summary_agent_chat",
                       sequence=1, input_tokens=1_000_000, output_tokens=1_000_000)

    bedrock_usage.persist(db, "corr-priced", [event])
    db.commit()

    row = bedrock_usage.usage_for(db, model_id=model_id)[0]
    assert row.rate_version == entry.rate_version
    assert row.cost_usd == entry.input_usd_per_million + entry.output_usd_per_million


def test_persist_leaves_cost_and_rate_version_null_for_an_unpriced_model(db):
    event = UsageEvent(provider="bedrock", model_id="not-a-real-model", use_case="summary_agent_chat",
                       sequence=1, input_tokens=100, output_tokens=20)

    bedrock_usage.persist(db, "corr-unpriced", [event])
    db.commit()

    row = bedrock_usage.usage_for(db, model_id="not-a-real-model")[0]
    assert row.rate_version is None
    assert row.cost_usd is None


def test_persist_leaves_cost_null_for_a_row_with_no_reported_tokens(db):
    """No tokens reported at all (e.g. the eligibility Converse port before
    W10 Metrics Stage 4) is a different case from an unpriced model with
    real tokens — neither should ever compute a cost, but only the latter
    is a `rate_unavailable` event."""
    model_id = next(iter(RATES))
    event = UsageEvent(provider="bedrock", model_id=model_id, use_case="summary_agent_chat", sequence=1)

    bedrock_usage.persist(db, "corr-no-tokens", [event])
    db.commit()

    row = bedrock_usage.usage_for(db, model_id=model_id)[0]
    assert row.cost_usd is None
    assert row.rate_version is None


def test_usage_for_filters_by_time_window(db):
    bedrock_usage.persist(db, "corr-a", [
        UsageEvent(provider="bedrock", model_id="model-x", use_case="summary_agent_chat", sequence=1),
    ])
    db.commit()

    from datetime import datetime, timedelta
    future = datetime.now() + timedelta(days=1)
    assert bedrock_usage.usage_for(db, since=future) == []
    past = datetime.now() - timedelta(days=1)
    assert len(bedrock_usage.usage_for(db, since=past)) == 1
