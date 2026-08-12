"""
Password hashing/verification (PBKDF2-SHA256, django-style string) and Redis
session handling.

PR #23 review round 2 (2026-08-07): sessions now carry a Redis TTL
(settings.session_timeout_seconds), refreshed on each authenticated read, so
an abandoned token expires instead of living forever. The session also carries
the stable `user_id` (users.id) as the authorization principal — username is
kept only as display/audit metadata.

Production-readiness Stage 1: the idle TTL alone caps only abandonment — an
actively-used session refreshed every read never lapsed. create_session now
also stamps `created_at`; get_session enforces settings.
absolute_session_timeout_seconds against it before refreshing the idle TTL,
so total session lifetime is bounded regardless of activity. A session
missing `created_at` (issued before this fix, same as the pre-user_id
sessions PR #23 round 3 already invalidates below) is treated as invalid,
not grandfathered in.
"""
import base64
import hashlib
import hmac
import os
import time
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


def _now() -> float:
    return time.time()


def create_session(user_id: int, username: str, role: str) -> str:
    token = uuid.uuid4().hex
    key = f"session:{token}"
    # user_id is the stable authorization principal; username/role are display
    # + audit metadata. created_at anchors the absolute-lifetime cap enforced
    # in get_session below. Redis values are strings (decode_responses=True).
    _redis().hset(
        key,
        mapping={
            "user_id": str(user_id),
            "username": username,
            "role": role,
            "created_at": str(_now()),
        },
    )
    _redis().expire(key, settings.session_timeout_seconds)  # idle TTL, refreshed on read
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    key = f"session:{token}"
    data = _redis().hgetall(key)
    if not data:
        return None
    # PR #23 review round 3 (2026-08-08): a session issued before the user_id
    # principal (origin/main sessions carried only username/role, and never
    # expired) cannot authorize anything — routes would forward an empty
    # X-Actor-Id and the caller would silently get empty rosters / 403s, and
    # intake would create patients with no registrar grant. Treat such a
    # session as invalid: delete it and return None so require_session issues a
    # clean 401 and the user simply logs in again (minting a user_id session).
    # Validate BEFORE refreshing, so a malformed session's life is never
    # extended (the review's second point).
    if not data.get("user_id"):
        _redis().delete(key)
        return None
    # Production-readiness Stage 1: a session issued before this fix has no
    # created_at and cannot be aged — same reasoning as the user_id check
    # above, treat it as invalid rather than grandfathering it into an
    # unbounded lifetime.
    created_at = data.get("created_at")
    if not created_at:
        _redis().delete(key)
        return None
    # Absolute lifetime cap: unlike the idle TTL below, this is never
    # refreshed, so a session dies at settings.absolute_session_timeout_seconds
    # after creation no matter how continuously it's used.
    if _now() - float(created_at) > settings.absolute_session_timeout_seconds:
        _redis().delete(key)
        return None
    # Sliding expiration: each authenticated request refreshes the idle TTL, so
    # an active user is never logged out mid-session but an abandoned token
    # lapses after settings.session_timeout_seconds of inactivity.
    _redis().expire(key, settings.session_timeout_seconds)
    return data


def destroy_session(token: str) -> None:
    _redis().delete(f"session:{token}")
