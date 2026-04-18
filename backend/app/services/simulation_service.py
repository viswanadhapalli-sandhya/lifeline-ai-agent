import re
from typing import Any, Dict, List

from firebase_admin import firestore

from app.core.firebase_client import db


def _fetch_progress_summary(uid: str) -> Dict[str, Any]:
    snap = (
        db.collection("users")
        .document(uid)
        .collection("progressStats")
        .document("summary")
        .get()
    )
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def _fetch_latest_plan(uid: str, collection_name: str) -> Dict[str, Any]:
    docs = (
        db.collection("users")
        .document(uid)
        .collection(collection_name)
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )
    if not docs:
        return {}
    payload = docs[0].to_dict() or {}
    return {"id": docs[0].id, **payload}


def _extract_skip_days(scenario: str) -> int:
    text = str(scenario or "").lower()
    match = re.search(r"(\d{1,2})\s*(?:day|days)", text)
    if match:
        return max(1, min(14, int(match.group(1))))

    # Intent like "skip workouts" without duration.
    if "skip" in text and ("workout" in text or "workouts" in text):
        return 3

    return 2


def simulate_outcome(uid: str, scenario: str) -> Dict[str, Any]:
    stats = _fetch_progress_summary(uid)
    workout_plan = _fetch_latest_plan(uid, "workoutPlans")
    nutrition_plan = _fetch_latest_plan(uid, "nutritionPlans")

    skip_days = _extract_skip_days(scenario)
    workout_days = int(stats.get("total_workout_days", 0) or 0)
    workout_minutes = int(stats.get("total_workout_minutes", 0) or 0)
    logged_days = int(stats.get("total_daily_logs", 0) or 0)

    active_streak = 0
    recent = stats.get("recent_workout_history")
    if isinstance(recent, list):
        active_streak = len([x for x in recent if isinstance(x, dict)])
        active_streak = max(0, min(14, active_streak))

    base_delay = max(1, round(skip_days * 1.5))
    consistency_penalty = 1 if workout_days >= 10 else 0
    total_delay = base_delay + consistency_penalty

    streak_loss = bool(active_streak > 0 and skip_days >= 1)

    recovery_plan: List[str] = [
        f"Restart with 60-70% intensity for {min(3, skip_days)} day(s)",
        "Add one 20-minute light session to rebuild consistency",
        "Keep protein and hydration high during recovery week",
    ]

    if workout_minutes < 120:
        recovery_plan.append("Use short daily workouts (15-25 min) to avoid drop-off")
    if logged_days == 0:
        recovery_plan.append("Log meals/workouts daily for the next 5 days to restore momentum")

    plan_meta = {
        "workout_plan_available": isinstance(workout_plan.get("plan"), list) and len(workout_plan.get("plan")) > 0,
        "nutrition_plan_available": isinstance(nutrition_plan.get("plan"), list) and len(nutrition_plan.get("plan")) > 0,
    }

    return {
        "impact": f"delay by {total_delay} days",
        "streak_loss": streak_loss,
        "recovery_plan": recovery_plan,
        "scenario": str(scenario or "").strip(),
        "inputs": {
            "current_stats": {
                "total_workout_days": workout_days,
                "total_workout_minutes": workout_minutes,
                "total_daily_logs": logged_days,
                "active_streak_estimate": active_streak,
            },
            "plan": plan_meta,
            "skip_days_assumed": skip_days,
        },
    }
