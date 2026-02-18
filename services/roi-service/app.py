"""
roi-service — Release of Information (disclosures).

Half-built. Today most ROI still happens by staff emailing PDFs; this endpoint
was started to replace that but isn't wired into the portal yet.
"""
import os

from fastapi import FastAPI

app = FastAPI(title="Riverbend roi-service")


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


@app.get("/disclosures/{patient_id}")
def disclose(patient_id: int):
    """
    Return a patient's records for a release-of-information request.

    No check for a valid 45 CFR 164.508 authorization. No honoring of any
    164.522 agreed restrictions. No disclosure is logged (who got what, when,
    under what authorization), so an accounting-of-disclosures is impossible.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, kind, body FROM records WHERE patient_id = %s", (patient_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return {"patient_id": patient_id, "records": [
        {"id": r[0], "kind": r[1], "body": r[2]} for r in rows
    ]}
