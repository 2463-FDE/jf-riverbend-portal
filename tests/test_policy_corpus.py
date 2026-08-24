"""Tests for the synthetic policy RAG corpus foundation
(libs/policy_corpus) — manifest validation and deterministic section
chunking. No embedding, retrieval, or persistence logic exists yet (see
w-9-2-planner P2 scope), so these tests cover exactly what's implemented:
the ingestion contract's pre-content checks and the chunker's determinism.
"""
import hashlib
import json
import os

import pytest

from libs.policy_corpus import (
    ManifestValidationError,
    chunk_markdown,
    load_ingestable_documents,
    load_manifest,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_MANIFEST_PATH = os.path.join(REPO_ROOT, "docs", "RagDocs", "manifest.json")


def _write_manifest(tmp_path, documents, *, max_documents=32, max_document_bytes=20000, chunking_overrides=None):
    chunking = {
        "strategy": "markdown_heading_sections",
        "max_characters": 1200,
        "overlap_characters": 120,
        "minimum_characters": 80,
        "preserve_heading_path": True,
    }
    chunking.update(chunking_overrides or {})
    manifest = {
        "schema_version": 1,
        "corpus_id": "test-corpus",
        "notice": "SYNTHETIC TEST CORPUS.",
        "ingestion": {
            "content_root": ".",
            "allowed_extensions": [".md"],
            "encoding": "utf-8",
            "max_document_bytes": max_document_bytes,
            "max_documents": max_documents,
            "chunking": chunking,
            "required_chunk_metadata": ["source_id", "source_version", "section_id"],
        },
        "documents": documents,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return str(manifest_path)


def _doc_entry(*, source_id="POL-1", source_version="1.0", content_path="policy.md", content_sha256=None, **overrides):
    entry = {
        "source_id": source_id,
        "source_version": source_version,
        "effective_date": "2026-08-01",
        "title": "Test Policy",
        "owner": "Test Owner",
        "approval_status": "approved_training",
        "synthetic": True,
        "retrieval_enabled": True,
        "content_path": content_path,
        "content_sha256": content_sha256 or "",
        "audiences": ["patient"],
        "workflows": ["patient_summary"],
        "topics": ["testing"],
        "allowed_uses": ["training_grounding"],
        "prohibited_uses": ["clinical_decision"],
        "relationships": [],
    }
    entry.update(overrides)
    return entry


def _write_content(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- manifest validation: structural rejections -------------------------------


def test_rejects_absolute_content_path(tmp_path):
    manifest_path = _write_manifest(tmp_path, [_doc_entry(content_path="/etc/passwd")])

    with pytest.raises(ManifestValidationError, match="relative"):
        load_ingestable_documents(manifest_path)


def test_rejects_path_traversal_that_escapes_content_root(tmp_path):
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.md").write_text("secret", encoding="utf-8")
    manifest_path = _write_manifest(tmp_path, [_doc_entry(content_path="../outside/secret.md")])

    with pytest.raises(ManifestValidationError, match="escapes content_root"):
        load_ingestable_documents(manifest_path)


def test_rejects_a_symlink_that_escapes_content_root(tmp_path):
    outside_dir = tmp_path.parent / "outside_symlink_target"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.md").write_text("secret", encoding="utf-8")
    link_path = tmp_path / "policy.md"
    os.symlink(outside_dir / "secret.md", link_path)
    manifest_path = _write_manifest(tmp_path, [_doc_entry(content_path="policy.md")])

    with pytest.raises(ManifestValidationError, match="escapes content_root"):
        load_ingestable_documents(manifest_path)


def test_rejects_unsupported_extension(tmp_path):
    _write_content(tmp_path, "policy.txt", "# Title\nbody")
    manifest_path = _write_manifest(tmp_path, [_doc_entry(content_path="policy.txt")])

    with pytest.raises(ManifestValidationError, match="unsupported extension"):
        load_ingestable_documents(manifest_path)


def test_rejects_oversize_file(tmp_path):
    _write_content(tmp_path, "policy.md", "x" * 200)
    manifest_path = _write_manifest(tmp_path, [_doc_entry(content_path="policy.md")], max_document_bytes=100)

    with pytest.raises(ManifestValidationError, match="exceeds max_document_bytes"):
        load_ingestable_documents(manifest_path)


def test_rejects_duplicate_source_id_and_version(tmp_path):
    digest_a = _write_content(tmp_path, "a.md", "# A")
    digest_b = _write_content(tmp_path, "b.md", "# B")
    manifest_path = _write_manifest(
        tmp_path,
        [
            _doc_entry(source_id="POL-1", source_version="1.0", content_path="a.md", content_sha256=digest_a),
            _doc_entry(source_id="POL-1", source_version="1.0", content_path="b.md", content_sha256=digest_b),
        ],
    )

    with pytest.raises(ManifestValidationError, match="duplicate"):
        load_ingestable_documents(manifest_path)


def test_rejects_missing_content_file(tmp_path):
    manifest_path = _write_manifest(tmp_path, [_doc_entry(content_path="does_not_exist.md")])

    with pytest.raises(ManifestValidationError, match="missing"):
        load_ingestable_documents(manifest_path)


def test_rejects_content_hash_mismatch(tmp_path):
    _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path, [_doc_entry(content_path="policy.md", content_sha256="0" * 64)]
    )

    with pytest.raises(ManifestValidationError, match="content_sha256 mismatch"):
        load_ingestable_documents(manifest_path)


def test_rejects_an_unsupported_chunking_strategy(tmp_path):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path,
        [_doc_entry(content_path="policy.md", content_sha256=digest)],
        chunking_overrides={"strategy": "semantic_paragraphs"},
    )

    with pytest.raises(ManifestValidationError, match="unsupported chunking.strategy"):
        load_manifest(manifest_path)


def test_rejects_a_non_positive_max_characters(tmp_path):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path,
        [_doc_entry(content_path="policy.md", content_sha256=digest)],
        chunking_overrides={"max_characters": 0},
    )

    with pytest.raises(ManifestValidationError, match="max_characters must be positive"):
        load_manifest(manifest_path)


