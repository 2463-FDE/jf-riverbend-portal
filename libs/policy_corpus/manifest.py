"""Manifest loading and the ingestion contract's pre-content validation
(w-9-2-planner P2, docs/RagDocs/manifest.json).

Two distinct failure modes, deliberately not conflated:

  * A STRUCTURAL manifest defect (bad path, hash mismatch, duplicate
    identity, oversize file, unsupported extension, missing file, over the
    document-count cap) is a `ManifestValidationError` — the whole load
    fails. A corpus this broken cannot be trusted to safely ingest anything
    from, security-relevant checks (path traversal, symlink escape) most of
    all.
  * A document that is well-formed but administratively excluded (not
    `synthetic`, not `retrieval_enabled`, or not yet `approval_status:
    approved_training`) is simply left out of `load_ingestable_documents`'s
    result — not an error. A manifest may legitimately carry a disabled or
    not-yet-approved document without that breaking ingestion of every
    other entry.

All of this runs BEFORE any document's Markdown body is read into a chunker
— see `load_ingestable_documents`.
"""
import hashlib
import json
import os
from typing import List, Tuple

from .contracts import ChunkingConfig, IngestionConfig, PolicyDocumentMeta, PolicyManifest


class ManifestValidationError(ValueError):
    """A structural defect in the manifest or its referenced content —
    never raised for a document that is merely excluded from ingestion."""


# The only strategy chunking.py actually implements. A manifest declaring
# anything else must fail loudly at load time rather than be silently
# chunked as if it had said "markdown_heading_sections" anyway.
_SUPPORTED_CHUNKING_STRATEGIES = frozenset({"markdown_heading_sections"})


def _validate_chunking_config(config: ChunkingConfig) -> None:
    """w-9-2-planner P2 review fix (CHUNK-CONFIG-UNVALIDATED): the manifest
    declares these as ingestion-contract fields (docs/RagDocs/manifest.json's
    `ingestion.chunking`) alongside paths and hashes, but nothing checked
    them — a malformed value (max_characters=0, an unrecognized strategy)
    loaded without error and silently produced empty or nonsensical chunks
    instead of failing the same way a bad path or hash mismatch does."""
    if config.strategy not in _SUPPORTED_CHUNKING_STRATEGIES:
        raise ManifestValidationError(
            f"unsupported chunking.strategy {config.strategy!r} (supported: {sorted(_SUPPORTED_CHUNKING_STRATEGIES)})"
        )
    if config.max_characters <= 0:
        raise ManifestValidationError(f"chunking.max_characters must be positive, got {config.max_characters}")
    if not (0 <= config.overlap_characters < config.max_characters):
        raise ManifestValidationError(
            f"chunking.overlap_characters ({config.overlap_characters}) must be in "
            f"[0, max_characters={config.max_characters})"
        )
    if config.minimum_characters > config.max_characters:
        raise ManifestValidationError(
            f"chunking.minimum_characters ({config.minimum_characters}) must be <= "
            f"max_characters ({config.max_characters})"
        )


def load_manifest(path: str) -> PolicyManifest:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    ingestion_raw = raw["ingestion"]
    chunking_raw = ingestion_raw["chunking"]
    chunking = ChunkingConfig(
        strategy=chunking_raw["strategy"],
        max_characters=chunking_raw["max_characters"],
        overlap_characters=chunking_raw["overlap_characters"],
        minimum_characters=chunking_raw["minimum_characters"],
        preserve_heading_path=chunking_raw["preserve_heading_path"],
    )
    _validate_chunking_config(chunking)
    ingestion = IngestionConfig(
        content_root=ingestion_raw["content_root"],
        allowed_extensions=tuple(ingestion_raw["allowed_extensions"]),
        encoding=ingestion_raw["encoding"],
        max_document_bytes=ingestion_raw["max_document_bytes"],
        max_documents=ingestion_raw["max_documents"],
        chunking=chunking,
        required_chunk_metadata=tuple(ingestion_raw["required_chunk_metadata"]),
    )

    documents = tuple(
        PolicyDocumentMeta(
            source_id=d["source_id"],
            source_version=d["source_version"],
            effective_date=d["effective_date"],
            title=d["title"],
            owner=d["owner"],
            approval_status=d["approval_status"],
            synthetic=d["synthetic"],
            retrieval_enabled=d["retrieval_enabled"],
            content_path=d["content_path"],
            content_sha256=d["content_sha256"],
            audiences=tuple(d["audiences"]),
            workflows=tuple(d["workflows"]),
            topics=tuple(d["topics"]),
            allowed_uses=tuple(d["allowed_uses"]),
            prohibited_uses=tuple(d["prohibited_uses"]),
            relationships=tuple(d.get("relationships", [])),
        )
        for d in raw["documents"]
    )

    return PolicyManifest(
        schema_version=raw["schema_version"],
        corpus_id=raw["corpus_id"],
        notice=raw["notice"],
        ingestion=ingestion,
        documents=documents,
    )


