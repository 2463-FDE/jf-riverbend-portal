"""Deterministic Markdown heading-based chunking (w-9-2-planner P2).

Character-based, not word/token-based — the manifest's own chunking config
(`max_characters`/`overlap_characters`/`minimum_characters`) is expressed in
characters, so splitting on characters is the literal, not approximated,
implementation of that contract. Same input always produces the same
`section_id`s and `chunk_id`s — no randomness, no dict-ordering ambiguity —
which is what lets reingestion compare a new run's chunk_hash against a
stored one and skip unchanged chunks (vector-rag.md's idempotent-reingestion
requirement; the actual skip-on-match comparison is a later slice's job once
there is somewhere to compare against).

Scope note: a tiny trailing FRAGMENT produced by sub-splitting an
over-length section is merged back into its predecessor rather than kept as
an orphan (`minimum_characters` applies there). A short but genuine section
— a heading with little content of its own — is still emitted as its own
chunk; merging it across a heading boundary would blur `heading_path` and
citation identity for no clear benefit at this corpus's scale.
"""
import hashlib
import re
from typing import List, Tuple

from .contracts import ChunkingConfig, PolicyChunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _split_into_sections(markdown_text: str) -> List[Tuple[Tuple[str, ...], str]]:
    """(heading_path, body) pairs, in document order. Content before the
    first heading (if any) is a preamble section with an empty heading_path.
    A heading with no body of its own before the next heading contributes no
    section (nothing to chunk), but still anchors its descendants'
    heading_path."""
    stack: List[Tuple[int, str]] = []  # (level, heading_text)
    sections: List[Tuple[Tuple[str, ...], str]] = []
    body_lines: List[str] = []

    def flush():
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((tuple(h for _, h in stack), body))

    for line in markdown_text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            body_lines.clear()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2)))
        else:
            body_lines.append(line)
    flush()
    return sections


def _split_body(body: str, config: ChunkingConfig) -> List[str]:
    if len(body) <= config.max_characters:
        return [body]

    step = max(config.max_characters - config.overlap_characters, 1)
    pieces = []
    start = 0
    n = len(body)
    while start < n:
        end = min(start + config.max_characters, n)
        pieces.append(body[start:end])
        if end >= n:
            break
        start += step

    if len(pieces) > 1 and len(pieces[-1]) < config.minimum_characters:
        pieces[-2] = pieces[-2] + pieces[-1]
        pieces.pop()
    return pieces


def chunk_markdown(
    *, source_id: str, source_version: str, markdown_text: str, config: ChunkingConfig
) -> List[PolicyChunk]:
    chunks: List[PolicyChunk] = []
    slug_counts: dict = {}

    for heading_path, body in _split_into_sections(markdown_text):
        pieces = _split_body(body, config)
        base_slug = _slugify(heading_path[-1]) if heading_path else "preamble"
        multi = len(pieces) > 1
        for piece in pieces:
            slug_counts[base_slug] = slug_counts.get(base_slug, 0) + 1
            occurrence = slug_counts[base_slug]
            section_id = f"{base_slug}-{occurrence}" if (multi or occurrence > 1) else base_slug
            chunks.append(
                PolicyChunk(
                    chunk_id=f"{source_id}@{source_version}#{section_id}",
                    source_id=source_id,
                    source_version=source_version,
                    section_id=section_id,
                    heading_path=heading_path,
                    text=piece,
                    chunk_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                )
            )
    return chunks