def test_rejects_overlap_characters_not_less_than_max_characters(tmp_path):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path,
        [_doc_entry(content_path="policy.md", content_sha256=digest)],
        chunking_overrides={"max_characters": 100, "overlap_characters": 100},
    )

    with pytest.raises(ManifestValidationError, match="overlap_characters"):
        load_manifest(manifest_path)


def test_rejects_negative_overlap_characters(tmp_path):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path,
        [_doc_entry(content_path="policy.md", content_sha256=digest)],
        chunking_overrides={"overlap_characters": -1},
    )

    with pytest.raises(ManifestValidationError, match="overlap_characters"):
        load_manifest(manifest_path)


def test_rejects_minimum_characters_greater_than_max_characters(tmp_path):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path,
        [_doc_entry(content_path="policy.md", content_sha256=digest)],
        chunking_overrides={"max_characters": 100, "overlap_characters": 10, "minimum_characters": 101},
    )

    with pytest.raises(ManifestValidationError, match="minimum_characters"):
        load_manifest(manifest_path)


def test_rejects_a_manifest_over_the_max_documents_cap(tmp_path):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody")
    manifest_path = _write_manifest(
        tmp_path, [_doc_entry(content_path="policy.md", content_sha256=digest)], max_documents=0
    )

    with pytest.raises(ManifestValidationError, match="over the configured cap"):
        load_ingestable_documents(manifest_path)


# --- manifest validation: administrative exclusion is not an error ------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"synthetic": False},
        {"retrieval_enabled": False},
        {"approval_status": "pending_review"},
    ],
)
def test_excludes_a_non_ingestable_document_without_erroring_the_whole_manifest(tmp_path, overrides):
    digest = _write_content(tmp_path, "policy.md", "# Title\nbody text long enough to not be trivial")
    manifest_path = _write_manifest(
        tmp_path, [_doc_entry(content_path="policy.md", content_sha256=digest, **overrides)]
    )

    result = load_ingestable_documents(manifest_path)

    assert result == []  # excluded, not an error


def test_loads_an_ingestable_document_alongside_an_excluded_one(tmp_path):
    good_digest = _write_content(tmp_path, "good.md", "# Title\nbody text long enough to not be trivial")
    bad_digest = _write_content(tmp_path, "bad.md", "# Title\nbody text long enough to not be trivial")
    manifest_path = _write_manifest(
        tmp_path,
        [
            _doc_entry(source_id="GOOD", content_path="good.md", content_sha256=good_digest),
            _doc_entry(source_id="BAD", content_path="bad.md", content_sha256=bad_digest, retrieval_enabled=False),
        ],
    )

    result = load_ingestable_documents(manifest_path)

    assert len(result) == 1
    assert result[0][0].source_id == "GOOD"


# --- manifest validation: the real corpus --------------------------------------


def test_the_real_ragdocs_manifest_and_corpus_pass_structural_validation():
    result = load_ingestable_documents(REAL_MANIFEST_PATH)

    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert len(result) == len(manifest.documents)  # every real document is currently ingestable
    for doc, text in result:
        assert doc.is_ingestable
        assert text  # real Markdown body was actually read


# --- chunking -------------------------------------------------------------


from libs.policy_corpus.contracts import ChunkingConfig  # noqa: E402

