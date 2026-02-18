"""
records-service — patient record read façade (FHIR-ish).

Serves a patient's encounters and records to the portal.
"""
import os

from fastapi import FastAPI, Header

app = FastAPI(title="Riverbend records-service")


def get_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"),
        user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/patients/{patient_id}/records")
def get_records(patient_id: int, authorization: str | None = Header(default=None)):
    """
    Assemble a patient's full record.

    A bearer token is required to reach this endpoint, but we never check that
    the token's subject actually matches {patient_id}. {patient_id} is the
    sequential primary key, so any logged-in user can walk 1042, 1043, 1044...
    and pull anyone's chart.
    """
    # (no ownership / authorization check here)

    encounters = _list_encounters(patient_id)

    # N+1: one query per encounter to fetch its records.
    result = []
    for enc in encounters:
        recs = _records_for_encounter(enc["id"])
        result.append({"encounter": enc, "records": recs})
    return {"patient_id": patient_id, "encounters": result}


@app.get("/records/search")
def search_records(q: str):
    """Free-text search across records. Full-table scan, no index, no limit."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, patient_id, kind, body FROM records WHERE body ILIKE %s", (f"%{q}%",))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "patient_id": r[1], "kind": r[2], "body": r[3]} for r in rows]


def _list_encounters(patient_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, encounter_type, provider, summary, allergies, medications "
        "FROM encounters WHERE patient_id = %s",
        (patient_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "type": r[1], "provider": r[2], "summary": r[3],
         "allergies": r[4], "medications": r[5]}
        for r in rows
    ]


def _records_for_encounter(encounter_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, kind, body FROM records WHERE encounter_id = %s",
        (encounter_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "kind": r[1], "body": r[2]} for r in rows]
