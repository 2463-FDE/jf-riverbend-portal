"""Pure-Python cosine similarity. No new dependency added — the eval corpus
and gold set are demonstration-sized (a handful of records/queries drawn
from db/seed's checked-in fixtures); this is not intended to scale past
this harness.
"""
import math
from typing import List


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
