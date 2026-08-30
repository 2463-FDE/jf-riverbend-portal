"""records-service orchestration for the policy navigator (w-9-2-planner P3).

records-service hosts this for the same reason it hosts the patient-summary
agent (summary_agent_path.py): it already carries the LangChain v1 +
Bedrock dependency set (requirements.txt) and Postgres access. Unlike that
agent, this path is stateless — no patient, no grant, no draft, no persisted
trace, no question/answer/retrieved text ever written anywhere; a caller's
ROLE alone (never a patient id) determines what policy text they may see,
via libs/policy_navigator.scope_for_role. The one deliberate exception (W10
Final Stage 5 sub-slice 3): a `db` session, when the caller provides one,
persists durable token-usage accounting for whichever turns genuinely
called a real Bedrock model — never a draft, an audit row, or anything
else this path's own docstring already forbids.
"""
import os

import bedrock_usage
from config import settings
from libs.agent_provenance import ProvenanceLabel
from libs.embedding_client import EmbeddingClient, EmbeddingConfig
from libs.policy_corpus import BedrockPolicyEmbeddingProvider, PolicyRetriever
from libs.policy_navigator import PolicyNavigatorResult, run_policy_navigator, scope_for_role
from libs.tracing.spans import new_correlation_id
from logging_config import configure

log = configure(settings.service_name)

_PROVIDER = "bedrock"
_USE_CASE = "policy_navigator_chat"
_SAFE_UNAVAILABLE_REPLY = "The policy navigator isn't available right now. Please try again shortly."


def _policy_connection():
    # Lazy — mirrors libs/rag_corpus/vector_store.py::PgVectorStore: importing
    # this module must never require psycopg2/pgvector installed; only
    # actually connecting does.
    import psycopg2
    from pgvector.psycopg2 import register_vector

    conn = psycopg2.connect(
        host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
        user=settings.db_user, password=settings.db_password,
    )
    register_vector(conn)
    return conn


def _unavailable() -> PolicyNavigatorResult:
    return PolicyNavigatorResult(
        answer=_SAFE_UNAVAILABLE_REPLY, citations=(), label=ProvenanceLabel.FALLBACK.value,
        model_id=None, termination_reason="provider_error",
    )


def ask_policy_navigator(question: str, *, actor_role: str, model=None, db=None) -> PolicyNavigatorResult:
    """One stateless navigator turn, scoped to `actor_role`. Never raises —
    a retrieval-infrastructure problem (unconfigured embedding model,
    unreachable Postgres) degrades exactly like a Bedrock chat-model problem
    already does inside run_policy_navigator: a safe fallback reply, never
    an unhandled exception.

    Review fix PN-CONN-LEAK: the embedding provider is validated BEFORE any
    Postgres connection is opened — POLICY_EMBEDDING_MODEL_ID unset (this
    environment's actual default) used to leave `conn` open with nothing
    ever closing it, since the old code opened the connection first and the
    only `conn.close()` lived in a `finally` this exact failure never
    reached. A connection is now only ever opened once the provider is
    already known-good, and its own construction failure returns before
    `conn` exists at all.

    `db=None` (the default, and every existing test's call shape) skips
    usage persistence entirely — a caller (app.py's /policy/ask route) that
    wants durable usage accounting passes its own request-scoped session.
    The correlation id minted here exists ONLY as this one write's
    idempotency key; nothing else about this stateless path uses it.
    """
    scope = scope_for_role(actor_role)
    model_id = os.getenv("POLICY_EMBEDDING_MODEL_ID", "")
    try:
        embedding_client = EmbeddingClient(
            config=EmbeddingConfig(provider=_PROVIDER),
            provider=BedrockPolicyEmbeddingProvider(model_id=model_id or None),
        )
    except Exception as exc:
        log.warning("policy navigator embedding provider unavailable (error_type=%s)", type(exc).__name__)
        return _unavailable()

    try:
        conn = _policy_connection()
    except Exception as exc:
        log.warning("policy navigator retrieval unavailable (error_type=%s)", type(exc).__name__)
        return _unavailable()

    try:
        from pgvector import Vector  # lazy — see _policy_connection

        retriever = PolicyRetriever(
            conn, embedding_client, provider=_PROVIDER, model=model_id, vector_cast=Vector,
        )
        result = run_policy_navigator(question, scope=scope, retriever=retriever, model=model)
    finally:
        conn.close()

    if db is not None and result.usage:
        bedrock_usage.persist(db, new_correlation_id(), [
            bedrock_usage.UsageEvent(
                provider=_PROVIDER, model_id=turn.model_id, use_case=_USE_CASE, sequence=turn.turn,
                input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
            )
            for turn in result.usage
        ])
    return result
