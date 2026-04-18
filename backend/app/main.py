import os
from typing import List, Optional, Dict, Any
from pathlib import Path
from dotenv import load_dotenv

# Load env files explicitly so startup works from either backend/ or backend/app/ cwd.
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(APP_DIR / ".env")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.routers.workout import router as workout_router
from app.services.analyze_service import analyze_user
from app.schemas.predict_schema import PredictRequest

from app.routers.nutrition import router as nutrition_router
from app.routers.chat import router as chat_router
from app.routers.agent import router as agent_router



# -----------------------------
# FastAPI app (ONLY ONE APP)
# -----------------------------
app = FastAPI(
    title="Lifeline AI Backend",
    version="1.0.0",
)
app.include_router(workout_router)
app.include_router(nutrition_router)
app.include_router(chat_router)
app.include_router(agent_router)
# -----------------------------
# CORS (frontend -> backend)
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Gemini Setup
# -----------------------------


# -----------------------------
# Models
# -----------------------------


class PredictResponse(BaseModel):
    bmi: float
    risk_score: int
    risk_level: str
    contributing_factors: List[str]
    note: str


class AnalyzeResponse(BaseModel):
    risk_summary: str
    risk_factors: List[str]
    workout_plan_summary: str
    nutrition_plan_summary: str
    daily_spark: str



# -----------------------------
# Helpers
# -----------------------------
def compute_bmi(height_cm: float, weight_kg: float) -> float:
    h_m = height_cm / 100.0
    return round(weight_kg / (h_m * h_m), 1)


def simple_risk_engine(req: PredictRequest) -> Dict[str, Any]:
    # BMI
    if req.height is not None and req.weight is not None:
        bmi = compute_bmi(req.height, req.weight)
    else:
        bmi = 0.0

    score = 0
    factors = []

    if bmi >= 30:
        score += 30
        factors.append("High BMI (Obesity range)")
    elif bmi >= 25:
        score += 20
        factors.append("BMI in overweight range")
    elif bmi > 0 and bmi < 18.5:
        score += 10
        factors.append("Low BMI (underweight)")

    # Sleep
    if req.sleep < 6:
        score += 10
        factors.append("Low sleep duration")
    elif req.sleep > 9:
        score += 5
        factors.append("Very high sleep duration")

    # Exercise
    if req.exercise < 20:
        score += 10
        factors.append("Low daily exercise")
    elif req.exercise >= 60:
        score -= 5
        factors.append("Good daily exercise")

    # Stress (numeric 1–10)
    if req.stress >= 8:
        score += 10
        factors.append("High stress level")
    elif req.stress >= 5:
        score += 5
        factors.append("Moderate stress level")

    # Medical background (string)
    medical_text = (req.medical or "").lower()

    if "diabetes" in medical_text:
        score += 15
        factors.append("Existing diabetes history")
    if "hypertension" in medical_text or "bp" in medical_text:
        score += 12
        factors.append("Hypertension history")
    if "heart" in medical_text:
        score += 15
        factors.append("Heart disease history")


    # Smoking / Alcohol
    if req.smoking:
        score += 15
        factors.append("Smoking")
    if req.alcohol:
        score += 8
        factors.append("Alcohol usage")

    # clamp score 0..100
    score = max(0, min(100, score))

    if score >= 70:
        level = "High"
    elif score >= 40:
        level = "Medium"
    else:
        level = "Low"

    return {
        "bmi": bmi,
        "risk_score": score,
        "risk_level": level,
        "contributing_factors": factors if factors else ["No major risk flags detected"],
        "note": "This is an early prototype estimate. Not medical advice.",
    }


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {"ok": True, "message": "Lifeline AI backend is running"}

@app.get("/health")
def health():
    return {"status": "UP"}

@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    return simple_risk_engine(req)


@app.post("/analyze")
def analyze(req: PredictRequest):
    return analyze_user(req)
