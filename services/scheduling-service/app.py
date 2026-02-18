"""
scheduling-service — appointment slots (FHIR Appointment / Slot shaped).
"""
from fastapi import FastAPI
from pydantic import BaseModel

from book import book

app = FastAPI(title="Riverbend scheduling-service")


class BookingRequest(BaseModel):
    patient_id: int
    slot_id: int


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/appointments", status_code=201)
def create_appointment(req: BookingRequest):
    appointment_id = book(req.patient_id, req.slot_id)
    if appointment_id is None:
        return {"status": "slot_taken"}
    return {"appointment_id": appointment_id, "status": "confirmed"}
