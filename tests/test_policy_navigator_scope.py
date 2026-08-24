"""Tests for role-derived retrieval scope
(libs/policy_navigator/scope.py) — the application-side authorization
boundary the navigator's tool closes over (vector-rag.md).
"""
from libs.policy_corpus import RetrievalScope
from libs.policy_navigator import scope_for_role


def test_a_known_role_gets_a_nonempty_scope():
    scope = scope_for_role("clinician")

    assert isinstance(scope, RetrievalScope)
    assert "clinician" in scope.audiences
    assert scope.workflows


def test_different_roles_get_different_scopes():
    assert scope_for_role("roi_clerk") != scope_for_role("billing")


def test_the_deprecated_legacy_staff_role_gets_an_empty_scope():
    # Fails closed rather than guessing a broad scope for the deprecated
    # legacy role — see module docstring.
    scope = scope_for_role("staff")

    assert scope.audiences == ()
    assert scope.workflows == ()


def test_an_unrecognized_role_gets_an_empty_scope():
    scope = scope_for_role("some_typo_role")

    assert scope.audiences == ()
    assert scope.workflows == ()
