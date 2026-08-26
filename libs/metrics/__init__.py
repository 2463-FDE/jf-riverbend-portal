"""Minimal, safe golden-signal counter emission — see counters.py for the
full contract. No monitoring platform or new dependency is introduced.
"""
from .counters import record_counter

__all__ = ["record_counter"]
