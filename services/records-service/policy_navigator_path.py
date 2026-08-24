"""records-service orchestration for the policy navigator (w-9-2-planner P3).

records-service hosts this for the same reason it hosts the patient-summary
agent (summary_agent_path.py): it already carries the LangChain v1 +
Bedrock dependency set (requirements.txt) and Postgres access. Unlike that
agent, this path is stateless — no patient, no grant, no draft, no persisted
trace; a caller's ROLE alone (never a patient id) determines what policy
text they may see, via libs/policy_navigator.scope_for_role.
"""
import os

from config import settings
from libs.agent_provenance import ProvenanceLabel
from libs.embedding_client import EmbeddingClient, EmbeddingConfig
from libs.policy_corpus import BedrockPolicyEmbeddingProvider, PolicyRetriever
from libs.policy_navigator import PolicyNavigatorResult, run_policy_navigator, scope_for_role
from logging_config import configure

log = configure(settings.service_name)

_PROVIDER = "bedrock"
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


def ask_policy_navigator(question: str, *, actor_role: str, model=None) -> PolicyNavigatorResult:
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
        return run_policy_navigator(question, scope=scope, retriever=retriever, model=model)
    finally:
        conn.close()