def _validate_structure(manifest: PolicyManifest, manifest_dir: str) -> None:
    if len(manifest.documents) > manifest.ingestion.max_documents:
        raise ManifestValidationError(
            f"manifest declares {len(manifest.documents)} documents, "
            f"over the configured cap of {manifest.ingestion.max_documents}"
        )

    seen_identities = set()
    content_root = os.path.realpath(os.path.join(manifest_dir, manifest.ingestion.content_root))

    for doc in manifest.documents:
        identity = (doc.source_id, doc.source_version)
        if identity in seen_identities:
            raise ManifestValidationError(f"duplicate source_id/source_version: {identity}")
        seen_identities.add(identity)

        if os.path.isabs(doc.content_path):
            raise ManifestValidationError(f"{doc.citation_id}: content_path must be relative, got {doc.content_path!r}")

        _, ext = os.path.splitext(doc.content_path)
        if ext not in manifest.ingestion.allowed_extensions:
            raise ManifestValidationError(f"{doc.citation_id}: unsupported extension {ext!r}")

        # Resolves both `..` traversal and a symlink that escapes
        # content_root to the same check: the real, final path must still
        # live under content_root once every symlink is followed.
        full_path = os.path.realpath(os.path.join(content_root, doc.content_path))
        if os.path.commonpath([content_root, full_path]) != content_root:
            raise ManifestValidationError(f"{doc.citation_id}: content_path escapes content_root: {doc.content_path!r}")

        if not os.path.isfile(full_path):
            raise ManifestValidationError(f"{doc.citation_id}: content file missing: {doc.content_path!r}")

        size = os.path.getsize(full_path)
        if size > manifest.ingestion.max_document_bytes:
            raise ManifestValidationError(
                f"{doc.citation_id}: {size} bytes exceeds max_document_bytes={manifest.ingestion.max_document_bytes}"
            )

        with open(full_path, "rb") as fh:
            raw_bytes = fh.read()
        try:
            raw_bytes.decode(manifest.ingestion.encoding)
        except UnicodeDecodeError as exc:
            raise ManifestValidationError(f"{doc.citation_id}: not valid {manifest.ingestion.encoding}") from exc

        actual_hash = hashlib.sha256(raw_bytes).hexdigest()
        if actual_hash != doc.content_sha256:
            raise ManifestValidationError(
                f"{doc.citation_id}: content_sha256 mismatch (manifest={doc.content_sha256}, actual={actual_hash})"
            )


def load_ingestable_documents(manifest_path: str) -> List[Tuple[PolicyDocumentMeta, str]]:
    """Loads and structurally validates the manifest (raises
    `ManifestValidationError` on any structural defect — see module
    docstring), then returns `(document_meta, markdown_text)` for every
    document that passes `PolicyDocumentMeta.is_ingestable`. Excluded
    (not-yet-approved / disabled) documents are silently omitted, not an
    error — every returned document has ALREADY had its path/hash/size/
    encoding validated, so callers never need to re-check those."""
    manifest = load_manifest(manifest_path)
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    _validate_structure(manifest, manifest_dir)

    content_root = os.path.realpath(os.path.join(manifest_dir, manifest.ingestion.content_root))
    result = []
    for doc in manifest.documents:
        if not doc.is_ingestable:
            continue
        full_path = os.path.join(content_root, doc.content_path)
        with open(full_path, encoding=manifest.ingestion.encoding) as fh:
            text = fh.read()
        result.append((doc, text))
    return result
