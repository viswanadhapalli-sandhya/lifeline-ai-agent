import json
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

from firebase_admin import firestore

from app.core.firebase_client import db
from app.core.groq_client import generate_ai_response, generate_ai_text_response
from app.schemas.agent import AgentRequest, AgentResponse, AgentStep
from app.services.nutrition_service import generate_nutrition_plan
from app.services.risk_engine import simple_risk_engine
from app.services.workout_service import generate_workout_plan


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_json_safe(value: Any) -> Any:
    # Firestore SERVER_TIMESTAMP sentinels are not JSON-serializable.
    if type(value).__name__ == "Sentinel":
        return None

    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_to_json_safe(v) for v in value]

    return value


def _today_key() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def _safe_json_loads(text: str) -> Dict[str, Any]:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            return json.loads(match.group())
        return {"message": cleaned}


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return default


def _fetch_user_profile(user_id: str) -> Dict[str, Any]:
    snap = db.collection("users").document(user_id).get()
    if not snap.exists:
        # Some flows only create nested collections (e.g., healthRecords)
        # without creating the root user document first.
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


def _fetch_recent_daily_logs(user_id: str, days: int = 7) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("dailyLogs")
        .order_by("date", direction=firestore.Query.DESCENDING)
        .limit(days)
        .get()
    )
    return [doc.to_dict() or {} for doc in docs]


def _fetch_all_daily_logs(user_id: str, max_entries: int = 1000) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("dailyLogs")
        .order_by("date", direction=firestore.Query.DESCENDING)
        .limit(max_entries)
        .get()
    )
    return [doc.to_dict() or {} for doc in docs]


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


