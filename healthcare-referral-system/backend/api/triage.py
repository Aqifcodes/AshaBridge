"""
Triage API
───────────
POST /api/triage/evaluate  — Run triage on submitted patient data.
POST /api/triage/refer     — Evaluate + auto-create referral + notify hospital.
"""

import uuid
import json
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from sqlalchemy.orm import Session

from database.db import get_db
from models.patient import Patient, PatientJourneyLog
from models.hospital import Hospital # Added for forced routing
from services.triage_engine import (
    TriageEngine, PatientInput, VitalsInput, triage_engine
)
from services.referral_service import referral_service
from services.notification_service import NotificationService
from datetime import datetime

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response Schemas
# ─────────────────────────────────────────────────────────────────────────────

class VitalsSchema(BaseModel):
    temperature: Optional[float] = None
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse_rate: Optional[int] = None
    spo2: Optional[float] = None
    weight: Optional[float] = None


class TriageRequest(BaseModel):
    symptoms: List[str] = Field(..., min_items=1)
    age: int
    gender: str
    symptom_duration_hours: int = 0
    vitals: Optional[VitalsSchema] = None
    is_pregnant: bool = False
    comorbidities: List[str] = []
    notes: str = ""


class ReferAndNotifyRequest(BaseModel):
    # Patient details
    patient_name: str
    age: int
    gender: str
    village: str
    district: str
    contact: Optional[str] = None
    # Clinical
    symptoms: List[str]
    symptom_duration_hours: int = 0
    vitals: Optional[VitalsSchema] = None
    is_pregnant: bool = False
    comorbidities: List[str] = []
    notes: str = ""
    # Workflow
    asha_worker_id: str
    origin_phc_id: Optional[str] = None
    transport_mode: str = "ambulance"
    target_hospital_id: Optional[str] = None # Added for demo overriding


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/evaluate")
async def evaluate_triage(req: TriageRequest):
    """
    Runs the triage algorithm and returns priority + action.
    Does NOT create a referral — use /refer for the full workflow.
    """
    vitals = None
    if req.vitals:
        vitals = VitalsInput(**req.vitals.dict())

    patient_input = PatientInput(
        symptoms=req.symptoms,
        age=req.age,
        gender=req.gender,
        symptom_duration_hours=req.symptom_duration_hours,
        vitals=vitals,
        is_pregnant=req.is_pregnant,
        comorbidities=req.comorbidities,
        notes=req.notes,
    )

    result = triage_engine.evaluate(patient_input)

    return {
        "score": result.score,
        "priority": result.priority,
        "category": result.category,
        "specialist_needed": result.specialist_needed,
        "action": result.action,
        "color": result.color,
        "needs_icu": result.needs_icu,
        "needs_referral": result.needs_referral,
        "symptom_breakdown": result.symptom_breakdown,
        "vitals_flags": result.vitals_flags,
        "reasoning": result.reasoning,
    }


@router.post("/refer")
async def triage_and_refer(req: ReferAndNotifyRequest, request: Request, db: Session = Depends(get_db)):
    """
    Full workflow:
    1. Register patient in DB
    2. Run triage
    3. Query bed & specialist availability
    4. Create digital referral ticket
    5. Push WebSocket alert to hospital dashboard
    6. Send confirmation to ASHA worker
    """
    # ── Step 1: Register patient ──────────────────────────────────────────────
    patient_id = f"PAT-{str(uuid.uuid4())[:8].upper()}"
    patient = Patient(
        id=patient_id,
        name=req.patient_name,
        age=req.age,
        gender=req.gender,
        village=req.village,
        district=req.district,
        contact=req.contact,
        symptoms=json.dumps(req.symptoms),
        symptom_duration_hours=req.symptom_duration_hours,
        notes=req.notes,
        asha_worker_id=req.asha_worker_id,
        temperature=req.vitals.temperature if req.vitals else None,
        blood_pressure_systolic=req.vitals.bp_systolic if req.vitals else None,
        blood_pressure_diastolic=req.vitals.bp_diastolic if req.vitals else None,
        pulse_rate=req.vitals.pulse_rate if req.vitals else None,
        spo2=req.vitals.spo2 if req.vitals else None,
        weight=req.vitals.weight if req.vitals else None,
    )
    db.add(patient)
    db.add(PatientJourneyLog(
        patient_id=patient_id,
        status="REGISTERED",
        location=req.village,
        notes="Patient registered by ASHA worker",
        logged_by=req.asha_worker_id,
    ))

    # ── Step 2: Triage ────────────────────────────────────────────────────────
    vitals = None
    if req.vitals:
        vitals = VitalsInput(**req.vitals.dict())

    patient_input = PatientInput(
        symptoms=req.symptoms,
        age=req.age,
        gender=req.gender,
        symptom_duration_hours=req.symptom_duration_hours,
        vitals=vitals,
        is_pregnant=req.is_pregnant,
        comorbidities=req.comorbidities,
        notes=req.notes,
    )
    triage_result = triage_engine.evaluate(patient_input)

    # ── Step 3: Find best hospital ────────────────────────────────────────────
    if req.target_hospital_id:
        hospital = db.query(Hospital).filter(Hospital.id == req.target_hospital_id).first()
        specialist = None # Bypass specialist assignment for forced routing
        if not hospital:
            hospital, specialist = referral_service.find_best_hospital(db, triage_result)
    else:
        hospital, specialist = referral_service.find_best_hospital(db, triage_result)

    if not hospital:
        db.commit()
        return {
            "status": "NO_BED_AVAILABLE",
            "patient_id": patient_id,
            "triage": {
                "score": triage_result.score,
                "priority": triage_result.priority,
                "action": triage_result.action,
            },
            "message": "No suitable hospital with available beds found. Please contact district health office.",
        }

    # ── Step 4: Create referral ticket ────────────────────────────────────────
    referral = referral_service.create_referral(
        db=db,
        patient=patient,
        triage_result=triage_result,
        asha_worker_id=req.asha_worker_id,
        hospital=hospital,
        specialist=specialist,
        origin_phc_id=req.origin_phc_id,
        transport_mode=req.transport_mode,
    )

    # ── Step 5: WebSocket notifications ──────────────────────────────────────
    manager = request.app.state.manager
    notification_service = NotificationService(manager)

    hospital_payload = referral_service.build_hospital_notification(
        referral, patient, triage_result, specialist
    )
    asha_payload = referral_service.build_asha_confirmation(referral, hospital)

    await notification_service.push_referral_to_hospital(hospital.id, hospital_payload)
    await notification_service.push_confirmation_to_asha(req.asha_worker_id, asha_payload)

    # Mark hospital notified
    referral.hospital_notified = True
    db.commit()

    return {
        "status": "REFERRAL_CREATED",
        "ticket_id": referral.id,
        "patient_id": patient_id,
        "triage": {
            "score": triage_result.score,
            "priority": triage_result.priority,
            "category": triage_result.category,
            "action": triage_result.action,
            "color": triage_result.color,
            "needs_icu": triage_result.needs_icu,
        },
        "hospital": {
            "id": hospital.id,
            "name": hospital.name,
            "district": hospital.district,
            "emergency_number": hospital.emergency_number,
            "available_beds": hospital.available_beds,
        },
        "specialist": {
            "name": specialist.name,
            "specialty": specialist.specialty,
            "contact": specialist.contact,
        } if specialist else None,
        "transport_mode": req.transport_mode,
    }