_CONFIG = ChunkingConfig(
    strategy="markdown_heading_sections",
    max_characters=1200,
    overlap_characters=120,
    minimum_characters=80,
    preserve_heading_path=True,
)


def test_chunks_by_heading_and_preserves_heading_path():
    markdown = "# Title\nintro text\n\n## Section A\nbody A\n\n### Subsection\nbody sub"
    chunks = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=_CONFIG)

    assert [c.heading_path for c in chunks] == [("Title",), ("Title", "Section A"), ("Title", "Section A", "Subsection")]
    assert chunks[0].text == "intro text"
    assert chunks[1].text == "body A"
    assert chunks[2].text == "body sub"


def test_preamble_before_first_heading_becomes_its_own_chunk():
    markdown = "preamble text\n\n# Title\nbody"
    chunks = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=_CONFIG)

    assert chunks[0].heading_path == ()
    assert chunks[0].section_id == "preamble"
    assert chunks[0].text == "preamble text"


def test_chunk_ids_are_source_id_at_version_hash_section_id():
    markdown = "# Overview\nbody"
    chunks = chunk_markdown(source_id="POL-7", source_version="2.1", markdown_text=markdown, config=_CONFIG)

    assert chunks[0].chunk_id == "POL-7@2.1#overview"


def test_splits_an_oversized_section_into_overlapping_pieces():
    config = ChunkingConfig(
        strategy="markdown_heading_sections", max_characters=100, overlap_characters=20, minimum_characters=10,
        preserve_heading_path=True,
    )
    body = "A" * 250
    markdown = f"# Title\n{body}"
    chunks = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=config)

    assert len(chunks) > 1
    # Reassembling the non-overlapping heads recovers the full original text length class.
    assert all(len(c.text) <= 100 for c in chunks)
    # Consecutive pieces actually overlap.
    assert chunks[0].text[-20:] == chunks[1].text[:20]


def test_a_small_trailing_remainder_never_pushes_a_chunk_past_max_characters():
    # w-9-2-planner P2 review fix (CHUNK-MAX-OVERFLOW): 210 chars with
    # max_characters=100/overlap=0 leaves a naive trailing remainder of only
    # 10 chars. Merging that remainder into its predecessor (the previous
    # behavior) produced a 110-char chunk — silently over the configured
    # cap. The final piece must instead be right-aligned to the body's end,
    # so EVERY piece stays within max_characters, never an average.
    config = ChunkingConfig(
        strategy="markdown_heading_sections", max_characters=100, overlap_characters=0, minimum_characters=50,
        preserve_heading_path=True,
    )
    body = "A" * 210
    markdown = f"# Title\n{body}"
    chunks = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=config)

    assert all(len(c.text) <= 100 for c in chunks)
    assert len(chunks) == 3
    assert [len(c.text) for c in chunks] == [100, 100, 100]
    # Right-alignment means the last piece is the body's final 100 chars —
    # no small orphan tail ever survives.
    assert body[-100:] == chunks[-1].text


def test_same_input_produces_identical_chunks_across_runs():
    markdown = "# Title\nintro\n\n## A\nbody a\n\n## B\nbody b"
    first = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=_CONFIG)
    second = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=_CONFIG)

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.chunk_hash for c in first] == [c.chunk_hash for c in second]


def test_disambiguates_sibling_headings_with_the_same_text():
    markdown = "# Overview\nfirst\n\n# Overview\nsecond"
    chunks = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=_CONFIG)

    assert chunks[0].section_id == "overview"
    assert chunks[1].section_id == "overview-2"
    assert chunks[0].chunk_id != chunks[1].chunk_id


def test_chunk_hash_matches_sha256_of_its_own_text():
    markdown = "# Title\nbody text"
    chunks = chunk_markdown(source_id="POL-1", source_version="1.0", markdown_text=markdown, config=_CONFIG)

    assert chunks[0].chunk_hash == hashlib.sha256(chunks[0].text.encode("utf-8")).hexdigest()


# --- chunking against the real corpus ------------------------------------


def test_chunking_the_real_corpus_produces_stable_nonempty_chunks():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    for doc, text in load_ingestable_documents(REAL_MANIFEST_PATH):
        chunks = chunk_markdown(
            source_id=doc.source_id,
            source_version=doc.source_version,
            markdown_text=text,
            config=manifest.ingestion.chunking,
        )
        assert chunks, f"{doc.citation_id} produced no chunks"
        chunk_ids = [c.chunk_id for c in chunks]
        assert len(chunk_ids) == len(set(chunk_ids)), f"{doc.citation_id} produced duplicate chunk_ids"
        for c in chunks:
            assert c.text.strip()
            assert c.chunk_id.startswith(f"{doc.source_id}@{doc.source_version}#")
