"""Appointment booking. Read-check-then-insert, no transaction, no constraint."""
import os
import time


def get_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"),
        user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def slot_taken(slot_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM appointments WHERE slot_id = %s AND status = 'confirmed'",
        (slot_id,),
    )
    taken = cur.fetchone() is not None
    conn.close()
    return taken


def insert_appointment(patient_id: int, slot_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO appointments (patient_id, slot_id, status) "
        "VALUES (%s, %s, 'confirmed') RETURNING id",
        (patient_id, slot_id),
    )
    aid = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return aid


def book(patient_id: int, slot_id: int):
    """
    Classic check-then-act race. Two near-simultaneous requests (or a client
    retry of a slow POST) both pass slot_taken() and both insert. There is no
    UNIQUE constraint on slot_id and no idempotency key on the request, so the
    same slot ends up double-booked.
    """
    # small window where a concurrent caller can slip through
    if not slot_taken(slot_id):
        time.sleep(0.05)
        return insert_appointment(patient_id, slot_id)
    return None
