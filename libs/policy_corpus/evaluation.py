"""Executable retrieval evaluation for the August 24 client cases.

This evaluates retrieval and authorization filtering, not clinical prose.
Questions are synthetic and remain ephemeral; reports contain eval IDs and
source/citation metadata only.
"""
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple

from .chunking import chunk_markdown
from .contracts import PolicyManifest
from .manifest import load_ingestable_documents, load_manifest
from .retrieval import RetrievalScope, RetrievedChunk

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class EvaluationContractError(ValueError):
    pass


@dataclass(frozen=True)
class AliasTarget:
    status: str
    source_id: Optional[str]
    source_version: Optional[str]
    reason: str = ""

    @property
    def identity(self) -> Optional[str]:
        if self.source_id and self.source_version:
            return f"{self.source_id}@{self.source_version}"
        return None


@dataclass(frozen=True)
class EvaluationCase:
    eval_id: str
    actor_roles: Tuple[str, ...]
    expected_behavior: str
    question: str
    required_citation_ids: Tuple[str, ...]
    forbidden_citation_ids: Tuple[str, ...]
    refusal_or_escalation: bool


@dataclass(frozen=True)
class ClassifiedCase:
    case: EvaluationCase
    classification: str
    reason: str
    required_targets: Tuple[str, ...]
    forbidden_targets: Tuple[str, ...]