def _upsert_daily_log(user_id: str, updates: Dict[str, Any], date_key: str | None = None) -> None:
    day = date_key or _today_key()
    ref = db.collection("users").document(user_id).collection("dailyLogs").document(day)
    updates = {
        **updates,
        "date": day,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    ref.set(updates, merge=True)


def _fetch_daily_log(user_id: str, date_key: str | None = None) -> Dict[str, Any]:
    day = date_key or _today_key()
    snap = db.collection("users").document(user_id).collection("dailyLogs").document(day).get()
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def _refresh_progress_summary(user_id: str, logs: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    all_logs = logs if isinstance(logs, list) else _fetch_all_daily_logs(user_id)

    workout_days = _count_workout_days(all_logs)
    max_workout_day_number = 0
    meal_days = _count_meal_logged_days(all_logs)
    total_workout_minutes = 0
    recent_workout_history: List[Dict[str, Any]] = []

    for item in all_logs:
        minutes = item.get("workout_minutes", 0)
        if isinstance(minutes, (int, float)):
            total_workout_minutes += max(0, int(minutes))

        completed = bool(item.get("workout_completed", False))
        day_worked = completed or (isinstance(minutes, (int, float)) and int(minutes) > 0)
        date_key = item.get("date")
        day_number = item.get("workout_day_number")
        if isinstance(day_number, (int, float)) and int(day_number) > 0:
            max_workout_day_number = max(max_workout_day_number, int(day_number))
        if day_worked and isinstance(date_key, str):
            recent_workout_history.append(
                {
                    "date": date_key,
                    "workout_minutes": int(minutes) if isinstance(minutes, (int, float)) else 0,
                    "workout_completed": completed,
                    "workout_day_number": int(day_number) if isinstance(day_number, (int, float)) and int(day_number) > 0 else None,
                }
            )

    # If user explicitly reports plan day number (e.g., day 2), trust that progression.
    workout_days = max(workout_days, max_workout_day_number)

    summary_to_store = {
        "total_workout_days": workout_days,
        "total_meal_log_days": meal_days,
        "total_daily_logs": len(all_logs),
        "total_workout_minutes": total_workout_minutes,
        "recent_workout_history": recent_workout_history[:30],
        "lastUpdatedAt": firestore.SERVER_TIMESTAMP,
    }

    (
        db.collection("users")
        .document(user_id)
        .collection("progressStats")
        .document("summary")
        .set(summary_to_store, merge=True)
    )

    response_summary = {
        **summary_to_store,
        "lastUpdatedAt": _utc_now().isoformat(),
    }
    return _to_json_safe(response_summary)


def _record_agent_event(user_id: str, payload: Dict[str, Any]) -> None:
    db.collection("users").document(user_id).collection("agentEvents").add(
        {
            **payload,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )


def _conversation_title_from_message(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return "New chat"
    return text[:60]


def _ensure_conversation(user_id: str, conversation_id: str | None, seed_message: str) -> str:
    conv_col = db.collection("users").document(user_id).collection("conversations")

    if conversation_id:
        conv_ref = conv_col.document(conversation_id)
        snap = conv_ref.get()
        if not snap.exists:
            conv_ref.set(
                {
                    "title": _conversation_title_from_message(seed_message),
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                    "lastMessage": (seed_message or "").strip()[:200],
                },
                merge=True,
            )
        else:
            conv_ref.set(
                {
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        return conversation_id

    conv_ref = conv_col.document()
    conv_ref.set(
        {
            "title": _conversation_title_from_message(seed_message),
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "lastMessage": (seed_message or "").strip()[:200],
        },
        merge=True,
    )
    return conv_ref.id


def _append_conversation_message(
    user_id: str,
    conversation_id: str,
    role: str,
    text: str,
    payload: Dict[str, Any] | None = None,
) -> None:
    if not text:
        return

    conv_ref = db.collection("users").document(user_id).collection("conversations").document(conversation_id)
    conv_ref.collection("messages").add(
        {
            "role": role,
            "text": text,
            "payload": payload or {},
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


def _fetch_latest_agent_event(user_id: str) -> Dict[str, Any]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("agentEvents")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )
    if not docs:
        return {}
    return docs[0].to_dict() or {}


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


def _save_plan_revision(
    user_id: str,
    collection_name: str,
    plan_key: str,
    plan_payload: Dict[str, Any],
    reason: str,
    goal: str,
) -> str:
    doc_ref = (
        db.collection("users")
        .document(user_id)
        .collection(collection_name)
        .document()
    )
    doc_ref.set(
        {
            "plan": plan_payload.get(plan_key, []),
            "goal": goal,
            "reason": reason,
            "source": "agent",
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )
    return doc_ref.id


def _to_predict_request(data: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        age=int(data.get("age", 25)),
        gender=str(data.get("gender", "Unknown")),
        height=data.get("height"),
        weight=data.get("weight"),
        sleep=int(data.get("sleep", 7)),
        exercise=int(data.get("exercise", 30)),
        stress=int(data.get("stress", 5)),
        smoking=bool(data.get("smoking", False)),
        alcohol=int(data.get("alcohol", 0)),
        medical=str(data.get("medical", "None")),
        activity=data.get("activity"),
        diet=data.get("diet"),
    )


def _resolve_goal(request_goal: str | None, profile_goal: str | None, message: str) -> str:
    message_l = (message or "").lower()

    # User correction should override stale profile defaults.
    correction_tokens = [
        "no weight loss",
        "not weight loss",
        "dont need to lose weight",
        "don't need to lose weight",
        "no need to lose weight",
        "i am underweight",
    ]
    if any(token in message_l for token in correction_tokens):
        return "general fitness"

    if isinstance(request_goal, str) and request_goal.strip():
        return request_goal.strip()

    if isinstance(profile_goal, str) and profile_goal.strip():
        return profile_goal.strip()

    # Neutral fallback avoids incorrect fat-loss coaching.
    return "general fitness"


def _build_workout_input(profile: Dict[str, Any], goal: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        goal=goal or profile.get("goal") or "general fitness",
        location=profile.get("location") or "home",
        time_per_day=int(profile.get("time_per_day", 30)),
        fitness_level=profile.get("fitness_level") or "beginner",
        equipment=profile.get("equipment") or "none",
    )


def _build_nutrition_input(profile: Dict[str, Any], goal: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        goal=goal or profile.get("goal") or "general fitness",
        diet=profile.get("diet") or "balanced",
        activity=profile.get("activity") or "moderate",
        allergies=profile.get("allergies") or "none",
    )


def _extract_structured_logs(message: str) -> Dict[str, Any]:
    text = (message or "").strip()
    lowered = text.lower()

    extracted: Dict[str, Any] = {}

    if _is_travel_resume_message(text):
        extracted["travel_window_closed"] = True
        extracted["travel_disruption"] = False
        extracted["travel_days"] = 0
        extracted["compensation_request"] = False

    weight_match = re.search(r"(\d+(?:\.\d+)?)\s?kg", lowered)
    if weight_match:
        extracted["weight_kg"] = float(weight_match.group(1))

    workout_match = re.search(r"(\d+)\s?(?:min|mins|minutes).{0,15}(?:workout|exercise|walk|run)", lowered)
    if workout_match:
        extracted["workout_minutes"] = int(workout_match.group(1))

    if any(token in lowered for token in ["ate", "breakfast", "lunch", "dinner", "snack"]):
        extracted["meal_text"] = text
        extracted["meal_logged"] = True

    if "skipped workout" in lowered or "missed workout" in lowered:
        extracted["workout_minutes"] = 0

    if any(
        token in lowered
        for token in [
            "did all workouts",
            "did all the workouts",
            "completed workout",
            "finished workout",
            "did the workout",
        ]
    ):
        extracted["workout_completed"] = True

    # Handle phrases like "done with day 2", "completed day 3", "day 1 done".
    completion_day_match = re.search(r"\b(?:done|completed|finished)\s+(?:with\s+)?day\s*(\d{1,2})\b", lowered)
    if completion_day_match:
        extracted["workout_completed"] = True
        extracted["workout_day_number"] = int(completion_day_match.group(1))

    completion_day_match_alt = re.search(r"\bday\s*(\d{1,2})\s*(?:done|completed|finished)\b", lowered)
    if completion_day_match_alt:
        extracted["workout_completed"] = True
        extracted["workout_day_number"] = int(completion_day_match_alt.group(1))

    completion_day_match_over = re.search(r"\bday\s*(\d{1,2})\s*(?:is\s+)?over\b", lowered)
    if completion_day_match_over:
        extracted["workout_completed"] = True
        extracted["workout_day_number"] = int(completion_day_match_over.group(1))

    if (
        not bool(extracted.get("travel_window_closed", False))
        and any(token in lowered for token in ["travel", "travelling", "traveling", "out of town", "trip"])
    ):
        extracted["travel_disruption"] = True
        travel_days = _extract_travel_days(text)
        if travel_days > 0:
            extracted["travel_days"] = travel_days

    if any(token in lowered for token in ["compensate", "make up", "make-up", "restructure", "adjust plan"]):
        extracted["compensation_request"] = True

    positive_follow_tokens = [
        "followed well",
        "followed the plan",
        "plan followed",
        "did everything",
        "completed today's plan",
        "completed todays plan",
        "stuck to plan",
        "done with today's work",
        "done with todays work",
        "done for today",
        "finished today's workout",
        "finished todays workout",
        "completed today's work",
        "completed todays work",
        "done with today",
        "finished it",
        "finished today",
    ]
    if any(token in lowered for token in positive_follow_tokens):
        extracted["adherence_status"] = "good"
        # Positive adherence without explicit minutes should still count as completed day.
        extracted["workout_completed"] = True
        extracted["meal_logged"] = True

    negative_follow_tokens = [
        "couldn't follow",
        "couldnt follow",
        "did not follow",
        "didn't follow",
        "missed plan",
        "not followed",
    ]
    if any(token in lowered for token in negative_follow_tokens):
        extracted["adherence_status"] = "poor"

    return extracted


def _extract_travel_days(message: str) -> int:
    text = (message or "").lower()
    if not text:
        return 0

    def _clamp_days(days: int) -> int:
        return max(1, min(14, int(days)))

    def _resolve_day_in_calendar(day_number: int, reference: date) -> date | None:
        if day_number < 1 or day_number > 31:
            return None

        year = reference.year
        month = reference.month

        for month_offset in [0, 1, 2]:
            target_month = month + month_offset
            target_year = year + (target_month - 1) // 12
            target_month = ((target_month - 1) % 12) + 1
            try:
                candidate = date(target_year, target_month, day_number)
            except ValueError:
                continue

            if month_offset == 0 and candidate < reference:
                continue
            return candidate

        return None

    # Prefer explicit numeric mentions like "next 4 days".
    match = re.search(r"(?:next|for|about|around)?\s*(\d{1,2})\s*(?:day|days)", text)
    if match:
        return _clamp_days(int(match.group(1)))

    word_to_num = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
    }
    for word, value in word_to_num.items():
        if f"{word} day" in text or f"{word} days" in text:
            return value

    if any(token in text for token in ["few days", "next few days"]):
        return 3

    today = _utc_now().date()

    # Examples: "till Wednesday", "until next monday".
    weekday_map = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    weekday_match = re.search(
        r"\b(?:till|until|through)(?:\s+next)?\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    )
    if weekday_match:
        target_weekday = weekday_map[weekday_match.group(1)]
        delta = (target_weekday - today.weekday()) % 7
        if delta == 0:
            delta = 7
        return _clamp_days(delta)

    # Examples: "from 20th to 24th", "20 to 24".
    range_match = re.search(
        r"\b(?:from\s+)?(\d{1,2})(?:st|nd|rd|th)?\s*(?:to|\-|until|till)\s*(\d{1,2})(?:st|nd|rd|th)?\b",
        text,
    )
    if range_match:
        start_day = int(range_match.group(1))
        end_day = int(range_match.group(2))
        start_date = _resolve_day_in_calendar(start_day, today)
        if start_date:
            end_date = _resolve_day_in_calendar(end_day, start_date)
            if end_date:
                span = (end_date - start_date).days + 1
                return _clamp_days(span)

    # Example: "until 24th".
    until_day_match = re.search(r"\b(?:till|until)\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
    if until_day_match:
        end_day = int(until_day_match.group(1))
        end_date = _resolve_day_in_calendar(end_day, today)
        if end_date:
            span = (end_date - today).days + 1
            return _clamp_days(span)

    return 0


def _travel_workout_day(day_number: int) -> Dict[str, Any]:
    return {
        "day": f"Day {day_number}",
        "warmup": ["Travel day: no structured workout"],
        "exercises": [],
        "cooldown": ["5 min gentle breathing before sleep"],
        "tip": "Focus on hydration and sleep during travel.",
    }


def _ensure_workout_day_shape(day: Dict[str, Any], day_number: int) -> Dict[str, Any]:
    safe = day if isinstance(day, dict) else {}
    return {
        "day": str(safe.get("day") or f"Day {day_number}"),
        "warmup": safe.get("warmup") if isinstance(safe.get("warmup"), list) else ["5 min walk"],
        "exercises": safe.get("exercises") if isinstance(safe.get("exercises"), list) else [],
        "cooldown": safe.get("cooldown") if isinstance(safe.get("cooldown"), list) else ["Light stretching"],
        "tip": str(safe.get("tip") or "Consistency matters more than intensity."),
    }


def _ensure_nutrition_day_shape(day: Dict[str, Any], day_number: int) -> Dict[str, Any]:
    safe = day if isinstance(day, dict) else {}
    return {
        "day": str(safe.get("day") or f"Day {day_number}"),
        "breakfast": safe.get("breakfast") if isinstance(safe.get("breakfast"), list) else ["Poha + curd"],
        "lunch": safe.get("lunch") if isinstance(safe.get("lunch"), list) else ["Dal, rice, sabzi"],
        "snacks": safe.get("snacks") if isinstance(safe.get("snacks"), list) else ["Fruit + nuts"],
        "dinner": safe.get("dinner") if isinstance(safe.get("dinner"), list) else ["Roti + paneer/chana + salad"],
        "tip": str(safe.get("tip") or "Keep portions moderate and hydrate well."),
    }


def _build_travel_compensation_workout_plan(base_plan: List[Dict[str, Any]], travel_days: int) -> Dict[str, Any]:
    if not isinstance(base_plan, list):
        base_plan = []

    normalized = [_ensure_workout_day_shape(day, idx + 1) for idx, day in enumerate(base_plan[:7])]
    while len(normalized) < 7:
        normalized.append(_ensure_workout_day_shape({}, len(normalized) + 1))

    td = max(1, min(6, travel_days))
    for idx in range(td):
        normalized[idx] = _travel_workout_day(idx + 1)

    # Gradual compensation after travel: avoid sudden spike.
    ramp_day_indices = [td, td + 1, td + 2]
    ramp_minutes = [20, 30, 40]
    for offset, day_idx in enumerate(ramp_day_indices):
        if day_idx >= len(normalized):
            break
        day = _ensure_workout_day_shape(normalized[day_idx], day_idx + 1)
        day["tip"] = (
            f"Compensation ramp day {offset + 1}: target about {ramp_minutes[offset]} minutes, keep form strict, no overtraining."
        )
        normalized[day_idx] = day

    return {"plan": normalized}


def _build_travel_compensation_nutrition_plan(base_plan: List[Dict[str, Any]], travel_days: int) -> Dict[str, Any]:
    if not isinstance(base_plan, list):
        base_plan = []

    normalized = [_ensure_nutrition_day_shape(day, idx + 1) for idx, day in enumerate(base_plan[:7])]
    while len(normalized) < 7:
        normalized.append(_ensure_nutrition_day_shape({}, len(normalized) + 1))

    td = max(1, min(6, travel_days))

    for idx in range(td):
        day_number = idx + 1
        normalized[idx] = {
            "day": f"Day {day_number}",
            "breakfast": ["Portable protein option (curd/eggs/protein milk)", "One fruit"],
            "lunch": ["Simple thali: dal + sabzi + controlled rice/roti"],
            "snacks": ["Roasted chana / nuts", "Buttermilk or water"],
            "dinner": ["Lean protein + vegetables", "Keep dinner lighter than lunch"],
            "tip": "Travel phase: prioritize protein, hydration, and portion control.",
        }

    for day_idx in [td, td + 1, td + 2]:
        if day_idx >= len(normalized):
            break
        normalized[day_idx]["tip"] = "Compensation phase: maintain high protein and avoid sugar-heavy snacks."

    return {"plan": normalized}


def _normalize_workout_plan(plan: Any) -> List[Dict[str, Any]]:
    normalized = []
    if not isinstance(plan, list):
        plan = []

    for idx, day in enumerate(plan[:7], start=1):
        normalized.append(_ensure_workout_day_shape(day, idx))

    while len(normalized) < 7:
        normalized.append(_ensure_workout_day_shape({}, len(normalized) + 1))

    return normalized


def _normalize_nutrition_plan(plan: Any) -> List[Dict[str, Any]]:
    normalized = []
    if not isinstance(plan, list):
        plan = []

    for idx, day in enumerate(plan[:7], start=1):
        normalized.append(_ensure_nutrition_day_shape(day, idx))

    while len(normalized) < 7:
        normalized.append(_ensure_nutrition_day_shape({}, len(normalized) + 1))

    return normalized


def _adaptive_travel_compensation_with_ai(
    goal: str,
    message: str,
    travel_days: int,
    base_workout_plan: List[Dict[str, Any]],
    base_nutrition_plan: List[Dict[str, Any]],
) -> Dict[str, Any] | None:
    td = max(1, min(6, int(travel_days)))

    payload = {
        "goal": goal,
        "message": message,
        "travel_days": td,
        "base_workout": _normalize_workout_plan(base_workout_plan),
        "base_nutrition": _normalize_nutrition_plan(base_nutrition_plan),
    }

    prompt = f"""
You are a fitness and nutrition planner.
User cannot do workouts for exactly {td} days due to travel.
Restructure plans dynamically, not generic.

Rules:
- Keep a 7-day output.
- Days 1..{td}: no structured workouts.
- After day {td}, create compensation progression based on remaining days.
- Update relevant sections in-place: warmup/exercises/cooldown/tip and breakfast/lunch/snacks/dinner/tip.
- Nutrition during travel should reduce damage (hydration, protein priority, portion control).
- Compensation should be safe, not extreme.

Input JSON:
{json.dumps(payload, default=str)}

Return STRICT JSON only:
{{
  "workout": {{ "plan": [{{"day":"Day 1","warmup":[],"exercises":[],"cooldown":[],"tip":""}}] }},
  "nutrition": {{ "plan": [{{"day":"Day 1","breakfast":[],"lunch":[],"snacks":[],"dinner":[],"tip":""}}] }}
}}
"""

    try:
        raw = generate_ai_response(prompt)
        parsed = _safe_json_loads(raw)
        workout = _normalize_workout_plan((parsed.get("workout") or {}).get("plan"))
        nutrition = _normalize_nutrition_plan((parsed.get("nutrition") or {}).get("plan"))

        for idx in range(td):
            # Enforce no structured workouts during declared travel window.
            workout[idx]["exercises"] = []
            workout[idx]["warmup"] = ["Travel day: mobility optional, no formal workout"]
            workout[idx]["cooldown"] = ["5 min deep breathing before sleep"]

            # Enforce travel-practical meals for the same window.
            day_number = idx + 1
            nutrition[idx] = {
                "day": f"Day {day_number}",
                "breakfast": ["Portable protein option (curd/eggs/protein milk)", "One fruit"],
                "lunch": ["Simple thali: dal + sabzi + controlled rice/roti"],
                "snacks": ["Roasted chana / nuts", "Buttermilk or water"],
                "dinner": ["Lean protein + vegetables", "Keep dinner lighter than lunch"],
                "tip": "Travel phase: prioritize hydration and protein; perfection is not required.",
            }

        return {
            "workout": {"plan": workout},
            "nutrition": {"plan": nutrition},
        }
    except Exception:
        return None


def _extract_structured_logs_with_ai(message: str) -> Dict[str, Any]:
    text = (message or "").strip()
    if not text:
        return {}

    prompt = f"""
You are a fitness log parser.
Extract structured daily log fields from the user message.

Message: {text}

Return STRICT JSON only:
{{
  "weight_kg": number or null,
  "workout_minutes": number or null,
    "workout_completed": boolean,
        "workout_day_number": number or null,
    "missed_workout_items": ["..."],
  "meal_text": string or "",
        "meal_logged": boolean,
    "adherence_status": "good" | "poor" | "neutral",
    "travel_disruption": boolean,
    "travel_days": number or null,
    "compensation_request": boolean
}}
"""

    try:
        raw = generate_ai_response(prompt)
        payload = _safe_json_loads(raw)
        cleaned: Dict[str, Any] = {}

        weight = payload.get("weight_kg")
        if isinstance(weight, (int, float)):
            cleaned["weight_kg"] = float(weight)

        workout = payload.get("workout_minutes")
        if isinstance(workout, (int, float)):
            cleaned["workout_minutes"] = int(workout)

        workout_completed = payload.get("workout_completed")
        if isinstance(workout_completed, bool):
            cleaned["workout_completed"] = workout_completed

        workout_day_number = payload.get("workout_day_number")
        if isinstance(workout_day_number, (int, float)) and int(workout_day_number) > 0:
            cleaned["workout_day_number"] = int(workout_day_number)

        missed_workout_items = payload.get("missed_workout_items")
        if isinstance(missed_workout_items, list):
            cleaned["missed_workout_items"] = [str(x).strip() for x in missed_workout_items if str(x).strip()][:5]

        meal_text = payload.get("meal_text")
        if isinstance(meal_text, str) and meal_text.strip():
            cleaned["meal_text"] = meal_text.strip()

        meal_logged = payload.get("meal_logged")
        if isinstance(meal_logged, bool):
            cleaned["meal_logged"] = meal_logged

        adherence_status = payload.get("adherence_status")
        if adherence_status in {"good", "poor", "neutral"}:
            cleaned["adherence_status"] = adherence_status
            if adherence_status == "good" and "workout_completed" not in cleaned:
                cleaned["workout_completed"] = True
            if adherence_status == "good" and "meal_logged" not in cleaned:
                cleaned["meal_logged"] = True

        travel_disruption = payload.get("travel_disruption")
        if isinstance(travel_disruption, bool):
            cleaned["travel_disruption"] = travel_disruption

        travel_days = payload.get("travel_days")
        if isinstance(travel_days, (int, float)) and int(travel_days) > 0:
            cleaned["travel_days"] = max(1, min(14, int(travel_days)))

        compensation_request = payload.get("compensation_request")
        if isinstance(compensation_request, bool):
            cleaned["compensation_request"] = compensation_request

        if cleaned:
            return cleaned
    except Exception:
        pass

    return _extract_structured_logs(text)


def _llm_agent_brain(
    goal: str,
    mode: str,
    message: str,
    observed: Dict[str, Any],
    recent_logs: List[Dict[str, Any]],
    latest_agent_event: Dict[str, Any],
    drift: Dict[str, Any],
    recovery_mode: Dict[str, Any],
    has_existing_plans: bool,
    autonomous: bool,
) -> Dict[str, Any]:
    context_payload = {
        "goal": goal,
        "mode": mode,
        "message": message,
        "observed": observed,
        "recent_logs": recent_logs[:5],
        "latest_agent_event": latest_agent_event,
        "drift": drift,
        "recovery_mode": recovery_mode,
        "has_existing_plans": has_existing_plans,
        "autonomous": autonomous,
    }

    prompt = f"""
You are the AI orchestration brain for a wellness coach.
Decide how the plan should adapt and what actions should happen now.
Infer intent from semantics, not exact phrase matching.
If the user sends short confirmations like "finished", "done", or "completed",
and latest_agent_event indicates a day plan was just sent, set completion_update=true.

Context JSON:
{json.dumps(context_payload, default=str)}

Return STRICT JSON only with these keys:
{{
  "adherence_signal": "good" | "neutral" | "poor",
    "today_plan_query": boolean,
    "completion_update": boolean,
    "log_summary_query": boolean,
    "travel_disruption": boolean,
    "compensation_requested": boolean,
  "should_refresh_plan": boolean,
  "weekly_reflection_requested": boolean,
  "user_requests_restructure": boolean,
  "needs_food_adaptation": boolean,
  "nudges": ["..."],
  "action_hints": ["..."]
}}
"""

    try:
        raw = generate_ai_response(prompt)
        payload = _safe_json_loads(raw)
    except Exception:
        payload = {}

    adherence = payload.get("adherence_signal")
    if adherence not in {"good", "neutral", "poor"}:
        adherence = "neutral"

    nudges = payload.get("nudges")
    if not isinstance(nudges, list):
        nudges = []
    nudges = [str(x).strip() for x in nudges if str(x).strip()][:3]

    action_hints = payload.get("action_hints")
    if not isinstance(action_hints, list):
        action_hints = []
    action_hints = [str(x).strip() for x in action_hints if str(x).strip()][:4]

    return {
        "adherence_signal": adherence,
        "today_plan_query": _safe_bool(payload.get("today_plan_query"), default=False),
        "completion_update": _safe_bool(payload.get("completion_update"), default=False),
        "log_summary_query": _safe_bool(payload.get("log_summary_query"), default=False),
        "travel_disruption": _safe_bool(payload.get("travel_disruption"), default=False),
        "compensation_requested": _safe_bool(payload.get("compensation_requested"), default=False),
        "should_refresh_plan": _safe_bool(payload.get("should_refresh_plan"), default=False),
        "weekly_reflection_requested": _safe_bool(payload.get("weekly_reflection_requested"), default=False),
        "user_requests_restructure": _safe_bool(payload.get("user_requests_restructure"), default=False),
        "needs_food_adaptation": _safe_bool(payload.get("needs_food_adaptation"), default=False),
        "nudges": nudges,
        "action_hints": action_hints,
    }


def _llm_finalize_response(
    message: str,
    user_intent: Dict[str, bool],
    decision: Dict[str, Any],
    actions: List[str],
    nudges: List[str],
    plan_updates: Dict[str, Any],
    current_plans: Dict[str, Any],
    progress_summary: Dict[str, Any],
    weekly_reflection: Dict[str, Any],
    recent_logs: List[Dict[str, Any]],
    structured_logs: Dict[str, Any],
) -> Dict[str, Any]:
    workout_source = (plan_updates.get("workout") or {}).get("plan") or (current_plans.get("workout") or {}).get("plan") or []
    nutrition_source = (plan_updates.get("nutrition") or {}).get("plan") or (current_plans.get("nutrition") or {}).get("plan") or []

    workout_preview = workout_source[:2] if isinstance(workout_source, list) else []
    nutrition_preview = nutrition_source[:2] if isinstance(nutrition_source, list) else []

    context = {
        "user_intent": user_intent,
        "decision": decision,
        "actions": actions,
        "nudges": nudges,
        "plan_update_keys": list(plan_updates.keys()),
        "progress_summary": progress_summary,
        "workout_preview": workout_preview,
        "nutrition_preview": nutrition_preview,
        "weekly_reflection": weekly_reflection,
        "structured_logs": structured_logs,
        "recent_logs": recent_logs[:3],
    }

    prompt = f"""
You are Lifeline Coach.
Create a user-facing coaching response that feels conversational and actionable.
Do not mention raw internals unless useful.
If user asks what to do today, provide a practical today plan with clear workout + meals + one priority habit.
If completion_update is true, acknowledge completion and give recovery/next-step guidance.
If log_summary_query is true, clearly list what has been tracked so far from available logs.
Do not repeat the same 'today plan' response for completion updates.
Keep it concise but concrete.

User message: {message or "(no direct message)"}
Context JSON: {json.dumps(context, default=str)}

Return STRICT JSON only:
{{
  "summary": "single short sentence",
  "ai_reply": "multi-line coaching response"
}}
"""

    def _has_today_plan_shape(text: str) -> bool:
        lowered = (text or "").lower()
        has_workout = "workout" in lowered
        has_meal = any(token in lowered for token in ["meal", "breakfast", "lunch", "dinner"])
        return has_workout and has_meal

    def _build_grounded_plan_text(header: str = "Here is your plan:") -> str:
        lines: List[str] = [header]
        travel_days = int(decision.get("travel_days") or 0)
        show_post_travel_preview = bool(user_intent.get("post_travel_query", False))
        meal_on_track_workout_missed = bool(decision.get("meal_on_track_workout_missed", False))
        completed_workout_days = int(progress_summary.get("total_workout_days", 0))
        cycle_day_number = (completed_workout_days % 7) + 1
        cycle_week_number = (completed_workout_days // 7) + 1
        cycle_day_idx = cycle_day_number - 1
        target_label = f"Week {cycle_week_number} Day {cycle_day_number}"

        if meal_on_track_workout_missed:
            lines.append(
                "You followed nutrition but missed workout, so I adjusted tomorrow with a safe +10 minute workout compensation and recovery-aware meal guidance."
            )

        lines.append(f"Today Target: {target_label}")

        if travel_days > 0:
            lines.append(
                f"Travel window (next {travel_days} day(s)): no structured workouts. Focus on sleep, hydration, and meal consistency."
            )

            travel_meal_day = (nutrition_source[0] if nutrition_source else {}) if isinstance(nutrition_source, list) else {}
            if isinstance(travel_meal_day, dict):
                breakfast = travel_meal_day.get("breakfast") if isinstance(travel_meal_day.get("breakfast"), list) else []
                lunch = travel_meal_day.get("lunch") if isinstance(travel_meal_day.get("lunch"), list) else []
                snacks = travel_meal_day.get("snacks") if isinstance(travel_meal_day.get("snacks"), list) else []
                dinner = travel_meal_day.get("dinner") if isinstance(travel_meal_day.get("dinner"), list) else []
                lines.append("Meals (Travel phase):")
                if breakfast:
                    lines.append(f"- Breakfast: {breakfast[0]}")
                if lunch:
                    lines.append(f"- Lunch: {lunch[0]}")
                if snacks:
                    lines.append(f"- Snacks: {snacks[0]}")
                if dinner:
                    lines.append(f"- Dinner: {dinner[0]}")

            if not show_post_travel_preview:
                if nudges:
                    lines.append(f"Priority habit: {nudges[0]}")
                lines.append("Send an evening check-in and I will adapt tomorrow's plan if needed.")
                return "\n".join(lines)

        if workout_preview:
            day_idx = cycle_day_idx
            if travel_days > 0 and len(workout_source) > travel_days:
                day_idx = travel_days
            if day_idx >= len(workout_source):
                day_idx = 0

            day = (workout_source[day_idx] if day_idx < len(workout_source) else workout_preview[0]) or {}
            day_name = target_label
            source_day = str(day.get("day", f"Day {day_idx + 1}"))
            warmup = day.get("warmup") if isinstance(day.get("warmup"), list) else []
            exercises = day.get("exercises") if isinstance(day.get("exercises"), list) else []
            cooldown = day.get("cooldown") if isinstance(day.get("cooldown"), list) else []

            label = "Post-travel workout preview" if travel_days > 0 else "Workout"
            lines.append(f"{label} ({day_name} | {source_day}):")
            if travel_days > 0:
                lines.append("- During travel days: no structured workout required.")
            if warmup:
                lines.append(f"- Warm-up: {', '.join([str(x) for x in warmup[:2]])}")
            if exercises:
                for ex in exercises[:4]:
                    if isinstance(ex, dict):
                        name = str(ex.get("name", "exercise")).strip()
                        sets = ex.get("sets", "-")
                        reps = ex.get("reps", "-")
                        rest = ex.get("rest", "-")
                        lines.append(f"- {name}: {sets} sets x {reps} reps (rest {rest})")
            if cooldown:
                lines.append(f"- Cool-down: {', '.join([str(x) for x in cooldown[:2]])}")

        if nutrition_preview:
            day_idx = cycle_day_idx if travel_days <= 0 else min(travel_days, max(len(nutrition_source) - 1, 0))
            if day_idx >= len(nutrition_source):
                day_idx = 0
            day = (nutrition_source[day_idx] if day_idx < len(nutrition_source) else nutrition_preview[0]) or {}
            day_name = target_label
            source_day = str(day.get("day", f"Day {day_idx + 1}"))
            breakfast = day.get("breakfast") if isinstance(day.get("breakfast"), list) else []
            lunch = day.get("lunch") if isinstance(day.get("lunch"), list) else []
            snacks = day.get("snacks") if isinstance(day.get("snacks"), list) else []
            dinner = day.get("dinner") if isinstance(day.get("dinner"), list) else []

            label = "Meals (Compensation phase)" if travel_days > 0 else f"Meals ({day_name} | {source_day})"
            lines.append(f"{label}:")
            if breakfast:
                lines.append(f"- Breakfast: {breakfast[0]}")
            if lunch:
                lines.append(f"- Lunch: {lunch[0]}")
            if snacks:
                lines.append(f"- Snacks: {snacks[0]}")
            if dinner:
                lines.append(f"- Dinner: {dinner[0]}")

        if nudges:
            lines.append(f"Priority habit: {nudges[0]}")

        lines.append("Send an evening check-in and I will adapt tomorrow's plan if needed.")
        return "\n".join(lines)

    # Only force plan-shaped responses when user intent is explicitly plan-related.
    # Background plan refreshes should not hijack normal conversational replies.
    should_force_plan_format = bool(
        user_intent.get("today_plan_query")
        or user_intent.get("post_travel_query")
        or bool(decision.get("user_requests_restructure", False))
        or bool(decision.get("resume_training_query", False))
    )

    if user_intent.get("cravings_query"):
        swaps_payload = plan_updates.get("craving_swaps") if isinstance(plan_updates.get("craving_swaps"), dict) else {}
        swaps = swaps_payload.get("swaps") if isinstance(swaps_payload.get("swaps"), list) else []
        rule = str(swaps_payload.get("rule", "Use a cleaner swap first, then decide after 10 minutes.")).strip()
        fallback_snack = str(swaps_payload.get("fallback_snack", "Fruit + nuts")).strip()

        lines = [
            "Love this question. Here are tasty options so you can stay on plan without feeling restricted:",
        ]
        for item in swaps[:5]:
            if not isinstance(item, dict):
                continue
            craving = str(item.get("craving", "Junk craving")).strip()
            better = str(item.get("better_option", "Protein + fiber snack")).strip()
            tip = str(item.get("portion_tip", "Keep portions controlled.")).strip()
            lines.append(f"- {craving} -> {better} ({tip})")

        lines.append(f"Rule: {rule}")
        lines.append(f"Emergency fallback: {fallback_snack}")

        return {
            "summary": "Shared tasty plan-friendly alternatives for junk cravings.",
            "ai_reply": "\n".join(lines),
            "meta": {
                "fallback_used": False,
                "fallback_reason": None,
            },
        }

    # Deterministic summary path for progress-count questions.
    if user_intent.get("log_summary_query"):
        workout_days = int(progress_summary.get("total_workout_days", _count_workout_days(recent_logs)))
        meal_days = int(progress_summary.get("total_meal_log_days", _count_meal_logged_days(recent_logs)))
        total_logs = int(progress_summary.get("total_daily_logs", len(recent_logs)))
        total_minutes = int(progress_summary.get("total_workout_minutes", 0))
        recent_history = progress_summary.get("recent_workout_history") if isinstance(progress_summary.get("recent_workout_history"), list) else []
        travel_days = int(decision.get("travel_days") or 0)

        lines = [f"Workout days completed so far: {workout_days}"]
        lines.append(f"Meal-log days so far: {meal_days}")
        lines.append(f"Total daily logs stored: {total_logs}")
        lines.append(f"Total workout minutes logged: {total_minutes}")
        if recent_history:
            last_entries = recent_history[:3]
            formatted = ", ".join([f"{x.get('date')}: {x.get('workout_minutes', 0)} min" for x in last_entries])
            lines.append(f"Recent workout history: {formatted}")
        if travel_days > 0:
            lines.append(f"Active travel window detected: {travel_days} day(s) remaining in current travel context.")
        lines.append("Keep sending daily updates and I will keep this count accurate.")

        return {
            "summary": f"Logged workout count: {workout_days} day(s).",
            "ai_reply": "\n".join(lines),
            "meta": {
                "fallback_used": False,
                "fallback_reason": None,
            },
        }

    def _apply_plan_format_if_needed(summary: str, ai_reply: str) -> Dict[str, str]:
        if not should_force_plan_format:
            return {"summary": summary, "ai_reply": ai_reply}

        header = "Here is your updated plan:" if (plan_updates.get("workout") is not None or plan_updates.get("nutrition") is not None) else "Here is your plan for today:"
        formatted = _build_grounded_plan_text(header=header)
        resolved_summary = summary or "Shared your structured plan."
        return {"summary": resolved_summary, "ai_reply": formatted}

    def _normalize_payload(payload: Dict[str, Any], default_summary: str) -> Dict[str, str]:
        summary = str(payload.get("summary", "")).strip()
        ai_reply = str(payload.get("ai_reply", "")).strip()
        if not ai_reply and isinstance(payload.get("message"), str):
            ai_reply = str(payload.get("message")).strip()
        if not summary:
            summary = default_summary
        return {"summary": summary, "ai_reply": ai_reply}

    default_summary = _summarize_response(decision, nudges, plan_updates)

    try:
        raw = generate_ai_response(prompt)
        payload = _safe_json_loads(raw)
        parsed = _normalize_payload(payload, default_summary)

        # Repair pass 1: weak or malformed answer.
        if len(parsed["ai_reply"]) < 16:
            repair_prompt = f"""
Rewrite the response using STRICT JSON only:
{{
  "summary": "single short sentence",
  "ai_reply": "clear actionable coaching response"
}}

Original context:
{json.dumps(context, default=str)}

Previous response:
{json.dumps(payload, default=str)}
"""
            repair_raw = generate_ai_response(repair_prompt)
            repair_payload = _safe_json_loads(repair_raw)
            parsed = _normalize_payload(repair_payload, default_summary)

        # Repair pass 2: plan-specific quality when asked for today's plan.
        if user_intent.get("today_plan_query") and not _has_today_plan_shape(parsed["ai_reply"]):
            plan_repair_prompt = f"""
User asked for today's plan. Rewrite with concrete, day-specific plan details.
Return STRICT JSON only:
{{
  "summary": "single short sentence",
  "ai_reply": "must include workout and meal guidance for today"
}}

Context:
{json.dumps(context, default=str)}

Previous response:
{json.dumps(parsed, default=str)}
"""
            plan_repair_raw = generate_ai_response(plan_repair_prompt)
            plan_repair_payload = _safe_json_loads(plan_repair_raw)
            parsed = _normalize_payload(plan_repair_payload, default_summary)

        if user_intent.get("completion_update") and "here is your plan for today" in parsed["ai_reply"].lower():
            raise ValueError("completion response repeated plan template")

        if user_intent.get("today_plan_query") and not _has_today_plan_shape(parsed["ai_reply"]):
            parsed["ai_reply"] = _build_grounded_plan_text(header="Here is your plan for today:")
            if not parsed["summary"]:
                parsed["summary"] = "Shared today's actionable plan."

        parsed = _apply_plan_format_if_needed(parsed["summary"], parsed["ai_reply"])

        if len(parsed["ai_reply"]) >= 16:
            return {
                "summary": parsed["summary"],
                "ai_reply": parsed["ai_reply"],
                "meta": {
                    "fallback_used": False,
                    "fallback_reason": None,
                },
            }
    except Exception as exc:
        # Recovery pass: ask for plain conversational response (no strict JSON).
        try:
            recovery_prompt = f"""
You are Lifeline Coach. Respond in plain text (not JSON).
Give a concise, actionable response to the user using the provided context.

User message: {message or "(no direct message)"}
Context: {json.dumps(context, default=str)}
"""
            recovery_text = (generate_ai_text_response(recovery_prompt) or "").strip()
            if recovery_text:
                if user_intent.get("today_plan_query") and not _has_today_plan_shape(recovery_text):
                    recovery_text = _build_grounded_plan_text(header="Here is your plan for today:")

                coerced = _apply_plan_format_if_needed(default_summary, recovery_text)
                return {
                    "summary": coerced["summary"],
                    "ai_reply": coerced["ai_reply"],
                    "meta": {
                        "fallback_used": False,
                        "fallback_reason": f"json-path-failed:{exc}",
                    },
                }
        except Exception:
            pass

        return {
            "summary": default_summary,
            "ai_reply": "I could not generate a reliable response just now. Please resend your last message and I will respond with a personalized update.",
            "meta": {
                "fallback_used": True,
                "fallback_reason": str(exc),
            },
        }


def _food_reality_adapter(goal: str, message: str) -> Dict[str, Any]:
    lowered = message.lower()
    trigger = "only have"
    if trigger not in lowered:
        return {}

    foods = message[lowered.index(trigger) + len(trigger) :].strip(" :.-")
    if not foods:
        return {}

    prompt = f"""
You are a practical Indian nutrition coach.
User goal: {goal}
Available foods right now: {foods}

Return STRICT JSON only:
{{
  "meal_now": ["..."],
  "rest_of_day": ["..."],
  "macro_note": "..."
}}
"""

    try:
        raw = generate_ai_response(prompt)
        return _safe_json_loads(raw)
    except Exception:
        return {
            "meal_now": [f"Use available foods: {foods}"],
            "rest_of_day": ["Balance with protein and vegetables in the next meals."],
            "macro_note": "Keep portions moderate and add protein at next opportunity.",
        }


def _compute_drift(goal: str, logs: List[Dict[str, Any]], baseline_weight: float | None = None) -> Dict[str, Any]:
    weights = [x.get("weight_kg") for x in logs if isinstance(x.get("weight_kg"), (int, float))]
    weights = [float(w) for w in weights]

    result = {
        "status": "unknown",
        "reason": "Not enough weight logs",
        "expected_weekly_delta": 0.0,
        "actual_delta": 0.0,
        "should_adapt_plan": False,
    }

    if len(weights) < 2 and isinstance(baseline_weight, (int, float)) and weights:
        # Use health-form weight as starting point until enough daily logs exist.
        weights = [weights[0], float(baseline_weight)]

    if len(weights) < 2:
        return result

    latest = weights[0]
    oldest = weights[-1]
    actual_delta = round(latest - oldest, 2)

    goal_l = (goal or "").lower()
    expected = -0.3 if "loss" in goal_l else 0.25 if any(x in goal_l for x in ["gain", "bulk"]) else 0.0

    should_adapt = False
    status = "on_track"
    reason = "Progress looks aligned"

    if expected < 0 and actual_delta >= -0.1:
        should_adapt = True
        status = "behind"
        reason = "Weight-loss trend is slower than expected"
    elif expected > 0 and actual_delta <= 0.1:
        should_adapt = True
        status = "behind"
        reason = "Weight-gain trend is slower than expected"

    result.update(
        {
            "status": status,
            "reason": reason,
            "expected_weekly_delta": expected,
            "actual_delta": actual_delta,
            "should_adapt_plan": should_adapt,
        }
    )
    return result


def _build_nudges(logs: List[Dict[str, Any]], autonomous: bool) -> List[str]:
    nudges: List[str] = []
    if not logs:
        return ["Log one meal and one activity today so I can adapt your plan."]

    latest = logs[0]
    workout_minutes = int(latest.get("workout_minutes", 0) or 0)
    meal_text = (latest.get("meal_text") or "").strip()
    adherence_status = str(latest.get("adherence_status", "")).lower()
    workout_completed = bool(latest.get("workout_completed", False))

    if adherence_status == "good" or workout_completed:
        nudges.append("Solid execution today. Focus on recovery and sleep quality tonight.")
        return nudges[:3]

    if workout_minutes == 0:
        nudges.append("No workout logged today. Try a 15-minute walk now.")
    if not meal_text:
        nudges.append("No meal log found today. Share what you ate to keep your plan accurate.")

    if autonomous and nudges:
        nudges.append("Autonomous mode active: I can auto-adjust tomorrow's plan if this pattern continues.")

    return nudges[:3]


def _resolve_allotted_workout_minutes(profile: Dict[str, Any]) -> int:
    planned = profile.get("exercise")
    if isinstance(planned, (int, float)) and int(planned) > 0:
        return int(planned)
    return 30


def _is_travel_resume_message(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False

    resume_tokens = [
        "travel done",
        "travelling done",
        "traveling done",
        "travelling is done",
        "traveling is done",
        "travel is done",
        "done with travelling",
        "done with traveling",
        "done travelling",
        "done traveling",
        "travel is over",
        "travelling is over",
        "traveling is over",
        "trip is over",
        "back from travel",
        "back from trip",
        "returned from travel",
        "returned from trip",
        "can start from today",
        "starting from today",
        "start from today",
        "resume workout",
        "resume training",
    ]
    return any(token in text for token in resume_tokens)


def _is_completion_message(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False

    if _is_travel_resume_message(text):
        return False

    completion_tokens = [
        "finished",
        "done",
        "completed",
        "wrapped up",
        "over",
    ]
    context_tokens = [
        "workout",
        "work",
        "plan",
        "tasks",
    ]

    return any(t in text for t in completion_tokens) and any(t in text for t in context_tokens)


def _weekly_reflection(goal: str, logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not logs:
        return {
            "summary": "No weekly logs available yet.",
            "problems": ["Missing logs"],
            "strategy_update": "Start with one daily check-in message.",
        }

    days_logged = len(logs)
    workout_days = sum(1 for x in logs if int(x.get("workout_minutes", 0) or 0) > 0)
    meal_days = sum(1 for x in logs if bool((x.get("meal_text") or "").strip()))

    adherence = round((workout_days + meal_days) / max(days_logged * 2, 1), 2)

    problems: List[str] = []
    if workout_days < max(3, days_logged // 2):
        problems.append("Workout consistency is low")
    if meal_days < max(3, days_logged // 2):
        problems.append("Nutrition logging is inconsistent")

    if not problems:
        problems.append("No major blockers detected")

    strategy = "Increase difficulty slightly next week" if adherence >= 0.7 else "Reduce plan friction and focus on easier daily actions"

    return {
        "summary": f"Weekly adherence for goal '{goal}' is {int(adherence * 100)}%.",
        "problems": problems,
        "strategy_update": strategy,
    }


def _compute_recovery_mode(logs: List[Dict[str, Any]], window: int = 3, threshold: int = 2) -> Dict[str, Any]:
    sample = logs[:window]
    missed_days = sum(1 for item in sample if int(item.get("workout_minutes", -1)) == 0)
    recovery_mode = missed_days >= threshold
    reason = (
        f"Detected {missed_days} missed workout day(s) in last {len(sample)} logs"
        if recovery_mode
        else "No repeated missed-workout pattern"
    )

    return {
        "enabled": recovery_mode,
        "missed_days": missed_days,
        "window": len(sample),
        "threshold": threshold,
        "reason": reason,
    }


def _count_workout_days(logs: List[Dict[str, Any]]) -> int:
    count = 0
    for item in logs:
        minutes = item.get("workout_minutes", 0)
        completed = item.get("workout_completed", False)
        if bool(completed) or (isinstance(minutes, (int, float)) and int(minutes) > 0):
            count += 1
    return count


def _count_meal_logged_days(logs: List[Dict[str, Any]]) -> int:
    count = 0
    for item in logs:
        meal = (item.get("meal_text") or "").strip()
        meal_logged = bool(item.get("meal_logged", False))
        if meal or meal_logged:
            count += 1
    return count


def _infer_active_travel_days(
    recent_logs: List[Dict[str, Any]],
    latest_agent_event: Dict[str, Any],
    explicit_travel_days: int,
) -> int:
    if explicit_travel_days > 0:
        return explicit_travel_days

    if recent_logs and bool(recent_logs[0].get("travel_window_closed", False)):
        return 0

    today = _utc_now().date()

    # Prefer structured daily logs with travel_days + date.
    dated_candidates: List[tuple[date, int]] = []
    for item in recent_logs:
        raw_date = item.get("date")
        td = item.get("travel_days")
        if not isinstance(raw_date, str) or not isinstance(td, int) or td <= 0:
            continue
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except Exception:
            continue
        dated_candidates.append((d, td))

    if dated_candidates:
        start_date, total = max(dated_candidates, key=lambda x: x[0])
        elapsed = max(0, (today - start_date).days)
        remaining = max(0, total - elapsed)
        if remaining > 0:
            return remaining

    # Fallback: latest agent decision context.
    decision = latest_agent_event.get("decision") if isinstance(latest_agent_event, dict) else {}
    if isinstance(decision, dict):
        td = decision.get("travel_days")
        travel_flag = bool(decision.get("travel_disruption", False))
        if travel_flag and isinstance(td, int) and td > 0:
            return min(14, td)

    return 0


def _infer_intent_overrides(message: str) -> Dict[str, bool]:
    text = (message or "").strip().lower()
    if not text:
        return {
            "today_plan_query": False,
            "log_summary_query": False,
            "post_travel_query": False,
            "resume_training_query": False,
            "cravings_query": False,
        }

    # Normalize common stretch typos like "todayy" -> "today".
    normalized_text = re.sub(r"\btoday+\b", "today", text)

    today_plan_query = any(
        token in normalized_text
        for token in [
            "plan for today",
            "what's the plan for today",
            "whats the plan for today",
            "today's plan",
            "todays plan",
            "what should i do today",
            "what do i do today",
            "what to do today",
        ]
    )

    # Fallback regex for casual variants like "what to do todayy".
    if not today_plan_query:
        today_plan_query = bool(
            re.search(r"\b(?:what|whats|what's)?\s*(?:to\s+)?do\s+today\b", normalized_text)
            or re.search(r"\b(?:plan|routine|schedule)\s+(?:for\s+)?today\b", normalized_text)
        )

    plan_update_status_query = any(
        token in normalized_text
        for token in [
            "did you update the plan",
            "have you updated the plan",
            "is the plan updated",
            "is plan updated",
            "plan updated",
            "updated plan",
            "did you update my plan",
            "have you updated my plan",
        ]
    )

    if not plan_update_status_query:
        plan_update_status_query = bool(
            re.search(r"\b(?:did|have)\s+you\s+update(?:d)?\s+(?:my\s+)?plan\b", normalized_text)
            or re.search(r"\b(?:is\s+)?(?:my\s+)?plan\s+updated\b", normalized_text)
        )

    if plan_update_status_query:
        today_plan_query = True

    workout_count_query = any(token in normalized_text for token in ["how many days", "give me the count", "count of days", "days i've done", "days i have done"]) and any(
        token in normalized_text for token in ["workout", "work out", "exercise", "trained"]
    )

    progress_summary_query = any(
        token in normalized_text
        for token in [
            "what's my progress",
            "whats my progress",
            "my progress so far",
            "progress so far",
            "show progress",
            "how is my progress",
        ]
    )

    # Today-plan intent should always take priority over summary intent.
    if today_plan_query:
        workout_count_query = False
        progress_summary_query = False

    post_travel_query = any(
        token in normalized_text
        for token in [
            "after travel",
            "post travel",
            "after my trip",
            "once i return",
            "when i return",
            "what after travel",
            "plan after travel",
        ]
    )

    cravings_query = any(
        token in normalized_text
        for token in [
            "junk food",
            "craving",
            "feel like eating",
            "tempted",
            "snack recommendation",
            "recommend something tasty",
        ]
    )

    resume_training_query = _is_travel_resume_message(normalized_text)
    if resume_training_query:
        today_plan_query = True
        workout_count_query = False
        progress_summary_query = False

    return {
        "today_plan_query": today_plan_query,
        "log_summary_query": workout_count_query or progress_summary_query,
        "post_travel_query": post_travel_query,
        "resume_training_query": resume_training_query,
        "cravings_query": cravings_query,
    }


def _craving_swap_recommendations(goal: str, message: str, travel_days: int) -> Dict[str, Any]:
    prompt = f"""
You are a practical nutrition coach.
User goal: {goal}
User message: {message}
Active travel days remaining: {travel_days}

Give tasty alternatives to junk cravings without deviating from plan.
Focus on easy Indian options.

Return STRICT JSON only:
{{
  "swaps": [
    {{"craving": "", "better_option": "", "portion_tip": ""}}
  ],
  "rule": "single simple rule",
  "fallback_snack": "one emergency snack"
}}
"""

    try:
        raw = generate_ai_response(prompt)
        payload = _safe_json_loads(raw)
        swaps = payload.get("swaps") if isinstance(payload.get("swaps"), list) else []
        cleaned_swaps: List[Dict[str, str]] = []
        for item in swaps[:5]:
            if not isinstance(item, dict):
                continue
            craving = str(item.get("craving", "")).strip()
            better = str(item.get("better_option", "")).strip()
            tip = str(item.get("portion_tip", "")).strip()
            if craving or better:
                cleaned_swaps.append(
                    {
                        "craving": craving or "Junk craving",
                        "better_option": better or "Protein + fiber snack",
                        "portion_tip": tip or "Keep portion controlled and eat slowly.",
                    }
                )

        return {
            "swaps": cleaned_swaps,
            "rule": str(payload.get("rule", "Use the 80/20 rule: satisfy craving with a cleaner swap first.")).strip(),
            "fallback_snack": str(payload.get("fallback_snack", "Roasted chana + buttermilk")).strip(),
        }
    except Exception:
        return {
            "swaps": [
                {
                    "craving": "Chips",
                    "better_option": "Roasted makhana with masala",
                    "portion_tip": "Use a small bowl (not the packet).",
                },
                {
                    "craving": "Chocolate",
                    "better_option": "2 squares dark chocolate + nuts",
                    "portion_tip": "Pair with water or tea, avoid second serving.",
                },
                {
                    "craving": "Burger/Pizza",
                    "better_option": "Paneer/egg wrap on roti with salad",
                    "portion_tip": "Limit sauces and avoid sugary drink.",
                },
            ],
            "rule": "Delay by 10 minutes, then choose the cleaner swap first.",
            "fallback_snack": "Fruit + peanut butter",
        }


def _summarize_response(decision: Dict[str, Any], nudges: List[str], plan_updates: Dict[str, Any]) -> str:
    parts = []
    travel_days = int(decision.get("travel_days") or 0)
    if travel_days > 0:
        parts.append(f"Travel-aware plan restructured for {travel_days} day(s)")

    if bool(decision.get("active_travel_window", False)):
        parts.append("Active travel window plan refresh applied")

    if bool(decision.get("meal_on_track_workout_missed", False)):
        parts.append("Meals on track but workout missed; compensation update prepared")

    if decision.get("recovery_mode", {}).get("enabled"):
        parts.append("Recovery mode activated for easier re-entry")

    if decision.get("drift", {}).get("status") == "behind":
        parts.append("Progress drift detected; adaptation prepared")
    else:
        parts.append("Progress is stable")

    if plan_updates:
        parts.append("Plan updates generated")

    if nudges:
        parts.append(f"{len(nudges)} nudge(s) prepared")

    return ". ".join(parts) + "."


def run_agent(request: AgentRequest) -> AgentResponse:
    trace: List[AgentStep] = []
    actions: List[str] = []

    conversation_id = _ensure_conversation(
        request.user_id,
        request.conversation_id,
        request.message or "New chat",
    )

    if (request.message or "").strip():
        _append_conversation_message(
            request.user_id,
            conversation_id,
            "user",
            (request.message or "").strip(),
        )

    profile = _fetch_user_profile(request.user_id)
    latest_health = _fetch_latest_health_record(request.user_id)

    if not profile and not latest_health:
        raise ValueError(
            "No user profile or health record found. Submit the health form once before using Coach."
        )
    merged_profile = {**profile, **latest_health}
    current_workout = _fetch_latest_plan(request.user_id, "workoutPlans")
    current_nutrition = _fetch_latest_plan(request.user_id, "nutritionPlans")
    latest_agent_event = _fetch_latest_agent_event(request.user_id)
    has_existing_plans = bool(current_workout) and bool(current_nutrition)

    observed = {
        "profile": {
            "age": merged_profile.get("age"),
            "goal": _resolve_goal(request.goal, merged_profile.get("goal"), request.message or ""),
            "sleep": merged_profile.get("sleep"),
            "exercise": merged_profile.get("exercise"),
            "diet": merged_profile.get("diet"),
            "activity": merged_profile.get("activity"),
        },
        "latest_health_record_found": bool(latest_health),
        "has_workout_plan": bool(current_workout),
        "has_nutrition_plan": bool(current_nutrition),
        "latest_agent_summary": latest_agent_event.get("summary"),
    }
    trace.append(AgentStep(name="observe", status="ok", detail="Loaded user state", output=observed))

    structured_logs = _extract_structured_logs_with_ai(request.message or "")

    progress_summary = _fetch_progress_summary(request.user_id)

    travel_resume_message = _is_travel_resume_message(request.message or "")
    if travel_resume_message:
        structured_logs["travel_window_closed"] = True
        structured_logs["travel_disruption"] = False
        structured_logs["travel_days"] = 0
        structured_logs["compensation_request"] = False
        structured_logs.pop("workout_completed", None)
        structured_logs.pop("workout_minutes", None)
        structured_logs.pop("workout_day_number", None)
        structured_logs.pop("meal_logged", None)

    # Deterministic guardrail: if user clearly reports completion, enforce
    # completion fields even if LLM parsing is weak.
    if _is_completion_message(request.message or ""):
        structured_logs["workout_completed"] = True
        structured_logs["meal_logged"] = True

        if not isinstance(structured_logs.get("workout_day_number"), int):
            prev_days = int(progress_summary.get("total_workout_days", 0))
            structured_logs["workout_day_number"] = prev_days + 1

    # If user confirms completion without explicit minutes, store planned minutes
    # so workout totals and history reflect the completed day.
    if structured_logs.get("workout_completed"):
        minutes = structured_logs.get("workout_minutes")
        if not isinstance(minutes, (int, float)) or int(minutes) <= 0:
            structured_logs["workout_minutes"] = _resolve_allotted_workout_minutes(merged_profile)

        # If multiple plan days are completed on the same calendar day,
        # accumulate minutes when day number progresses (e.g., day 1 -> day 2).
        existing_today = _fetch_daily_log(request.user_id)
        existing_minutes = existing_today.get("workout_minutes")
        existing_day_num = existing_today.get("workout_day_number")
        incoming_day_num = structured_logs.get("workout_day_number")

        if (
            isinstance(existing_minutes, (int, float))
            and int(existing_minutes) >= 0
            and isinstance(incoming_day_num, int)
            and incoming_day_num > 0
            and isinstance(existing_day_num, (int, float))
            and int(existing_day_num) > 0
            and incoming_day_num > int(existing_day_num)
        ):
            structured_logs["workout_minutes"] = int(existing_minutes) + int(structured_logs.get("workout_minutes", 0) or 0)

    if structured_logs:
        _upsert_daily_log(request.user_id, structured_logs)
        all_logs = _fetch_all_daily_logs(request.user_id)
        progress_summary = _refresh_progress_summary(request.user_id, logs=all_logs)
        actions.append("daily_log_updated")
        actions.append("progress_summary_updated")
        trace.append(
            AgentStep(
                name="auto_log",
                status="ok",
                detail="Parsed and stored chat-based logs",
                output=structured_logs,
            )
        )
    else:
        if not progress_summary:
            all_logs = _fetch_all_daily_logs(request.user_id)
            progress_summary = _refresh_progress_summary(request.user_id, logs=all_logs)
        trace.append(AgentStep(name="auto_log", status="skipped", detail="No structured logs detected", output={}))

    goal = _resolve_goal(request.goal, merged_profile.get("goal"), request.message or "")
    recent_logs = _fetch_recent_daily_logs(request.user_id, days=7)
    baseline_weight = merged_profile.get("weight")
    drift = _compute_drift(goal, recent_logs, baseline_weight=baseline_weight)
    recovery_mode = _compute_recovery_mode(recent_logs)

    llm_brain = _llm_agent_brain(
        goal=goal,
        mode=request.mode,
        message=request.message or "",
        observed=observed,
        recent_logs=recent_logs,
        latest_agent_event=latest_agent_event,
        drift=drift,
        recovery_mode=recovery_mode,
        has_existing_plans=has_existing_plans,
        autonomous=request.autonomous,
    )

    message_l = (request.message or "").lower()
    inferred_completion_from_logs = structured_logs.get("adherence_status") == "good"
    intent_overrides = _infer_intent_overrides(request.message or "")
    today_plan_query = bool(llm_brain.get("today_plan_query", False)) or intent_overrides["today_plan_query"]
    log_summary_query = bool(llm_brain.get("log_summary_query", False)) or intent_overrides["log_summary_query"]
    post_travel_query = bool(intent_overrides.get("post_travel_query", False))
    resume_training_query = bool(intent_overrides.get("resume_training_query", False))
    cravings_query = bool(intent_overrides.get("cravings_query", False))
    travel_resume_signal = bool(travel_resume_message or resume_training_query)

    # Guardrail: if user asks for today's plan, do not route to progress summary.
    if today_plan_query:
        log_summary_query = False

    travel_disruption = bool(llm_brain.get("travel_disruption", False)) or bool(structured_logs.get("travel_disruption", False))
    compensation_requested = bool(llm_brain.get("compensation_requested", False)) or bool(structured_logs.get("compensation_request", False))
    explicit_travel_days = _extract_travel_days(request.message or "")
    if isinstance(structured_logs.get("travel_days"), int) and structured_logs["travel_days"] > 0:
        explicit_travel_days = max(explicit_travel_days, int(structured_logs["travel_days"]))

    travel_days = _infer_active_travel_days(
        recent_logs=recent_logs,
        latest_agent_event=latest_agent_event,
        explicit_travel_days=explicit_travel_days,
    )
    if travel_resume_signal:
        travel_days = 0
        travel_disruption = False
        compensation_requested = False

    active_travel_window = travel_days > 0
    if travel_days > 0:
        travel_disruption = True
        # Keep compensation active through the travel window so forward plan
        # remains restructured and persisted each time the user checks in.
        compensation_requested = True
    completion_update = bool(llm_brain.get("completion_update", False)) or (
        inferred_completion_from_logs and not today_plan_query
    )

    meal_logged_today = bool(structured_logs.get("meal_logged")) or bool((structured_logs.get("meal_text") or "").strip())
    workout_minutes_today = structured_logs.get("workout_minutes")
    workout_completed_today = structured_logs.get("workout_completed")
    explicit_missed_workout = any(
        token in message_l
        for token in [
            "missed workout",
            "skipped workout",
            "didn't workout",
            "didnt workout",
            "no workout",
            "couldn't workout",
            "couldnt workout",
        ]
    )
    workout_missed_today = bool(
        explicit_missed_workout
        or (workout_completed_today is False and isinstance(workout_minutes_today, (int, float)) and int(workout_minutes_today) == 0)
    )
    meal_on_track_workout_missed = bool(meal_logged_today and workout_missed_today)

    if meal_on_track_workout_missed:
        compensation_requested = True
    food_adapter_needed = llm_brain.get("needs_food_adaptation", False) or ("only have" in message_l)
    food_adapter = _food_reality_adapter(goal, request.message or "") if food_adapter_needed else {}

    llm_wants_refresh = llm_brain.get("should_refresh_plan", False)
    if compensation_requested:
        llm_wants_refresh = True
    if meal_on_track_workout_missed:
        llm_wants_refresh = True
    if cravings_query:
        llm_wants_refresh = False
    if completion_update and not today_plan_query and request.mode != "plan":
        # Completion updates should not re-trigger full plan refresh by default.
        llm_wants_refresh = False

    decision = {
        "goal": goal,
        "mode": request.mode,
        "drift": drift,
        "adherence_signal": llm_brain.get("adherence_signal", "neutral"),
        "needs_food_adaptation": bool(food_adapter),
        "recovery_mode": recovery_mode,
        "travel_disruption": travel_disruption,
        "travel_days": travel_days,
        "travel_resumed": travel_resume_signal,
        "active_travel_window": active_travel_window,
        "cravings_query": cravings_query,
        "meal_on_track_workout_missed": meal_on_track_workout_missed,
        "compensation_requested": compensation_requested,
        "user_requests_restructure": llm_brain.get("user_requests_restructure", False),
        "should_initialize_plan": not has_existing_plans,
        "should_refresh_plan": (
            llm_wants_refresh
            or bool(drift.get("should_adapt_plan"))
            or request.mode == "plan"
            or not has_existing_plans
            or resume_training_query
            or bool(food_adapter)
            or recovery_mode.get("enabled", False)
            or meal_on_track_workout_missed
            or active_travel_window
        ),
        "weekly_reflection_requested": llm_brain.get("weekly_reflection_requested", False)
        or request.mode == "weekly_reflection"
        or "weekly" in (request.message or "").lower(),
        "resume_training_query": resume_training_query,
    }

    if cravings_query and request.mode != "plan":
        decision["should_refresh_plan"] = False
    trace.append(AgentStep(name="decide", status="ok", detail="Computed plan/nudge decisions", output=decision))

    plan_updates: Dict[str, Any] = {}

    if decision["should_refresh_plan"]:
        workout_input = _build_workout_input(merged_profile, goal)
        nutrition_input = _build_nutrition_input(merged_profile, goal)

        if recovery_mode.get("enabled"):
            # Low-friction recovery mode prioritizes consistency over intensity.
            workout_input.time_per_day = min(20, max(10, workout_input.time_per_day))
            workout_input.fitness_level = "beginner"
            nutrition_input.activity = "low"
            actions.append("recovery_mode_activated")

        if compensation_requested:
            # Keep compensation practical after a disruption day.
            workout_input.time_per_day = min(60, max(30, workout_input.time_per_day))
            actions.append("travel_compensation_planned")

        if meal_on_track_workout_missed:
            # User followed meals but missed workout: compensate with a practical bump.
            workout_input.time_per_day = min(75, max(30, workout_input.time_per_day + 10))
            nutrition_input.activity = "low"
            actions.append("meal_on_track_workout_compensation")

        if drift.get("status") == "behind":
            workout_input.time_per_day = min(90, max(20, workout_input.time_per_day + 10))

        base_workout_plan = (current_workout or {}).get("plan") if isinstance(current_workout, dict) else None
        base_nutrition_plan = (current_nutrition or {}).get("plan") if isinstance(current_nutrition, dict) else None

        if compensation_requested and travel_disruption and travel_days > 0:
            # Dynamic rewrite from existing plans, driven by user-specified travel duration.
            if not isinstance(base_workout_plan, list) or not base_workout_plan:
                base_workout_plan = generate_workout_plan(workout_input).get("plan", [])
            if not isinstance(base_nutrition_plan, list) or not base_nutrition_plan:
                base_nutrition_plan = generate_nutrition_plan(nutrition_input).get("plan", [])

            adaptive = _adaptive_travel_compensation_with_ai(
                goal=goal,
                message=request.message or "",
                travel_days=travel_days,
                base_workout_plan=base_workout_plan,
                base_nutrition_plan=base_nutrition_plan,
            )

            if adaptive:
                plan_updates["workout"] = adaptive["workout"]
                plan_updates["nutrition"] = adaptive["nutrition"]
                actions.append("travel_window_structured_dynamic")
            else:
                # Deterministic fallback if model output is malformed.
                plan_updates["workout"] = _build_travel_compensation_workout_plan(base_workout_plan, travel_days)
                plan_updates["nutrition"] = _build_travel_compensation_nutrition_plan(base_nutrition_plan, travel_days)
            actions.append("travel_window_structured")
        else:
            plan_updates["workout"] = generate_workout_plan(workout_input)
            plan_updates["nutrition"] = generate_nutrition_plan(nutrition_input)

        if decision.get("should_initialize_plan"):
            reason = "initial_agent_plan"
        elif resume_training_query:
            reason = "post_travel_resume_restructure"
        elif active_travel_window:
            reason = "active_travel_window_restructure"
        elif meal_on_track_workout_missed:
            reason = "missed_workout_compensation_with_nutrition_adherence"
        elif compensation_requested:
            reason = "travel_disruption_compensation"
        elif recovery_mode.get("enabled"):
            reason = "recovery_mode_due_to_missed_days"
        elif drift.get("status") == "behind":
            reason = "adaptive_update_due_to_drift"
        else:
            reason = "manual_or_periodic_refresh"

        workout_doc_id = _save_plan_revision(
            request.user_id,
            "workoutPlans",
            "plan",
            plan_updates["workout"],
            reason,
            goal,
        )
        nutrition_doc_id = _save_plan_revision(
            request.user_id,
            "nutritionPlans",
            "plan",
            plan_updates["nutrition"],
            reason,
            goal,
        )

        current_workout = {
            "id": workout_doc_id,
            "plan": plan_updates["workout"].get("plan", []),
            "goal": goal,
            "reason": reason,
            "source": "agent",
        }
        current_nutrition = {
            "id": nutrition_doc_id,
            "plan": plan_updates["nutrition"].get("plan", []),
            "goal": goal,
            "reason": reason,
            "source": "agent",
        }
        actions.append("plans_refreshed")

    if food_adapter:
        plan_updates["food_reality_adapter"] = food_adapter
        actions.append("food_adaptation_generated")

    if cravings_query:
        plan_updates["craving_swaps"] = _craving_swap_recommendations(goal, request.message or "", travel_days)
        actions.append("craving_swaps_generated")

    for hint in llm_brain.get("action_hints", []):
        action_label = f"ai_hint:{hint}"
        if action_label not in actions:
            actions.append(action_label)

    risk_input = _to_predict_request(merged_profile)
    decision["risk_snapshot"] = simple_risk_engine(risk_input)

    weekly = {}
    if decision["weekly_reflection_requested"]:
        weekly = _weekly_reflection(goal, recent_logs)
        actions.append("weekly_reflection_created")

    nudges = llm_brain.get("nudges") or _build_nudges(recent_logs, request.autonomous)
    if nudges:
        actions.append("nudges_prepared")

    trace.append(
        AgentStep(
            name="act",
            status="ok",
            detail="Prepared actions and plan updates",
            output={
                "actions": actions,
                "plan_update_keys": list(plan_updates.keys()),
                "nudge_count": len(nudges),
            },
        )
    )

    if nudges:
        _upsert_daily_log(request.user_id, {"nudges": nudges})

    final_text = _llm_finalize_response(
        message=request.message or "",
        user_intent={
            "today_plan_query": today_plan_query,
            "completion_update": completion_update,
            "log_summary_query": log_summary_query,
            "post_travel_query": post_travel_query,
            "cravings_query": cravings_query,
        },
        decision=decision,
        actions=actions,
        nudges=nudges,
        plan_updates=plan_updates,
        current_plans={
            "workout": current_workout,
            "nutrition": current_nutrition,
        },
        progress_summary=progress_summary,
        weekly_reflection=weekly,
        recent_logs=recent_logs,
        structured_logs=structured_logs,
    )
    summary = final_text["summary"]
    decision["response_meta"] = final_text.get(
        "meta",
        {"fallback_used": False, "fallback_reason": None},
    )

    response = AgentResponse(
        summary=summary,
        conversation_id=conversation_id,
        ai_reply=final_text["ai_reply"],
        actions=actions,
        nudges=nudges,
        observed_state=observed,
        decision=decision,
        current_plans={
            "workout": current_workout,
            "nutrition": current_nutrition,
        },
        progress_summary=progress_summary,
        structured_logs=structured_logs,
        plan_updates=plan_updates,
        weekly_reflection=weekly,
        trace=trace,
    )

    _record_agent_event(
        request.user_id,
        {
            "conversation_id": conversation_id,
            "mode": request.mode,
            "message": request.message,
            "summary": summary,
            "ai_reply": final_text["ai_reply"],
            "response_meta": decision.get("response_meta", {}),
            "actions": actions,
            "decision": decision,
            "progress_summary": progress_summary,
            "structured_logs": structured_logs,
        },
    )

    _append_conversation_message(
        request.user_id,
        conversation_id,
        "assistant",
        final_text["ai_reply"],
        payload={
            "summary": summary,
            "actions": actions,
            "decision": decision,
            "response_meta": decision.get("response_meta", {}),
            "progress_summary": progress_summary,
            "structured_logs": structured_logs,
            "plan_updates": plan_updates,
            "weekly_reflection": weekly,
            "trace": [step.model_dump() for step in trace],
        },
    )

    return response
