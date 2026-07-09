"""Retrieval-eval harness: measures the Commit 2 corpus/embedding pipeline
(`libs/rag_corpus`) against `db/seed/goldset.json` and reports recall@k,
precision@k, and two fragmentation metrics (duplicate-rate,
fragment-coverage gap) tied to AUD-09. Measurement only — does not implement
retrieval for production use and does not fix AUD-09; see
`adr/0004-master-patient-index-match-key.md` for the proposed fix.
"""
from .goldset import GoldCase, load_goldset
from .harness import EvalConfig, run_eval
from .metrics import EvalReport, compute_metrics
from .report import render_markdown

__all__ = [
    "GoldCase",
    "load_goldset",
    "EvalConfig",
    "run_eval",
    "EvalReport",
    "compute_metrics",
    "render_markdown",
]
