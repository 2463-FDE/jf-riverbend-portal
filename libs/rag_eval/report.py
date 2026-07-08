"""Renders an EvalReport as Markdown. Includes only ids, counts, and rates —
never raw corpus/query text or embedding vectors. Patient ids/names that do
appear are the same synthetic seed fixtures already committed elsewhere in
the repo (db/seed/patients.csv), not newly-invented PHI-like data.
"""
from .metrics import EvalReport


def render_markdown(report: EvalReport) -> str:
    lines = [
        "# Retrieval-Eval Report",
        "",
        f"- **Embedding provider:** `{report.provider_name}`",
        f"- **top_k:** {report.top_k}",
        f"- **Gold-set cases:** {report.total_cases} (`db/seed/goldset.json`)",
        "",
        "## Metrics",
        "",
        f"- **recall@{report.top_k}:** {report.recall_at_k:.1f}%",
        f"- **precision@{report.top_k}:** {report.precision_at_k:.1f}%",
        f"- **duplicate-rate:** {report.duplicate_rate:.1f}%",
        f"- **fragment-coverage gap:** {report.fragment_coverage_gap:.1f}%",
        "",
        "## Duplicate clusters (identity proxy: normalized SSN match)",
        "",
    ]
    for cluster in report.duplicate_clusters:
        lines.append(f"- patient_ids {cluster}")
    lines += [
        "",
        "## Per-case detail",
        "",
        "| Query | Expected patient | Expected record(s) | Retrieved record(s) | recall hit | fragment gap |",
        "|---|---|---|---|---|---|",
    ]
    for case in report.per_case:
        lines.append(
            f"| {case['query']} | {case['expected_patient_id']} | "
            f"{', '.join(case['expected_record_ids'])} | "
            f"{', '.join(case['retrieved_record_ids']) or '(none)'} | "
            f"{'yes' if case['recall_hit'] else 'no'} | "
            f"{'yes' if case['fragment_coverage_gap'] else 'no'} |"
        )
    return "\n".join(lines)
