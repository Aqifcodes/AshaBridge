from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
from database.db import get_db
from models.referral import Referral
from models.patient import PatientJourneyLog

router = APIRouter()

class StatusUpdate(BaseModel):
    status: str
    notes: Optional[str] = ""
    diagnosis: Optional[str] = None
    medication_prescribed: Optional[str] = None

@router.get("/")
def list_referrals(
    priority: Optional[str] = None, 
    status: Optional[str] = None, 
    hospital_id: Optional[str] = None, 
    asha_worker_id: Optional[str] = None, 
    db: Session = Depends(get_db)
):
    from models.hospital import Hospital # Ensure Hospital model is imported
    # Note: If you have a separate Specialist model, import it here too. 
    # If specialists are just strings in your DB, you might not need to import a model.
    # Assuming for this fix that specialists are handled by the referral_service or are related objects.

    q = db.query(Referral)
    if priority:
        q = q.filter(Referral.triage_priority == priority.upper())
    if status:
        q = q.filter(Referral.status == status.upper())
    if hospital_id:
        q = q.filter(Referral.destination_hospital_id == hospital_id)
    if asha_worker_id:
        q = q.filter(Referral.asha_worker_id == asha_worker_id)
        
    referrals = q.order_by(Referral.created_at.desc()).all()

    results = []
    for r in referrals:
        patient = getattr(r, "patient", None)
        
        # Get patient details safely
        patient_name = getattr(patient, "full_name", getattr(patient, "name", r.patient_id)) if patient else r.patient_id
        village = getattr(patient, "village", "-") if patient else "-"
        
        # --- NEW: Fetch actual Hospital and Specialist details ---
        hospital = db.query(Hospital).filter(Hospital.id == r.destination_hospital_id).first()
        hospital_name = hospital.name if hospital else r.destination_hospital_id
        available_beds = hospital.available_beds if hospital else 0
        emergency_number = hospital.emergency_number if hospital else "+91 108"

        # Note: If you have a Specialist table, query it similarly here.
        # If 'assigned_specialist_id' is just a string name, use it directly.
        # This assumes your DB might just be storing the string name or "Not Assigned"
        specialist_name = getattr(r, "assigned_specialist_id", "Not Assigned") or "Not Assigned"

        
        results.append({
            "id": r.id,
            "patient_id": patient_name,
            "priority": r.triage_priority,
            "status": r.status,
            "hospital_id": hospital_name, # Send the NAME, not just the ID
            "created_at": r.created_at,
            
            # --- EXACT MATCHES FOR YOUR JAVASCRIPT ---
            "village": village,
            "category": getattr(r, "triage_category", "-") or "-", 
            "specialist": specialist_name, # Send the actual name
            "emergency_number": emergency_number, # Send the real emergency number
            "available_beds": available_beds, # Send the bed count

            "hospital": { # Provide the nested object the frontend expects
                "name": hospital_name,
                "available_beds": available_beds,
                "emergency_number": emergency_number
            },
            
            "patient": {
                "name": patient_name,
                "village": village
            } if patient else None
        })
        
    return results

@router.get("/{referral_id}")
def get_referral(referral_id: str, db: Session = Depends(get_db)):
    r = db.query(Referral).filter(Referral.id == referral_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Referral not found")
    return r

@router.patch("/{referral_id}/status")
def update_referral_status(referral_id: str, body: StatusUpdate, db: Session = Depends(get_db)):
    r = db.query(Referral).filter(Referral.id == referral_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Referral not found")

    valid = ["PENDING","ACCEPTED","IN_TRANSIT","ADMITTED","DIAGNOSED","DISCHARGED","CANCELLED"]
    if body.status.upper() not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status. Choose from {valid}")

    r.status = body.status.upper()
    if body.status.upper() == "ADMITTED":
        r.admitted_at = datetime.utcnow()
    if body.status.upper() == "DISCHARGED":
        r.discharged_at = datetime.utcnow()
        r.diagnosis = body.diagnosis
        r.medication_prescribed = body.medication_prescribed

    db.add(PatientJourneyLog(
        patient_id=r.patient_id,
        referral_id=r.id,
        status=body.status.upper(),
        notes=body.notes,
    ))
    db.commit()
    return {"message": "Status updated", "referral_id": r.id, "new_status": r.status}

@router.get("/stats/summary")
def referral_stats(db: Session = Depends(get_db)):
    total = db.query(Referral).count()
    critical = db.query(Referral).filter(Referral.triage_priority == "CRITICAL").count()
    high = db.query(Referral).filter(Referral.triage_priority == "HIGH").count()
    pending = db.query(Referral).filter(Referral.status == "PENDING").count()
    admitted = db.query(Referral).filter(Referral.status == "ADMITTED").count()
    return {"total": total, "critical": critical, "high": high, "pending": pending, "admitted": admitted}
