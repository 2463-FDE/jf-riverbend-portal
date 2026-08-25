#!/usr/bin/env python3
"""Sanitized freshness and real-vector retrieval evaluation for policy RAG."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2  # noqa: E402
from pgvector import Vector  # noqa: E402
from pgvector.psycopg2 import register_vector  # noqa: E402

from libs.embedding_client import EmbeddingClient, EmbeddingConfig  # noqa: E402
from libs.policy_corpus import BedrockPolicyEmbeddingProvider, PolicyRetriever, check_corpus_freshness  # noqa: E402
from libs.policy_corpus.evaluation import (  # noqa: E402
    KeywordPolicyRetriever,
    evaluate_retrieval,
    load_aliases,
    load_case_overrides,
    load_evaluation_cases,
)
from libs.policy_corpus.manifest import load_manifest  # noqa: E402
from libs.policy_navigator import scope_for_role  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MANIFEST = os.path.join(_ROOT, "docs", "RagDocs", "manifest.json")
_EVALUATIONS = os.path.join(
    _ROOT, "docs", "client-inputs", "2026-08-24", "evaluations", "retrieval-evaluations.jsonl"
)
_ALIASES = os.path.join(
    _ROOT, "docs", "client-inputs", "2026-08-24", "evaluations", "citation-aliases.json"
)
_PROVIDER = "bedrock"


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    model_id = os.getenv("POLICY_EMBEDDING_MODEL_ID", "")
    if not model_id or model_id == "changeme":
        raise SystemExit("POLICY_EMBEDDING_MODEL_ID is not configured — see .env.example")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        register_vector(conn)
        freshness = check_corpus_freshness(conn, _MANIFEST, provider=_PROVIDER, model=model_id)
        output = {"freshness": freshness.as_dict()}
        if not freshness.is_fresh or args.verify_only:
            print(json.dumps(output, indent=2, sort_keys=True))
            return 0 if freshness.is_fresh else 2

        # Constructed here, not above the verify-only return: a DB-only parity
        # check must not require AWS_REGION (EVAL-VERIFY-BEDROCK-REGION).
        embedding_client = EmbeddingClient(
            config=EmbeddingConfig(provider=_PROVIDER),
            provider=BedrockPolicyEmbeddingProvider(model_id=model_id),
        )
        cases = load_evaluation_cases(_EVALUATIONS)
        aliases = load_aliases(_ALIASES)
        case_overrides = load_case_overrides(_ALIASES)
        manifest = load_manifest(_MANIFEST)
        vector_retriever = PolicyRetriever(
            conn, embedding_client, provider=_PROVIDER, model=model_id, vector_cast=Vector,
        )
        vector_report = evaluate_retrieval(
            cases, aliases=aliases, manifest=manifest, retriever=vector_retriever,
            scope_resolver=scope_for_role, top_k=args.top_k, case_overrides=case_overrides,
        )
        keyword_report = evaluate_retrieval(
            cases, aliases=aliases, manifest=manifest, retriever=KeywordPolicyRetriever(_MANIFEST),
            scope_resolver=scope_for_role, top_k=args.top_k, case_overrides=case_overrides,
        )
        output.update(
            {
                "top_k": args.top_k,
                "vector": vector_report.as_dict(),
                "keyword_baseline": keyword_report.as_dict(),
            }
        )
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if not vector_report.unauthorized_retrieval_count and not vector_report.forbidden_citation_count else 3
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
