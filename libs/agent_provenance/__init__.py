"""Provenance and tracing spine for the agentic patient-summary path.

Persists and traces METADATA ABOUT a generation, never the generation. See
`recorder.py` for the forbidden-class guard that enforces it.
"""
from .recorder import (
    FORBIDDEN_KEYS,
    STAGES,
    ForbiddenPayload,
    ProvenanceLabel,
    Stage,
    StageEvent,
    TraceRecorder,
    assert_safe,
)

__all__ = [
    "TraceRecorder",
    "StageEvent",
    "Stage",
    "STAGES",
    "ProvenanceLabel",
    "ForbiddenPayload",
    "FORBIDDEN_KEYS",
    "assert_safe",
]
