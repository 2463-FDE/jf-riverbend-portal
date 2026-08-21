"""Safe-Harbor de-identification scrub, applied before any LLM or analytics path.

Week 8 deliverable. Distinct from `libs.safe_logging`, which matches dict KEYS
and deliberately returns a bare string unchanged — it cannot touch clinical
narrative, which is exactly what a model receives.

READ `safe_harbor.py`'s module docstring before claiming anything about what
this achieves. It reduces re-identification risk; it does not by itself make
data de-identified under 45 CFR 164.514(b)(2).
"""
from .safe_harbor import (
    DeidReport,
    IDENTIFIER_CATEGORIES,
    RESIDUAL_RISK_CATEGORIES,
    scrub,
    scrub_structured,
)

__all__ = [
    "scrub",
    "scrub_structured",
    "DeidReport",
    "IDENTIFIER_CATEGORIES",
    "RESIDUAL_RISK_CATEGORIES",
]
