from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from firebase_admin import firestore

from app.core.firebase_client import db
from app.services.agent_service import get_proactive_recommendations
from app.services.nutrition_service import generate_nutrition_plan
from app.services.workout_service import generate_workout_plan

try:
    APP_TZ = ZoneInfo("Asia/Kolkata")
except Exception:
    APP_TZ = timezone.utc

WORKOUT_FOCUS_MAP = {
    1: "Upper Body",
    2: "Lower Body",
    3: "Core + Mobility",
    4: "Push Strength",
    5: "Leg Day",
    6: "Conditioning",
    7: "Recovery + Mobility",
}

PROACTIVE_INTERVAL_HOURS = float(os.getenv("PROACTIVE_INTERVAL_HOURS", "6"))
PROACTIVE_DEDUP_HOURS = int(os.getenv("PROACTIVE_DEDUP_HOURS", "12"))
PROACTIVE_MAX_USER_CONCURRENCY = int(os.getenv("PROACTIVE_MAX_USER_CONCURRENCY", "10"))
PROACTIVE_RETENTION_DAYS = int(os.getenv("PROACTIVE_RETENTION_DAYS", "45"))
PROACTIVE_ARCHIVE_MAX_EVENTS_PER_USER = int(os.getenv("PROACTIVE_ARCHIVE_MAX_EVENTS_PER_USER", "250"))

WEEKLY_EVOLUTION_HOUR = int(os.getenv("WEEKLY_PLAN_EVOLUTION_HOUR", "6"))
WEEKLY_EVOLUTION_MINUTE = int(os.getenv("WEEKLY_PLAN_EVOLUTION_MINUTE", "15"))
WEEKLY_EVOLUTION_DAY_OF_WEEK = os.getenv("WEEKLY_PLAN_EVOLUTION_DAY", "mon")


def _now_local() -> datetime:
    return datetime.now(APP_TZ)


def _today_key_local() -> str:
    return _now_local().strftime("%Y-%m-%d")


def _get_all_user_ids() -> List[str]:
    snaps = db.collection("users").stream()
    return [snap.id for snap in snaps if getattr(snap, "id", None)]


def _fetch_progress_summary(user_id: str) -> Dict[str, Any]:
    snap = (
        db.collection("users")
        .document(user_id)
        .collection("progressStats")
        .document("summary")
        .get()
    )
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def _fetch_today_log(user_id: str) -> Dict[str, Any]:
    today = _today_key_local()
    snap = db.collection("users").document(user_id).collection("dailyLogs").document(today).get()
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def _already_sent_slot(user_id: str, slot: str) -> bool:
    snap = (
        db.collection("users")
        .document(user_id)
        .collection("proactiveState")
        .document("daily")
        .get()
    )
    if not snap.exists:
        return False

    payload = snap.to_dict() or {}
    if str(payload.get("date") or "") != _today_key_local():
        return False

    slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
    return bool(slots.get(slot, False))


