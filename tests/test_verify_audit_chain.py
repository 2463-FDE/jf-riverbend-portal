"""Unit tests for db/migrations/scripts/verify_audit_chain.py's pure chain
logic — no database needed. The real trigger's chain-hash formula is
exercised end-to-end against a real Postgres in
tests/integration/test_audit_logs_append_only.py; this file proves the
verifier's own detection logic is correct in isolation, including cases
that cannot be constructed against the real append-only table at all.
"""
from conftest import load_module

verify = load_module("db/migrations/scripts/verify_audit_chain.py", "verify_audit_chain")


def _chain(rows):
    """rows: list of (actor, message, created_at_canonical). Builds a
    genuinely valid chain, keyed by chain_position (1-indexed, NOT a row
    id), using the module's own hash function — mirrors exactly what the
    real BEFORE INSERT trigger computes."""
    out = []
    prev_hash = None
    for position, (actor, message, created_at_canonical) in enumerate(rows, start=1):
        chain_hash = verify._row_hash(prev_hash, position, actor, message, created_at_canonical)
        out.append((position, actor, message, created_at_canonical, prev_hash, chain_hash))
        prev_hash = chain_hash
    return out


def test_a_genuinely_valid_chain_verifies():
    rows = _chain([
        ("drkim", "get_patient outcome=allowed patient_id=1738", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "list_patients returned 3 patient(s): [1042, 1737, 1738]", "2026-08-26T00:00:01.000000Z"),
        ("drnguyen", "get_patient_records outcome=allowed patient_id=1739", "2026-08-26T00:00:02.000000Z"),
    ])

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is True
    assert break_position is None
    assert reason is None


def test_an_empty_chain_verifies_trivially():
    ok, break_position, reason = verify.verify_chain([])

    assert ok is True
    assert break_position is None


def test_a_single_row_genesis_chain_verifies():
    rows = _chain([("tester", "one event", "2026-08-26T00:00:00.000000Z")])

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is True


def test_a_tampered_message_breaks_the_chain_at_that_row():
    # The whole point of the chain: a row whose content changed after the
    # fact no longer matches the hash computed over its original content.
    rows = _chain([
        ("drkim", "get_patient outcome=allowed patient_id=1738", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "list_patients returned 3 patient(s)", "2026-08-26T00:00:01.000000Z"),
    ])
    position, actor, _message, created_at, prev_hash, chain_hash = rows[1]
    rows[1] = (position, actor, "list_patients returned 0 patient(s)", created_at, prev_hash, chain_hash)

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is False
    assert break_position == 2
    assert "own content" in reason


def test_a_spliced_prev_hash_is_detected_even_if_the_rows_own_hash_is_internally_consistent():
    # A more subtle tamper: replace a row AND recompute its own chain_hash
    # from a fabricated prev_hash, so the row is internally self-consistent
    # -- only checking prev_chain_hash against the ACTUAL preceding row's
    # real chain_hash catches this. This is exactly why verify_chain checks
    # both, not just "does this row's stored hash match its own recompute."
    rows = _chain([
        ("drkim", "get_patient outcome=allowed patient_id=1738", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "list_patients returned 3 patient(s)", "2026-08-26T00:00:01.000000Z"),
    ])
    fabricated_prev = "0" * 64
    position, actor, message, created_at, _real_prev, _real_chain = rows[1]
    fabricated_chain = verify._row_hash(fabricated_prev, position, actor, message, created_at)
    rows[1] = (position, actor, message, created_at, fabricated_prev, fabricated_chain)

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is False
    assert break_position == 2
    assert "preceding row" in reason


def test_a_missing_row_leaves_a_detectable_gap():
    # Deleting a row outright is already blocked by migration 026's trigger,
    # but the verifier's OWN job is to prove that independently -- if a row
    # were ever missing (e.g. a future bug, a restore from an incomplete
    # backup), the next surviving row's prev_chain_hash would no longer
    # match anything actually present. This is an INTERNAL gap (a middle
    # row missing, positions 1 and 3 present without 2) -- a TAIL gap
    # (the chain simply stopping early) is the opposite, undetectable case;
    # see test_a_tail_truncation_is_not_detectable below.
    rows = _chain([
        ("drkim", "event one", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "event two", "2026-08-26T00:00:01.000000Z"),
        ("drnguyen", "event three", "2026-08-26T00:00:02.000000Z"),
    ])
    del rows[1]  # position 2 vanishes; position 3 remains with its original prev_chain_hash

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is False
    assert break_position == 3
    assert "missing from the chain" in reason


def test_a_relinked_and_rehashed_gap_is_still_detected():
    # Round-1 review (AUD-B01): a plain deletion is already caught above
    # because the surviving row's OWN stored prev_chain_hash/chain_hash
    # are left untouched and so stop matching. A more sophisticated tamper
    # also rewrites the surviving row's prev_chain_hash and chain_hash to
    # relink directly onto the new actual predecessor, using that row's
    # own unchanged chain_position (3, not renumbered to 2) — every
    # hash-linkage check then passes, because the attacker used the same
    # formula the trigger does. Only an explicit chain_position density
    # check (1, 2, 3, ... no gaps) catches this.
    rows = _chain([
        ("drkim", "event one", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "event two", "2026-08-26T00:00:01.000000Z"),
        ("drnguyen", "event three", "2026-08-26T00:00:02.000000Z"),
    ])
    del rows[1]  # position 2 vanishes, leaving positions 1 and 3

    position, actor, message, created_at, _stale_prev, _stale_hash = rows[1]
    real_prev_hash = rows[0][5]  # position 1's actual chain_hash
    relinked_hash = verify._row_hash(real_prev_hash, position, actor, message, created_at)
    rows[1] = (position, actor, message, created_at, real_prev_hash, relinked_hash)

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is False
    assert break_position == 3
    assert "missing from the chain" in reason


