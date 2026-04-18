import asyncio
import os
from typing import List, Optional, Dict, Any
from pathlib import Path
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load env files explicitly so startup works from either backend/ or backend/app/ cwd.
APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(APP_DIR / ".env")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.routers.workout import router as workout_router
from app.services.analyze_service import analyze_user
from app.schemas.predict_schema import PredictRequest

from app.routers.nutrition import router as nutrition_router
from app.routers.chat import router as chat_router
from app.routers.agent import router as agent_router
from app.services.proactive_loop_service import (
    APP_TZ,
    run_autonomous_proactive_loop,
    run_proactive_event_retention_cleanup,
    run_proactive_slot,
)


def _safe_run_slot(slot: str) -> None:
    try:
        run_proactive_slot(slot)
    except Exception as exc:
        print(f"[proactive-loop] slot={slot} failed: {exc}")


def _safe_run_proactive_cleanup(retention_days: int, max_events_per_user: int) -> None:
    try:
        result = run_proactive_event_retention_cleanup(
            retention_days=retention_days,
            max_events_per_user=max_events_per_user,
            dry_run=False,
        )
        print(
            "[proactive-cleanup]"
            f" users={result.get('users_processed', 0)}"
            f" archived={result.get('events_archived', 0)}"
            f" errors={result.get('errors', 0)}"
        )
    except Exception as exc:
        print(f"[proactive-cleanup] failed: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler = BackgroundScheduler(timezone=APP_TZ)
    proactive_stop_event = asyncio.Event()
    proactive_task = None

    # Autonomous daily loop:
    # morning plan cue, afternoon meal prompt, night calorie-gap cue.
    scheduler.add_job(lambda: _safe_run_slot("morning"), CronTrigger(hour=8, minute=0, timezone=APP_TZ), id="proactive-morning", replace_existing=True)
    scheduler.add_job(lambda: _safe_run_slot("afternoon"), CronTrigger(hour=14, minute=0, timezone=APP_TZ), id="proactive-afternoon", replace_existing=True)
    scheduler.add_job(lambda: _safe_run_slot("night"), CronTrigger(hour=21, minute=0, timezone=APP_TZ), id="proactive-night", replace_existing=True)

    archive_enabled = os.getenv("PROACTIVE_ARCHIVE_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
    archive_retention_days = int(os.getenv("PROACTIVE_RETENTION_DAYS", "45"))
    archive_max_events_per_user = int(os.getenv("PROACTIVE_ARCHIVE_MAX_EVENTS_PER_USER", "250"))
    archive_hour = int(os.getenv("PROACTIVE_ARCHIVE_DAILY_HOUR", "3"))
    archive_minute = int(os.getenv("PROACTIVE_ARCHIVE_DAILY_MINUTE", "30"))
    if archive_enabled:
        scheduler.add_job(
            lambda: _safe_run_proactive_cleanup(archive_retention_days, archive_max_events_per_user),
            CronTrigger(hour=archive_hour, minute=archive_minute, timezone=APP_TZ),
            id="proactive-archive-cleanup",
            replace_existing=True,
        )

    scheduler.start()

    autonomous_enabled = os.getenv("AUTONOMOUS_PROACTIVE_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
    interval_hours = float(os.getenv("PROACTIVE_INTERVAL_HOURS", "6"))
    if autonomous_enabled:
        proactive_task = asyncio.create_task(
            run_autonomous_proactive_loop(
                proactive_stop_event,
                interval_hours=interval_hours,
            )
        )

    try:
        yield
    finally:
        proactive_stop_event.set()
        if proactive_task:
            await proactive_task
        scheduler.shutdown(wait=False)


# -----------------------------
# FastAPI app (ONLY ONE APP)
# -----------------------------
app = FastAPI(
    title="Lifeline AI Backend",
    version="1.0.0",
    lifespan=lifespan,
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
