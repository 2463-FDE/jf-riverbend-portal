"""The synthetic training corpus, loaded from `manifest.json`.

Four short documents, three approved and one deliberately not. None of it is
real Riverbend policy and none of it is PHI — the manifest's own `notice` field
says so, so a demo can show that claim rather than assert it verbally.

`approved` is a property of the DOCUMENT, never a parameter a caller or a model
can pass. `retrieval.py` explains why that distinction is the whole defence.
"""
import json
import os
from dataclasses import dataclass
from typing import Optional

_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.json")


@dataclass(frozen=True)
class CorpusDocument:
    source_id: str
    source_version: str
    title: str
    category: str
    audiences: tuple
    approved: bool
    text: str

    @property
    def citation_id(self) -> str:
        """Pins the version into the citation the model has to utter, so a draft
        cannot cite "POL-001" loosely and silently resolve to a later revision —
        the same reason `agent_draft_citation` stores the version separately."""
        return f"{self.source_id}@{self.source_version}"


@dataclass(frozen=True)
class Corpus:
    corpus_id: str
    notice: str
    documents: tuple

    def approved_for(self, audience: str, category: Optional[str] = None) -> tuple:
        """Approved documents for this audience, optionally narrowed by category.
        Category only ever NARROWS an already-approved, already-scoped set, which
        is why it is safe to let a model choose it."""
        return tuple(
            d for d in self.documents
            if d.approved and audience in d.audiences
            and (not category or d.category == category)
        )


def load_corpus(path: Optional[str] = None) -> Corpus:
    with open(path or _MANIFEST_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    return Corpus(
        corpus_id=raw["corpus_id"], notice=raw["notice"],
        # **d rather than a field list: an unexpected manifest key should be a
        # loud TypeError, not a value silently dropped on the floor.
        documents=tuple(CorpusDocument(**{**d, "audiences": tuple(d["audiences"])})
                        for d in raw["documents"]),
    )