def _mark_slot_sent(user_id: str, slot: str) -> None:
    ref = db.collection("users").document(user_id).collection("proactiveState").document("daily")
    ref.set(
        {
            "date": _today_key_local(),
            f"slots.{slot}": True,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )


def _estimate_calorie_gap(today_log: Dict[str, Any]) -> int:
    meal_text = str(today_log.get("meal_text") or "").lower()
    meal_logged = bool(today_log.get("meal_logged", False)) or bool(meal_text.strip())

    if not meal_logged:
        return 600

    meals_count = 0
    for token in ["breakfast", "lunch", "dinner", "snack"]:
        if token in meal_text:
            meals_count += 1

    if meals_count == 0:
        meals_count = 1

    workout_minutes = today_log.get("workout_minutes", 0)
    extra = 100 if isinstance(workout_minutes, (int, float)) and int(workout_minutes) >= 45 else 0

    gap = max(0, 600 - (meals_count * 200) + extra)
    return int(gap)


def _build_morning_message(progress_summary: Dict[str, Any]) -> str:
    completed_days = int(progress_summary.get("total_workout_days", 0) or 0)
    day_number = (completed_days % 7) + 1
    week_number = (completed_days // 7) + 1
    focus = WORKOUT_FOCUS_MAP.get(day_number, "Training Day")

    history = progress_summary.get("recent_workout_history") if isinstance(progress_summary.get("recent_workout_history"), list) else []
    recent = history[:3]
    recent_minutes: List[int] = []
    for item in recent:
        minutes = item.get("workout_minutes") if isinstance(item, dict) else 0
        if isinstance(minutes, (int, float)) and int(minutes) > 0:
            recent_minutes.append(int(minutes))

    if recent_minutes:
        avg = sum(recent_minutes) / len(recent_minutes)
        if avg >= 45:
            return (
                f"Good morning. Today is Week {week_number} Day {day_number} ({focus}). "
                f"Based on your last 3 workouts (about {int(avg)} min avg), reduce intensity slightly and prioritize form."
            )
        return (
            f"Good morning. Today is Week {week_number} Day {day_number} ({focus}). "
            f"Your recent load looks manageable ({int(avg)} min avg). Keep a steady pace and finish strong."
        )

    return (
        f"Good morning. Today is Week {week_number} Day {day_number} ({focus}). "
        "Start with a moderate session and share your log so I can adapt tomorrow automatically."
    )


def _build_afternoon_message(today_log: Dict[str, Any]) -> str | None:
    meal_logged = bool(today_log.get("meal_logged", False)) or bool(str(today_log.get("meal_text") or "").strip())
    if meal_logged:
        return "Afternoon check-in: nice meal consistency so far. Keep hydration high and stay on your portions."
    return "You have not logged meals today. Want a quick meal suggestion based on your current plan?"


def _build_night_message(today_log: Dict[str, Any]) -> str | None:
    gap = _estimate_calorie_gap(today_log)
    if gap < 200:
        return "Great close to the day. Your intake looks reasonably aligned. Keep tomorrow equally consistent."
    return f"Night check-in: you are estimated to be about {gap} calories short today. Suggesting a quick protein-fiber snack before bed."


def _ensure_latest_conversation(user_id: str) -> str:
    conv_col = db.collection("users").document(user_id).collection("conversations")
    docs = conv_col.order_by("updatedAt", direction=firestore.Query.DESCENDING).limit(1).get()
    if docs:
        return docs[0].id

    conv_ref = conv_col.document()
    conv_ref.set(
        {
            "title": "Proactive Coach Updates",
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "lastMessage": "Autonomous coaching updates enabled.",
        },
        merge=True,
    )
    return conv_ref.id


def _persist_proactive_message(user_id: str, slot: str, text: str, meta: Dict[str, Any]) -> None:
    nudge_ref = db.collection("users").document(user_id).collection("proactiveNudges").document()
    nudge_ref.set(
        {
            "slot": slot,
            "text": text,
            "meta": meta,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "date": _today_key_local(),
        }
    )

    conv_id = _ensure_latest_conversation(user_id)
    conv_ref = db.collection("users").document(user_id).collection("conversations").document(conv_id)
    conv_ref.collection("messages").add(
        {
            "role": "assistant",
            "text": text,
            "payload": {
                "system_generated": True,
                "proactive": True,
                "slot": slot,
                "meta": meta,
            },
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )
    conv_ref.set(
        {
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "lastMessage": text[:200],
        },
        merge=True,
    )

    db.collection("users").document(user_id).collection("agentEvents").add(
        {
            "summary": f"Proactive {slot} nudge delivered",
            "actions": ["proactive_nudge_delivered"],
            "decision": {
                "slot": slot,
                "meta": meta,
            },
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )


def run_proactive_slot(slot: str, user_id: str | None = None) -> Dict[str, Any]:
    if slot not in {"morning", "afternoon", "night"}:
        raise ValueError("slot must be one of: morning, afternoon, night")

    user_ids = [user_id] if user_id else _get_all_user_ids()

    delivered = 0
    skipped = 0

    for uid in user_ids:
        if not uid:
            continue

        if _already_sent_slot(uid, slot):
            skipped += 1
            continue

        progress = _fetch_progress_summary(uid)
        today_log = _fetch_today_log(uid)

        if slot == "morning":
            text = _build_morning_message(progress)
            meta = {
                "target_week": (int(progress.get("total_workout_days", 0) or 0) // 7) + 1,
                "target_day": (int(progress.get("total_workout_days", 0) or 0) % 7) + 1,
            }
        elif slot == "afternoon":
            text = _build_afternoon_message(today_log)
            meta = {
                "meal_logged_today": bool(today_log.get("meal_logged", False)) or bool(str(today_log.get("meal_text") or "").strip()),
            }
        else:
            text = _build_night_message(today_log)
            meta = {
                "estimated_calorie_gap": _estimate_calorie_gap(today_log),
            }

        if not text:
            skipped += 1
            _mark_slot_sent(uid, slot)
            continue

        _persist_proactive_message(uid, slot, text, meta)
        _mark_slot_sent(uid, slot)
        delivered += 1

    return {
        "ok": True,
        "slot": slot,
        "users_processed": len(user_ids),
        "delivered": delivered,
        "skipped": skipped,
        "date": _today_key_local(),
    }


def _fetch_recent_daily_logs(user_id: str, days: int = 3) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("dailyLogs")
        .order_by("date", direction=firestore.Query.DESCENDING)
        .limit(days)
        .get()
    )
    return [doc.to_dict() or {} for doc in docs]


def _fetch_user_profile(user_id: str) -> Dict[str, Any]:
    snap = db.collection("users").document(user_id).get()
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def _fetch_latest_health_record(user_id: str) -> Dict[str, Any]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("healthRecords")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )
    if not docs:
        return {}
    return docs[0].to_dict() or {}


def _is_workout_logged(item: Dict[str, Any]) -> bool:
    minutes = item.get("workout_minutes", 0)
    if isinstance(minutes, (int, float)) and int(minutes) > 0:
        return True
    return bool(item.get("workout_completed", False))


def _is_meal_logged(item: Dict[str, Any]) -> bool:
    meal_text = str(item.get("meal_text") or "").strip()
    return bool(item.get("meal_logged", False)) or bool(meal_text)


def _is_travel_context_active(user_id: str, recent_logs: List[Dict[str, Any]]) -> bool:
    for item in recent_logs[:14]:
        if bool(item.get("travel_window_closed", False)):
            return False
        if bool(item.get("travel_disruption", False)):
            return True

    snap = db.collection("users").document(user_id).collection("travelState").document("current").get()
    if not snap.exists:
        return False
    payload = snap.to_dict() or {}
    return bool(payload.get("active", False))


def _compute_recent_adherence(summary: Dict[str, Any], recent_logs: List[Dict[str, Any]]) -> float:
    raw = summary.get("adherence_rate_7d")
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))

    logs_7d = recent_logs[:7]
    workout_days = sum(1 for item in logs_7d if _is_workout_logged(item))
    meal_days = sum(1 for item in logs_7d if _is_meal_logged(item))
    return max(0.0, min(1.0, (workout_days + meal_days) / 14.0))


def _normalize_workout_day(day: Dict[str, Any], day_number: int) -> Dict[str, Any]:
    safe = day if isinstance(day, dict) else {}
    return {
        "day": str(safe.get("day") or f"Day {day_number}"),
        "warmup": safe.get("warmup") if isinstance(safe.get("warmup"), list) else ["5 min walk"],
        "exercises": safe.get("exercises") if isinstance(safe.get("exercises"), list) else [],
        "cooldown": safe.get("cooldown") if isinstance(safe.get("cooldown"), list) else ["Light stretching"],
        "tip": str(safe.get("tip") or "Stay consistent and keep form controlled."),
    }


def _normalize_nutrition_day(day: Dict[str, Any], day_number: int) -> Dict[str, Any]:
    safe = day if isinstance(day, dict) else {}
    return {
        "day": str(safe.get("day") or f"Day {day_number}"),
        "breakfast": safe.get("breakfast") if isinstance(safe.get("breakfast"), list) else ["Vegetable oats"],
        "lunch": safe.get("lunch") if isinstance(safe.get("lunch"), list) else ["Dal, rice, sabzi"],
        "snacks": safe.get("snacks") if isinstance(safe.get("snacks"), list) else ["Fruit + nuts"],
        "dinner": safe.get("dinner") if isinstance(safe.get("dinner"), list) else ["Roti + protein + salad"],
        "tip": str(safe.get("tip") or "Hydrate well and avoid long fasting gaps."),
    }


def _normalize_workout_plan(plan: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    raw = plan if isinstance(plan, list) else []
    for idx, day in enumerate(raw[:7], start=1):
        normalized.append(_normalize_workout_day(day, idx))
    while len(normalized) < 7:
        normalized.append(_normalize_workout_day({}, len(normalized) + 1))
    return normalized


def _normalize_nutrition_plan(plan: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    raw = plan if isinstance(plan, list) else []
    for idx, day in enumerate(raw[:7], start=1):
        normalized.append(_normalize_nutrition_day(day, idx))
    while len(normalized) < 7:
        normalized.append(_normalize_nutrition_day({}, len(normalized) + 1))
    return normalized


def _adjust_reps(value: Any, delta: int) -> str:
    text = str(value or "").strip()
    if not text:
        return "10"
    match = re.search(r"\d+", text)
    if not match:
        return text
    base = int(match.group(0))
    adjusted = max(6, base + delta)
    return text[: match.start()] + str(adjusted) + text[match.end() :]


def _adjust_rest_seconds(value: Any, delta: int) -> str:
    text = str(value or "").strip()
    if not text:
        return "60 sec"
    match = re.search(r"\d+", text)
    if not match:
        return text
    base = int(match.group(0))
    adjusted = max(20, base + delta)
    return text[: match.start()] + str(adjusted) + text[match.end() :]


def _workout_plan_increase_intensity(base_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated = _normalize_workout_plan(base_plan)
    for day in updated:
        next_exercises = []
        for ex in day.get("exercises", []):
            if not isinstance(ex, dict):
                continue
            sets_val = ex.get("sets")
            sets = int(sets_val) if isinstance(sets_val, (int, float)) and int(sets_val) > 0 else 3
            next_exercises.append(
                {
                    **ex,
                    "sets": min(6, sets + 1),
                    "reps": _adjust_reps(ex.get("reps"), +2),
                    "rest": _adjust_rest_seconds(ex.get("rest"), -10),
                }
            )
        day["exercises"] = next_exercises
        day["tip"] = "Progression week: slightly increase load while keeping technique strict."
    return updated


def _workout_plan_simplify(base_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated = _normalize_workout_plan(base_plan)
    for day in updated:
        next_exercises = []
        for ex in day.get("exercises", []):
            if not isinstance(ex, dict):
                continue
            sets_val = ex.get("sets")
            sets = int(sets_val) if isinstance(sets_val, (int, float)) and int(sets_val) > 0 else 3
            next_exercises.append(
                {
                    **ex,
                    "sets": max(1, sets - 1),
                    "reps": _adjust_reps(ex.get("reps"), -2),
                    "rest": _adjust_rest_seconds(ex.get("rest"), +15),
                }
            )
        day["exercises"] = next_exercises[: max(1, len(next_exercises) - 1)] if next_exercises else []
        day["tip"] = "Recovery-first week: lower volume and rebuild consistency with easier sessions."
    return updated


def _workout_plan_maintenance(base_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated = _normalize_workout_plan(base_plan)
    for day in updated:
        day["warmup"] = ["10 min light walk or mobility"]
        day["exercises"] = []
        day["cooldown"] = ["5-10 min breathing and gentle stretching"]
        day["tip"] = "Maintenance mode: preserve routine and recovery while travel context is active."
    return updated


def _nutrition_plan_increase_intensity(base_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated = _normalize_nutrition_plan(base_plan)
    for day in updated:
        snacks = day.get("snacks") if isinstance(day.get("snacks"), list) else []
        day["snacks"] = [*snacks[:2], "Post-workout protein-rich option"]
        day["tip"] = "High-adherence week: keep protein timing consistent and fuel training quality."
    return updated


def _nutrition_plan_simplify(base_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated = _normalize_nutrition_plan(base_plan)
    for day in updated:
        day["breakfast"] = (day.get("breakfast") or [])[:2]
        day["lunch"] = (day.get("lunch") or [])[:2]
        day["snacks"] = (day.get("snacks") or [])[:1]
        day["dinner"] = (day.get("dinner") or [])[:2]
        day["tip"] = "Low-friction week: simpler meals, repeatable choices, and focus on logging consistency."
    return updated


def _nutrition_plan_maintenance(base_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated = _normalize_nutrition_plan(base_plan)
    for day in updated:
        day["breakfast"] = ["Portable protein option", "One fruit"]
        day["lunch"] = ["Simple thali: dal + sabzi + controlled rice/roti"]
        day["snacks"] = ["Roasted chana or nuts", "Hydration check"]
        day["dinner"] = ["Lean protein + vegetables", "Lighter dinner than lunch"]
        day["tip"] = "Maintenance mode: travel-practical meals, hydration, and portion control."
    return updated


def _save_plan_evolution_revision(
    user_id: str,
    collection_name: str,
    plan: List[Dict[str, Any]],
    goal: str,
    mode: str,
    adherence: float,
    previous_plan_id: str | None,
) -> str:
    col_ref = db.collection("users").document(user_id).collection(collection_name)
    doc_ref = col_ref.document()
    now_iso = _now_local().isoformat()

    doc_ref.set(
        {
            "plan": plan,
            "goal": goal,
            "source": "weekly_plan_evolution",
            "reason": mode,
            "isLatest": True,
            "evolvedFromActivity": True,
            "evolutionBanner": "Your plan evolved based on your activity",
            "evolutionMeta": {
                "mode": mode,
                "adherence_rate_7d": round(adherence, 2),
                "previous_plan_id": previous_plan_id,
                "trigger": "weekly_scheduler",
                "triggered_at": now_iso,
            },
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
    )

    latest_docs = col_ref.where("isLatest", "==", True).stream()
    batch = db.batch()
    for snap in latest_docs:
        if snap.id == doc_ref.id:
            continue
        batch.set(
            snap.reference,
            {
                "isLatest": False,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
    batch.commit()

    return doc_ref.id


def _already_evolved_recently(user_id: str) -> bool:
    events = _fetch_recent_agent_events(user_id, max_entries=30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=6)
    for event in events:
        if str(event.get("type", "")).lower() != "plan_evolution":
            continue
        created = _to_utc_datetime(event.get("createdAt"))
        if created is not None and created >= cutoff:
            return True
    return False


def _build_default_workout_seed(profile: Dict[str, Any]) -> Any:
    return type(
        "WorkoutSeed",
        (),
        {
            "goal": str(profile.get("goal") or "general fitness"),
            "location": str(profile.get("location") or "home"),
            "time_per_day": int(profile.get("time_per_day", 30) or 30),
            "fitness_level": str(profile.get("fitness_level") or "beginner"),
            "equipment": str(profile.get("equipment") or "none"),
        },
    )()


def _build_default_nutrition_seed(profile: Dict[str, Any]) -> Any:
    return type(
        "NutritionSeed",
        (),
        {
            "goal": str(profile.get("goal") or "general fitness"),
            "diet": str(profile.get("diet") or "balanced"),
            "activity": str(profile.get("activity") or "moderate"),
            "allergies": str(profile.get("allergies") or "none"),
        },
    )()


def evaluate_and_evolve_plan(uid: str) -> Dict[str, Any]:
    if not uid:
        return {"ok": False, "status": "invalid_user"}

    if _already_evolved_recently(uid):
        return {
            "ok": True,
            "status": "skipped_recent_evolution",
            "user_id": uid,
        }

    summary = _fetch_progress_summary(uid)
    recent_logs = _fetch_recent_daily_logs(uid, days=14)
    adherence = _compute_recent_adherence(summary, recent_logs)
    travel_active = _is_travel_context_active(uid, recent_logs)

    if travel_active:
        mode = "maintenance"
    elif adherence > 0.80:
        mode = "increase_intensity"
    elif adherence < 0.50:
        mode = "simplify"
    else:
        return {
            "ok": True,
            "status": "skipped_no_adjustment_needed",
            "user_id": uid,
            "adherence_rate_7d": round(adherence, 2),
        }

    workout_latest = _fetch_latest_plan(uid, "workoutPlans")
    nutrition_latest = _fetch_latest_plan(uid, "nutritionPlans")
    profile = {**_fetch_user_profile(uid), **_fetch_latest_health_record(uid)}
    goal = str(
        workout_latest.get("goal")
        or nutrition_latest.get("goal")
        or profile.get("goal")
        or "general fitness"
    )

    workout_plan = workout_latest.get("plan") if isinstance(workout_latest.get("plan"), list) else []
    nutrition_plan = nutrition_latest.get("plan") if isinstance(nutrition_latest.get("plan"), list) else []

    if not workout_plan:
        generated = generate_workout_plan(_build_default_workout_seed(profile))
        workout_plan = generated.get("plan") if isinstance(generated.get("plan"), list) else []
    if not nutrition_plan:
        generated = generate_nutrition_plan(_build_default_nutrition_seed(profile))
        nutrition_plan = generated.get("plan") if isinstance(generated.get("plan"), list) else []

    if mode == "increase_intensity":
        next_workout = _workout_plan_increase_intensity(workout_plan)
        next_nutrition = _nutrition_plan_increase_intensity(nutrition_plan)
    elif mode == "simplify":
        next_workout = _workout_plan_simplify(workout_plan)
        next_nutrition = _nutrition_plan_simplify(nutrition_plan)
    else:
        next_workout = _workout_plan_maintenance(workout_plan)
        next_nutrition = _nutrition_plan_maintenance(nutrition_plan)

    workout_doc_id = _save_plan_evolution_revision(
        user_id=uid,
        collection_name="workoutPlans",
        plan=next_workout,
        goal=goal,
        mode=mode,
        adherence=adherence,
        previous_plan_id=workout_latest.get("id") if isinstance(workout_latest, dict) else None,
    )
    nutrition_doc_id = _save_plan_evolution_revision(
        user_id=uid,
        collection_name="nutritionPlans",
        plan=next_nutrition,
        goal=goal,
        mode=mode,
        adherence=adherence,
        previous_plan_id=nutrition_latest.get("id") if isinstance(nutrition_latest, dict) else None,
    )

    db.collection("users").document(uid).collection("agentEvents").add(
        {
            "type": "plan_evolution",
            "summary": "Weekly plans evolved based on adherence and context",
            "actions": ["plans_refreshed", "weekly_plan_evolved"],
            "source": "weekly_scheduler",
            "decision": {
                "mode": mode,
                "adherence_rate_7d": round(adherence, 2),
                "travel_active": travel_active,
                "workout_plan_id": workout_doc_id,
                "nutrition_plan_id": nutrition_doc_id,
            },
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {
        "ok": True,
        "status": "evolved",
        "user_id": uid,
        "mode": mode,
        "adherence_rate_7d": round(adherence, 2),
        "travel_active": travel_active,
        "workout_plan_id": workout_doc_id,
        "nutrition_plan_id": nutrition_doc_id,
    }


def run_weekly_plan_evolution(user_id: str | None = None) -> Dict[str, Any]:
    user_ids = [user_id] if user_id else _get_all_user_ids()

    results: List[Dict[str, Any]] = []
    evolved = 0
    skipped = 0
    errors = 0

    for uid in user_ids:
        if not uid:
            continue
        try:
            result = evaluate_and_evolve_plan(uid)
            results.append(result)
            if result.get("status") == "evolved":
                evolved += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            results.append(
                {
                    "ok": False,
                    "status": "error",
                    "user_id": uid,
                    "error": str(exc),
                }
            )

    return {
        "ok": errors == 0,
        "users_processed": len(user_ids),
        "evolved": evolved,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }


def _fetch_latest_plan(user_id: str, collection_name: str) -> Dict[str, Any]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection(collection_name)
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )
    if not docs:
        return {}

    payload = docs[0].to_dict() or {}
    return {"id": docs[0].id, **payload}


def _fetch_recent_agent_events(user_id: str, max_entries: int = 25) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("agentEvents")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(max_entries)
        .get()
    )
    return [doc.to_dict() or {} for doc in docs]


def _to_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_dedupe_hash(action: str, message: str) -> str:
    key = f"{action.strip().lower()}|{message.strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _is_duplicate_suggestion(
    dedupe_hash: str,
    recent_agent_events: List[Dict[str, Any]],
    dedupe_hours: int,
    now_utc: datetime,
) -> bool:
    cutoff = now_utc - timedelta(hours=max(1, dedupe_hours))
    for event in recent_agent_events:
        if str(event.get("type", "")).lower() != "proactive":
            continue

        existing_hash = str(event.get("dedupe_hash", "")).strip()
        if existing_hash != dedupe_hash:
            continue

        created_at = _to_utc_datetime(event.get("createdAt"))
        if created_at is None:
            return True
        if created_at >= cutoff:
            return True
    return False


def _persist_autonomous_proactive_event(
    user_id: str,
    action: str,
    priority: str,
    message: str,
    why_this_action: str,
    dedupe_hash: str,
    context_payload: Dict[str, Any],
) -> None:
    db.collection("users").document(user_id).collection("agentEvents").add(
        {
            "type": "proactive",
            "source": "autonomous-loop",
            "action": action,
            "priority": priority,
            "message": message,
            "why_this_action": why_this_action,
            "decision_path": [
                "autonomous-loop",
                "proactive-recommendation",
                f"action:{action}",
            ],
            "inputs_used": {
                "priority": priority,
                "dedupe_hash": dedupe_hash,
                "progress_summary_present": bool(context_payload.get("progress_summary")),
                "daily_log_count": len(context_payload.get("last_3_daily_logs") or []),
            },
            "dedupe_hash": dedupe_hash,
            "summary": f"Proactive suggestion: {action}",
            "actions": ["proactive_suggestion_generated", action],
            "decision": {
                "action": action,
                "priority": priority,
                "why_this_action": why_this_action,
                "message": message,
            },
            "context": context_payload,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )


def _build_recommendation_message(rec: Dict[str, Any]) -> str:
    suggested = str(rec.get("suggested_message") or "").strip()
    title = str(rec.get("title") or "").strip()
    reason = str(rec.get("reason") or "").strip()
    if suggested:
        return suggested
    if title:
        return title
    return reason or "Take the next best wellness action and log your progress."


def _build_why_action(rec: Dict[str, Any]) -> str:
    reason = str(rec.get("reason") or "").strip()
    title = str(rec.get("title") or "").strip()
    if reason:
        return reason
    if title:
        return f"This recommendation is based on your recent trend: {title}."
    return "This recommendation is based on your recent progress and adherence trends."


async def _build_user_context(user_id: str) -> Dict[str, Any]:
    progress_task = asyncio.to_thread(_fetch_progress_summary, user_id)
    logs_task = asyncio.to_thread(_fetch_recent_daily_logs, user_id, 3)
    workout_task = asyncio.to_thread(_fetch_latest_plan, user_id, "workoutPlans")
    nutrition_task = asyncio.to_thread(_fetch_latest_plan, user_id, "nutritionPlans")
    events_task = asyncio.to_thread(_fetch_recent_agent_events, user_id, 25)

    progress, logs, workout, nutrition, events = await asyncio.gather(
        progress_task,
        logs_task,
        workout_task,
        nutrition_task,
        events_task,
    )

    return {
        "progress_summary": progress,
        "daily_logs": logs,
        "current_workout_plan": workout,
        "current_nutrition_plan": nutrition,
        "recent_agent_events": events,
    }


def _is_active_user_context(context_payload: Dict[str, Any]) -> bool:
    if context_payload.get("progress_summary"):
        return True
    if context_payload.get("daily_logs"):
        return True
    if context_payload.get("current_workout_plan"):
        return True
    if context_payload.get("current_nutrition_plan"):
        return True
    return False


async def _process_user_autonomous_proactive(user_id: str, dedupe_hours: int) -> Dict[str, Any]:
    context_payload = await _build_user_context(user_id)

    if not _is_active_user_context(context_payload):
        return {
            "user_id": user_id,
            "active": False,
            "generated": 0,
            "duplicates_skipped": 0,
            "status": "skipped_inactive",
        }

    proactive_result = await asyncio.to_thread(get_proactive_recommendations, user_id, False)
    recommendations = proactive_result.get("recommendations") if isinstance(proactive_result.get("recommendations"), list) else []

    if not recommendations:
        return {
            "user_id": user_id,
            "active": True,
            "generated": 0,
            "duplicates_skipped": 0,
            "status": "no_intervention_needed",
        }

    generated = 0
    duplicates = 0
    recent_events = list(context_payload.get("recent_agent_events") or [])
    now_utc = datetime.now(timezone.utc)

    for rec in recommendations:
        action = str(rec.get("type") or "general_coaching").strip() or "general_coaching"
        priority = str(rec.get("priority") or "medium").strip().lower() or "medium"
        message = _build_recommendation_message(rec)
        why_this_action = _build_why_action(rec)
        dedupe_hash = _build_dedupe_hash(action, message)

        if _is_duplicate_suggestion(dedupe_hash, recent_events, dedupe_hours, now_utc):
            duplicates += 1
            continue

        await asyncio.to_thread(
            _persist_autonomous_proactive_event,
            user_id,
            action,
            priority,
            message,
            why_this_action,
            dedupe_hash,
            {
                "progress_summary": context_payload.get("progress_summary", {}),
                "last_3_daily_logs": context_payload.get("daily_logs", []),
                "current_workout_plan": context_payload.get("current_workout_plan", {}),
                "current_nutrition_plan": context_payload.get("current_nutrition_plan", {}),
            },
        )
        recent_events.insert(
            0,
            {
                "type": "proactive",
                "dedupe_hash": dedupe_hash,
                "createdAt": now_utc,
            },
        )
        generated += 1

    return {
        "user_id": user_id,
        "active": True,
        "generated": generated,
        "duplicates_skipped": duplicates,
        "status": "ok",
    }


async def run_autonomous_proactive_cycle(
    user_id: str | None = None,
    dedupe_hours: int | None = None,
) -> Dict[str, Any]:
    effective_dedupe_hours = dedupe_hours if isinstance(dedupe_hours, int) else PROACTIVE_DEDUP_HOURS
    user_ids = [user_id] if user_id else await asyncio.to_thread(_get_all_user_ids)

    if not user_ids:
        return {
            "ok": True,
            "users_processed": 0,
            "active_users": 0,
            "events_generated": 0,
            "duplicates_skipped": 0,
            "results": [],
        }

    sem = asyncio.Semaphore(max(1, PROACTIVE_MAX_USER_CONCURRENCY))

    async def _bounded_process(uid: str) -> Dict[str, Any]:
        async with sem:
            return await _process_user_autonomous_proactive(uid, effective_dedupe_hours)

    results = await asyncio.gather(*[_bounded_process(uid) for uid in user_ids], return_exceptions=True)

    normalized_results: List[Dict[str, Any]] = []
    active_users = 0
    events_generated = 0
    duplicates_skipped = 0

    for idx, result in enumerate(results):
        if isinstance(result, Exception):
            normalized_results.append(
                {
                    "user_id": user_ids[idx],
                    "active": False,
                    "generated": 0,
                    "duplicates_skipped": 0,
                    "status": "error",
                    "error": str(result),
                }
            )
            continue

        normalized_results.append(result)
        if result.get("active"):
            active_users += 1
        events_generated += int(result.get("generated", 0) or 0)
        duplicates_skipped += int(result.get("duplicates_skipped", 0) or 0)

    return {
        "ok": True,
        "users_processed": len(user_ids),
        "active_users": active_users,
        "events_generated": events_generated,
        "duplicates_skipped": duplicates_skipped,
        "results": normalized_results,
    }


async def run_autonomous_proactive_loop(stop_event: asyncio.Event, interval_hours: float | None = None) -> None:
    hours = interval_hours if isinstance(interval_hours, (int, float)) else PROACTIVE_INTERVAL_HOURS
    effective_interval_seconds = max(300.0, float(hours) * 3600.0)

    while not stop_event.is_set():
        try:
            await run_autonomous_proactive_cycle()
        except Exception as exc:
            print(f"[autonomous-proactive-loop] cycle failed: {exc}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=effective_interval_seconds)
        except asyncio.TimeoutError:
            continue


def _iter_old_proactive_event_candidates(
    user_id: str,
    cutoff_utc: datetime,
    max_events_per_user: int,
) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("agentEvents")
        .order_by("createdAt", direction=firestore.Query.ASCENDING)
        .limit(max(1, max_events_per_user))
        .get()
    )

    candidates: List[Dict[str, Any]] = []
    for doc in docs:
        payload = doc.to_dict() or {}
        if str(payload.get("type", "")).lower() != "proactive":
            continue

        created_at = _to_utc_datetime(payload.get("createdAt"))
        if created_at is None:
            continue

        if created_at >= cutoff_utc:
            continue

        candidates.append(
            {
                "doc_id": doc.id,
                "doc_ref": doc.reference,
                "payload": payload,
            }
        )

    return candidates


def _archive_proactive_event_candidates(user_id: str, candidates: List[Dict[str, Any]], dry_run: bool) -> int:
    if not candidates:
        return 0

    if dry_run:
        return len(candidates)

    archived = 0
    archive_col = db.collection("users").document(user_id).collection("agentEventsArchive")
    batch = db.batch()
    op_count = 0

    for item in candidates:
        source_ref = item["doc_ref"]
        source_payload = item["payload"]
        archive_ref = archive_col.document(item["doc_id"])

        batch.set(
            archive_ref,
            {
                **source_payload,
                "archivedAt": firestore.SERVER_TIMESTAMP,
                "archivedFrom": "agentEvents",
                "originalEventId": item["doc_id"],
            },
            merge=True,
        )
        batch.delete(source_ref)
        op_count += 2
        archived += 1

        # Firestore WriteBatch allows up to 500 operations per commit.
        if op_count >= 400:
            batch.commit()
            batch = db.batch()
            op_count = 0

    if op_count > 0:
        batch.commit()

    return archived


def run_proactive_event_retention_cleanup(
    user_id: str | None = None,
    retention_days: int | None = None,
    max_events_per_user: int | None = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    effective_retention_days = retention_days if isinstance(retention_days, int) else PROACTIVE_RETENTION_DAYS
    effective_max_events = max_events_per_user if isinstance(max_events_per_user, int) else PROACTIVE_ARCHIVE_MAX_EVENTS_PER_USER

    effective_retention_days = max(1, effective_retention_days)
    effective_max_events = max(1, effective_max_events)

    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=effective_retention_days)
    user_ids = [user_id] if user_id else _get_all_user_ids()

    total_candidates = 0
    total_archived = 0
    errors = 0
    per_user: List[Dict[str, Any]] = []

    for uid in user_ids:
        if not uid:
            continue

        try:
            candidates = _iter_old_proactive_event_candidates(uid, cutoff_utc, effective_max_events)
            archived_count = _archive_proactive_event_candidates(uid, candidates, dry_run)

            total_candidates += len(candidates)
            total_archived += archived_count
            per_user.append(
                {
                    "user_id": uid,
                    "candidates": len(candidates),
                    "archived": archived_count,
                    "status": "ok",
                }
            )
        except Exception as exc:
            errors += 1
            per_user.append(
                {
                    "user_id": uid,
                    "candidates": 0,
                    "archived": 0,
                    "status": "error",
                    "error": str(exc),
                }
            )

    return {
        "ok": errors == 0,
        "dry_run": dry_run,
        "retention_days": effective_retention_days,
        "cutoff_utc": cutoff_utc.isoformat(),
        "users_processed": len(user_ids),
        "events_considered_for_archive": total_candidates,
        "events_archived": total_archived,
        "errors": errors,
        "results": per_user,
    }