@dataclass(frozen=True)
class CaseResult:
    eval_id: str
    classification: str
    actor_role: str
    retrieved_source_ids: Tuple[str, ...]
    retrieved_citation_ids: Tuple[str, ...]
    required_targets: Tuple[str, ...]
    missing_targets: Tuple[str, ...]
    forbidden_hits: Tuple[str, ...]
    unauthorized_hits: Tuple[str, ...]
    reason: str
    # W10 Metrics Stage 5 (MRR): every retrieved chunk's `source_id@version`
    # identity, IN RETRIEVAL RANK ORDER, deduped by identity (first
    # occurrence kept) — unlike retrieved_source_ids/retrieved_citation_ids
    # above, this carries the version too, so it matches required_targets'
    # own identity shape exactly. Never a query, document, or citation TEXT
    # — only the same source_id/version identifiers already exposed above.
    retrieved_identities: Tuple[str, ...] = ()

    @property
    def retrieval_passed(self) -> bool:
        if self.classification not in {"runnable", "negative"}:
            return False
        return not self.missing_targets and not self.forbidden_hits and not self.unauthorized_hits

    def as_dict(self) -> dict:
        return {
            "eval_id": self.eval_id,
            "classification": self.classification,
            "actor_role": self.actor_role,
            "retrieval_passed": self.retrieval_passed,
            "retrieved_source_ids": list(self.retrieved_source_ids),
            "retrieved_citation_ids": list(self.retrieved_citation_ids),
            "retrieved_identities": list(self.retrieved_identities),
            "required_targets": list(self.required_targets),
            "missing_targets": list(self.missing_targets),
            "forbidden_hits": list(self.forbidden_hits),
            "unauthorized_hits": list(self.unauthorized_hits),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvaluationReport:
    total_cases: int
    runnable_cases: int
    negative_cases: int
    deferred_cases: int
    spec_conflicts: int
    required_targets: int
    required_hits: int
    retrieved_sources_for_runnable: int
    forbidden_citation_count: int
    unauthorized_retrieval_count: int
    results: Tuple[CaseResult, ...]

    @property
    def recall_at_k(self) -> Optional[float]:
        return self.required_hits / self.required_targets if self.required_targets else None

    @property
    def precision_at_k(self) -> Optional[float]:
        if not self.retrieved_sources_for_runnable:
            return None
        return self.required_hits / self.retrieved_sources_for_runnable

    @property
    def citation_target_accuracy(self) -> Optional[float]:
        runnable = [r for r in self.results if r.classification == "runnable"]
        return sum(r.retrieval_passed for r in runnable) / len(runnable) if runnable else None

    @property
    def case_coverage(self) -> float:
        return (self.runnable_cases + self.negative_cases) / self.total_cases if self.total_cases else 0.0

    def as_dict(self) -> dict:
        return {
            "total_cases": self.total_cases,
            "runnable_cases": self.runnable_cases,
            "negative_cases": self.negative_cases,
            "deferred_cases": self.deferred_cases,
            "spec_conflicts": self.spec_conflicts,
            "case_coverage": self.case_coverage,
            "recall_at_k": self.recall_at_k,
            "precision_at_k": self.precision_at_k,
            "citation_target_accuracy": self.citation_target_accuracy,
            "forbidden_citation_count": self.forbidden_citation_count,
            "unauthorized_retrieval_count": self.unauthorized_retrieval_count,
            "agent_refusal_accuracy": None,
            "agent_refusal_status": "not_evaluated_by_retrieval_harness",
            "results": [result.as_dict() for result in self.results],
        }


def load_aliases(path: str) -> Mapping[str, AliasTarget]:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if raw.get("schema_version") != 1 or not isinstance(raw.get("aliases"), dict):
        raise EvaluationContractError("citation alias file must use schema_version=1 and an aliases object")

    aliases = {}
    for citation_id, value in raw["aliases"].items():
        if citation_id in aliases or not isinstance(value, dict):
            raise EvaluationContractError(f"invalid alias entry for {citation_id!r}")
        status = value.get("status")
        if status not in {"mapped", "excluded"}:
            raise EvaluationContractError(f"{citation_id}: unsupported alias status {status!r}")
        target = AliasTarget(
            status=status,
            source_id=value.get("source_id"),
            source_version=value.get("source_version"),
            reason=value.get("reason", ""),
        )
        if status == "mapped" and not target.identity:
            raise EvaluationContractError(f"{citation_id}: mapped alias requires source_id and source_version")
        aliases[citation_id] = target
    return aliases


def load_case_overrides(path: str) -> Mapping[str, Mapping[str, str]]:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    overrides = raw.get("case_overrides", {})
    if not isinstance(overrides, dict):
        raise EvaluationContractError("case_overrides must be an object")
    result = {}
    for eval_id, value in overrides.items():
        if not isinstance(value, dict) or value.get("classification") not in {"deferred", "spec_conflict"}:
            raise EvaluationContractError(f"{eval_id}: invalid case override")
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise EvaluationContractError(f"{eval_id}: case override requires a reason")
        result[eval_id] = {"classification": value["classification"], "reason": reason}
    return result


def load_evaluation_cases(path: str) -> Tuple[EvaluationCase, ...]:
    cases = []
    seen = set()
    with open(path, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                case = EvaluationCase(
                    eval_id=raw["eval_id"],
                    actor_roles=tuple(raw["actor_roles"]),
                    expected_behavior=raw["expected_behavior"],
                    question=raw["question"],
                    required_citation_ids=tuple(raw["required_citation_ids"]),
                    forbidden_citation_ids=tuple(raw["forbidden_citation_ids"]),
                    refusal_or_escalation=bool(raw["refusal_or_escalation"]),
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise EvaluationContractError(f"invalid evaluation case at line {line_number}") from exc
            if case.eval_id in seen:
                raise EvaluationContractError(f"duplicate eval_id {case.eval_id}")
            if len(case.actor_roles) != 1:
                raise EvaluationContractError(f"{case.eval_id}: exactly one actor role is required")
            seen.add(case.eval_id)
            cases.append(case)
    return tuple(cases)


def _manifest_identities(manifest: PolicyManifest):
    return {f"{doc.source_id}@{doc.source_version}": doc for doc in manifest.documents}


def _resolve(citation_id: str, aliases: Mapping[str, AliasTarget], manifest: PolicyManifest) -> AliasTarget:
    if citation_id in aliases:
        return aliases[citation_id]
    if citation_id in _manifest_identities(manifest):
        source_id, source_version = citation_id.split("@", 1)
        return AliasTarget(status="mapped", source_id=source_id, source_version=source_version)
    raise EvaluationContractError(f"no citation mapping for {citation_id}")


def classify_case(
    case: EvaluationCase, aliases: Mapping[str, AliasTarget], manifest: PolicyManifest,
    case_overrides: Mapping[str, Mapping[str, str]] = None,
) -> ClassifiedCase:
    identities = _manifest_identities(manifest)
    required_targets = []
    forbidden_targets = []
    excluded_required = []

    for citation_id in case.required_citation_ids:
        target = _resolve(citation_id, aliases, manifest)
        if target.status == "excluded":
            excluded_required.append(f"{citation_id}:{target.reason or 'excluded'}")
        elif target.identity:
            required_targets.append(target.identity)

    for citation_id in case.forbidden_citation_ids:
        target = _resolve(citation_id, aliases, manifest)
        if target.source_id:
            forbidden_targets.append(target.source_id)
        else:
            forbidden_targets.append(citation_id.split("@", 1)[0])

    override = (case_overrides or {}).get(case.eval_id)
    if override:
        return ClassifiedCase(
            case, override["classification"], override["reason"],
            tuple(sorted(set(required_targets))), tuple(sorted(set(forbidden_targets))),
        )

    if excluded_required:
        return ClassifiedCase(
            case, "deferred", ";".join(sorted(excluded_required)),
            tuple(sorted(set(required_targets))), tuple(sorted(set(forbidden_targets))),
        )

    inactive = [identity for identity in required_targets if identity not in identities or not identities[identity].is_ingestable]
    if inactive:
        return ClassifiedCase(
            case, "spec_conflict", "required_source_not_active:" + ",".join(sorted(inactive)),
            tuple(sorted(set(required_targets))), tuple(sorted(set(forbidden_targets))),
        )

    classification = "runnable" if case.required_citation_ids else "negative"
    return ClassifiedCase(
        case, classification, "",
        tuple(sorted(set(required_targets))), tuple(sorted(set(forbidden_targets))),
    )


def _authorized(chunk: RetrievedChunk, scope: RetrievalScope, manifest: PolicyManifest) -> bool:
    doc = next(
        (
            item for item in manifest.documents
            if item.source_id == chunk.source_id and item.source_version == chunk.source_version
        ),
        None,
    )
    if doc is None or not doc.is_ingestable:
        return False
    return bool(set(doc.audiences) & set(scope.audiences)) and bool(set(doc.workflows) & set(scope.workflows))


def evaluate_retrieval(
    cases: Sequence[EvaluationCase], *, aliases: Mapping[str, AliasTarget], manifest: PolicyManifest,
    retriever, scope_resolver: Callable[[str], RetrievalScope], top_k: int,
    case_overrides: Mapping[str, Mapping[str, str]] = None,
) -> EvaluationReport:
    if top_k <= 0:
        raise EvaluationContractError("top_k must be positive")

    results = []
    for case in cases:
        classified = classify_case(case, aliases, manifest, case_overrides)
        actor_role = case.actor_roles[0]
        if classified.classification in {"deferred", "spec_conflict"}:
            results.append(
                CaseResult(
                    eval_id=case.eval_id, classification=classified.classification, actor_role=actor_role,
                    retrieved_source_ids=(), retrieved_citation_ids=(),
                    required_targets=classified.required_targets, missing_targets=classified.required_targets,
                    forbidden_hits=(), unauthorized_hits=(), reason=classified.reason,
                )
            )
            continue

        scope = scope_resolver(actor_role)
        chunks = retriever.retrieve(case.question, scope, top_k)
        source_ids = tuple(dict.fromkeys(chunk.source_id for chunk in chunks))
        citation_ids = tuple(dict.fromkeys(chunk.citation_id for chunk in chunks))
        # Ordered (rank-preserving), deduped by identity — the MRR input.
        # Different from the two dedup tuples above: this one keeps the
        # source VERSION too, matching required_targets' own identity shape.
        ordered_identities = tuple(dict.fromkeys(f"{chunk.source_id}@{chunk.source_version}" for chunk in chunks))
        missing = tuple(sorted(set(classified.required_targets) - set(ordered_identities)))
        forbidden = tuple(sorted(set(source_ids) & set(classified.forbidden_targets)))
        unauthorized = tuple(sorted({chunk.citation_id for chunk in chunks if not _authorized(chunk, scope, manifest)}))
        results.append(
            CaseResult(
                eval_id=case.eval_id, classification=classified.classification, actor_role=actor_role,
                retrieved_source_ids=source_ids, retrieved_citation_ids=citation_ids,
                retrieved_identities=ordered_identities,
                required_targets=classified.required_targets, missing_targets=missing,
                forbidden_hits=forbidden, unauthorized_hits=unauthorized, reason="",
            )
        )

    result_tuple = tuple(results)
    runnable = [result for result in result_tuple if result.classification == "runnable"]
    required_targets = sum(len(result.required_targets) for result in runnable)
    required_hits = sum(len(result.required_targets) - len(result.missing_targets) for result in runnable)
    return EvaluationReport(
        total_cases=len(result_tuple),
        runnable_cases=sum(result.classification == "runnable" for result in result_tuple),
        negative_cases=sum(result.classification == "negative" for result in result_tuple),
        deferred_cases=sum(result.classification == "deferred" for result in result_tuple),
        spec_conflicts=sum(result.classification == "spec_conflict" for result in result_tuple),
        required_targets=required_targets,
        required_hits=required_hits,
        retrieved_sources_for_runnable=sum(len(result.retrieved_source_ids) for result in runnable),
        forbidden_citation_count=sum(len(result.forbidden_hits) for result in result_tuple),
        unauthorized_retrieval_count=sum(len(result.unauthorized_hits) for result in result_tuple),
        results=result_tuple,
    )


class KeywordPolicyRetriever:
    """Deterministic metadata-filtered keyword baseline for comparison only."""

    def __init__(self, manifest_path: str):
        self._manifest = load_manifest(manifest_path)
        self._rows = []
        for doc, text in load_ingestable_documents(manifest_path):
            for chunk in chunk_markdown(
                source_id=doc.source_id,
                source_version=doc.source_version,
                markdown_text=text,
                config=self._manifest.ingestion.chunking,
            ):
                self._rows.append((doc, chunk))

    def retrieve(self, query: str, scope: RetrievalScope, limit: int) -> Sequence[RetrievedChunk]:
        if not scope.audiences or not scope.workflows:
            return []
        query_tokens = set(_TOKEN_RE.findall(query.lower()))
        ranked = []
        for doc, chunk in self._rows:
            if not set(doc.audiences) & set(scope.audiences) or not set(doc.workflows) & set(scope.workflows):
                continue
            if scope.topic and scope.topic not in doc.topics:
                continue
            chunk_tokens = set(_TOKEN_RE.findall(chunk.text.lower()))
            overlap = len(query_tokens & chunk_tokens)
            if overlap:
                ranked.append((overlap / max(len(query_tokens), 1), doc, chunk))
        ranked.sort(key=lambda item: (-item[0], item[2].chunk_id))
        return [
            RetrievedChunk(
                citation_id=chunk.chunk_id, source_id=doc.source_id, source_version=doc.source_version,
                title=doc.title, effective_date=doc.effective_date, section_id=chunk.section_id,
                heading_path=chunk.heading_path, score=score, text=chunk.text,
            )
            for score, doc, chunk in ranked[:limit]
        ]
