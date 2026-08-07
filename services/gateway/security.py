"""
Password hashing/verification (PBKDF2-SHA256, django-style string) and Redis
session handling.

PR #23 review round 2 (2026-08-07): sessions now carry a Redis TTL
(settings.session_timeout_seconds), refreshed on each authenticated read, so
an abandoned token expires instead of living forever. The session also carries
the stable `user_id` (users.id) as the authorization principal — username is
kept only as display/audit metadata. There is still no second factor; password
only (MFA remains out of scope for this catch-up).
"""
import base64
import hashlib
import hmac
import os
import uuid

import redis as redis_lib

from config import settings

_ITERATIONS = 260000
_ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(12).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, b64hash = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != _ALGORITHM:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    expected = base64.b64encode(dk).decode()
    return hmac.compare_digest(expected, b64hash)


_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def create_session(user_id: int, username: str, role: str) -> str:
    token = uuid.uuid4().hex
    key = f"session:{token}"
    # user_id is the stable authorization principal; username/role are display
    # + audit metadata. Redis values are strings (decode_responses=True).
    _redis().hset(key, mapping={"user_id": str(user_id), "username": username, "role": role})
    _redis().expire(key, settings.session_timeout_seconds)  # idle TTL, refreshed on read
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    key = f"session:{token}"
    data = _redis().hgetall(key)
    if not data:
        return None
    # Sliding expiration: each authenticated request refreshes the idle TTL, so
    # an active user is never logged out mid-session but an abandoned token
    # lapses after settings.session_timeout_seconds of inactivity.
    _redis().expire(key, settings.session_timeout_seconds)
    return data


def destroy_session(token: str) -> None:
    _redis().delete(f"session:{token}")