def test_a_chain_not_starting_at_position_one_is_rejected():
    # The density invariant applies from the very first row, not just
    # between consecutive rows -- a chain missing its genesis entirely
    # (e.g. position 1 deleted, leaving 2, 3, ...) must fail too.
    rows = _chain([
        ("drkim", "event one", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "event two", "2026-08-26T00:00:01.000000Z"),
    ])
    del rows[0]  # position 1 vanishes; position 2 remains, prev_hash None unchanged

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is False
    assert break_position == 2
    assert "missing from the chain" in reason


def test_a_duplicate_chain_position_is_rejected():
    # 027's UNIQUE constraint on chain_position should make this
    # unconstructible against the real table, but the verifier's own
    # logic must not silently accept it if that constraint were ever
    # bypassed (e.g. a direct, trigger-disabled write) -- density means
    # strictly increasing by exactly 1, not merely non-decreasing.
    # `rows` must arrive pre-sorted by chain_position (the real caller's
    # `ORDER BY chain_position` does this) -- two rows tied at position 1
    # sort adjacent, ahead of position 2, regardless of which was forged.
    genesis = _chain([("drkim", "event one", "2026-08-26T00:00:00.000000Z")])[0]
    forged_duplicate = (1, "attacker", "forged event", "2026-08-26T00:00:02.000000Z", None, "0" * 64)
    real_position_two = _chain([
        ("drkim", "event one", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "event two", "2026-08-26T00:00:01.000000Z"),
    ])[1]
    rows = [genesis, forged_duplicate, real_position_two]  # positions: 1, 1, 2

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is False
    assert break_position == 1  # the forged duplicate: expected_position was already 2 by then
    assert "missing from the chain" in reason


def test_verify_chain_failure_reasons_never_contain_row_content():
    # The reason string is the only thing main() ever prints for a broken
    # chain (besides the bare chain_position integer) -- it must stay pure
    # metadata, never actor/message content, regardless of which check
    # trips. Exercises one failure from each of the three return points in
    # verify_chain.
    secret = "PHI-LOOKING-CONTENT-MUST-NEVER-APPEAR-IN-A-REASON-STRING"

    gap_rows = _chain([("drkim", secret, "2026-08-26T00:00:00.000000Z")])
    gap_rows[0] = (2, "drkim", secret, "2026-08-26T00:00:00.000000Z", None, gap_rows[0][5])
    _, _, gap_reason = verify.verify_chain(gap_rows)

    splice_rows = _chain([
        ("drkim", secret, "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", secret, "2026-08-26T00:00:01.000000Z"),
    ])
    splice_rows[1] = (2, "frontdesk", secret, "2026-08-26T00:00:01.000000Z", "0" * 64, splice_rows[1][5])
    _, _, splice_reason = verify.verify_chain(splice_rows)

    tamper_rows = _chain([("drkim", secret, "2026-08-26T00:00:00.000000Z")])
    tamper_rows[0] = (1, "drkim", "different content now", "2026-08-26T00:00:00.000000Z", None, tamper_rows[0][5])
    _, _, tamper_reason = verify.verify_chain(tamper_rows)

    for reason in (gap_reason, splice_reason, tamper_reason):
        assert secret not in reason


def test_a_tail_truncation_is_not_detectable():
    # Documents the chain's known, stated limitation (see migration 027 and
    # verify_audit_chain.py's module docstring): removing the LAST rows and
    # stopping there leaves nothing after the cut to reveal a break, so the
    # remaining prefix verifies as fully intact. This is not a bug in
    # verify_chain -- it is the reason the docs say "detects content changes
    # and internal gaps," not "detects every possible deletion."
    full = _chain([
        ("drkim", "event one", "2026-08-26T00:00:00.000000Z"),
        ("frontdesk", "event two", "2026-08-26T00:00:01.000000Z"),
        ("drnguyen", "event three", "2026-08-26T00:00:02.000000Z"),
    ])
    truncated = full[:2]  # position 3 (the tail) is gone; 1 and 2 remain untouched

    ok, break_position, reason = verify.verify_chain(truncated)

    assert ok is True
    assert break_position is None


def test_encode_field_gives_null_a_distinct_marker_from_empty_string():
    assert verify.encode_field(None) == "N"
    assert verify.encode_field("") == "0:"
    assert verify.encode_field(None) != verify.encode_field("")


def test_encode_field_is_self_delimiting_against_delimiter_characters_in_content():
    # A naive "|".join() encoding would let a message containing "|" shift a
    # field boundary. The length-prefix form can't be fooled by that: the
    # decoded length always says exactly how many bytes belong to this field.
    tricky = "5:hi|3:bye|N:also-tricky"
    encoded = verify.encode_field(tricky)
    assert encoded == f"{len(tricky.encode('utf-8'))}:{tricky}"
    # And two different fields whose payloads happen to share a prefix must
    # not collide when concatenated -- the point of length-prefixing at all.
    assert verify.encode_field("ab") + verify.encode_field("cd") != verify.encode_field("abcd")


def test_row_hash_never_receives_or_needs_patient_content():
    # The formula's own inputs are exactly the metadata every current writer
    # already logs -- this is a documentation-as-test check that the
    # function's signature has not grown a content field.
    import inspect

    params = list(inspect.signature(verify._row_hash).parameters)
    assert params == ["prev_hash", "chain_position", "actor", "message", "created_at_canonical"]
