import json
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

from firebase_admin import firestore

from app.core.firebase_client import db
from app.core.groq_client import generate_ai_response, generate_ai_text_response
from app.schemas.agent import AgentRequest, AgentResponse, AgentStep
from app.services.nutrition_shopping_service import build_nutrition_shopping_plan
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


def _clip_text(value: Any, limit: int = 80) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _is_workout_logged(item: Dict[str, Any]) -> bool:
    minutes = item.get("workout_minutes", 0)
    completed = bool(item.get("workout_completed", False))
    return completed or (isinstance(minutes, (int, float)) and int(minutes) > 0)


def _is_meal_logged(item: Dict[str, Any]) -> bool:
    meal = (item.get("meal_text") or "").strip()
    meal_logged = bool(item.get("meal_logged", False))
    return bool(meal) or meal_logged


def _summarize_workout_log(item: Dict[str, Any]) -> Dict[str, Any]:
    minutes = item.get("workout_minutes", 0)
    day_number = item.get("workout_day_number")
    return {
        "date": str(item.get("date") or ""),
        "minutes": int(minutes) if isinstance(minutes, (int, float)) and int(minutes) > 0 else 0,
        "day": int(day_number) if isinstance(day_number, (int, float)) and int(day_number) > 0 else None,
    }


def _summarize_meal_log(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "date": str(item.get("date") or ""),
        "meal": _clip_text(item.get("meal_text") or "Meal logged", limit=72),
    }


def _find_last_missed_workout_day(logs: List[Dict[str, Any]]) -> str | None:
    for item in logs:
        if not _is_workout_logged(item):
            raw_date = item.get("date")
            if isinstance(raw_date, str) and raw_date.strip():
                return raw_date.strip()
    return None


def _build_coach_memory_payload(user_id: str) -> Dict[str, Any]:
    all_logs = _fetch_all_daily_logs(user_id, max_entries=120)
    latest_health = _fetch_latest_health_record(user_id)
    profile = _fetch_user_profile(user_id)
    latest_event = _fetch_latest_agent_event(user_id)

    workout_logs = [_summarize_workout_log(item) for item in all_logs if _is_workout_logged(item)][:3]
    meal_logs = [_summarize_meal_log(item) for item in all_logs if _is_meal_logged(item)][:3]
    streak = _compute_activity_streak(all_logs)
    missed_day = _find_last_missed_workout_day(all_logs)

    explicit_travel_days = 0
    for item in all_logs[:14]:
        if not bool(item.get("travel_disruption", False)):
            continue
        raw_days = item.get("travel_days")
        if isinstance(raw_days, (int, float)) and int(raw_days) > 0:
            explicit_travel_days = int(raw_days)
            break

    travel_remaining = _infer_active_travel_days(all_logs[:30], latest_event, explicit_travel_days)
    travel_last_noted = None
    for item in all_logs:
        if bool(item.get("travel_disruption", False)):
            raw_date = item.get("date")
            if isinstance(raw_date, str) and raw_date.strip():
                travel_last_noted = raw_date.strip()
                break

    active_goal = str(latest_health.get("goal") or profile.get("goal") or "general fitness").strip() or "general fitness"

    return {
        "last_workouts": workout_logs,
        "last_meals": meal_logs,
        "streak": int(streak),
        "missed_days": missed_day,
        "active_goal": active_goal,
        "travel_status": {
            "active": bool(travel_remaining > 0),
            "days_remaining": int(travel_remaining),
            "last_noted": travel_last_noted,
        },
    }


def _format_coach_memory_system_prompt(memory_payload: Dict[str, Any], response_mode: str = "text", max_chars: int = 1800) -> str:
    workouts = memory_payload.get("last_workouts") if isinstance(memory_payload.get("last_workouts"), list) else []
    meals = memory_payload.get("last_meals") if isinstance(memory_payload.get("last_meals"), list) else []
    streak = int(memory_payload.get("streak", 0) or 0)
    missed_day = memory_payload.get("missed_days")
    goal = str(memory_payload.get("active_goal") or "general fitness").strip()

    travel = memory_payload.get("travel_status") if isinstance(memory_payload.get("travel_status"), dict) else {}
    travel_active = bool(travel.get("active", False))
    travel_days_remaining = int(travel.get("days_remaining", 0) or 0)
    travel_last_noted = travel.get("last_noted")

    workout_line = "none"
    if workouts:
        workout_line = "; ".join(
            [
                f"{item.get('date')}: {int(item.get('minutes', 0) or 0)} min"
                + (f" (Day {item.get('day')})" if item.get("day") is not None else "")
                for item in workouts
                if isinstance(item, dict)
            ]
        )

    meal_line = "none"
    if meals:
        meal_line = "; ".join(
            [
                f"{item.get('date')}: {_clip_text(item.get('meal'), 54)}"
                for item in meals
                if isinstance(item, dict)
            ]
        )

    travel_line = "inactive"
    if travel_active:
        travel_line = f"active, about {travel_days_remaining} day(s) remaining"
    if isinstance(travel_last_noted, str) and travel_last_noted.strip():
        travel_line = f"{travel_line}, last noted {travel_last_noted.strip()}"

    output_rule = "Reply in plain conversational text."
    if response_mode == "json":
        output_rule = "Respond only in valid JSON matching the requested schema."

    system_prompt = (
        "You are Lifeline Coach, a fitness and nutrition coach who uses recent user memory to personalize advice.\n"
        f"Structured user memory:\n"
        f"- Active goal: {goal}\n"
        f"- Current streak: {streak} day(s)\n"
        f"- Last missed workout day: {missed_day or 'none recorded'}\n"
        f"- Recent workouts (up to 3): {workout_line}\n"
        f"- Recent meals (up to 3): {meal_line}\n"
        f"- Travel status: {travel_line}\n"
        "Use memory naturally and briefly. Reference trends when relevant (for example: 'Last week...' or 'You have been consistent...'). "
        "Do not dump raw database fields or IDs. Keep memory usage concise and focused. "
        f"{output_rule}"
    )

    if len(system_prompt) > max_chars:
        system_prompt = system_prompt[: max(0, max_chars - 3)].rstrip() + "..."

    return system_prompt


def _generate_ai_response_with_memory(prompt: str, user_id: str | None = None) -> str:
    if not user_id:
        return generate_ai_response(prompt)

    try:
        payload = _build_coach_memory_payload(user_id)
        system_prompt = _format_coach_memory_system_prompt(payload, response_mode="json")
        return generate_ai_response(prompt, system_prompt=system_prompt)
    except Exception:
        return generate_ai_response(prompt)


def _generate_ai_text_response_with_memory(prompt: str, user_id: str | None = None) -> str:
    if not user_id:
        return generate_ai_text_response(prompt)

    try:
        payload = _build_coach_memory_payload(user_id)
        system_prompt = _format_coach_memory_system_prompt(payload, response_mode="text")
        return generate_ai_text_response(prompt, system_prompt=system_prompt)
    except Exception:
        return generate_ai_text_response(prompt)


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

    meal_tokens = [
        "ate",
        "had",
        "eaten",
        "meal",
        "breakfast",
        "lunch",
        "dinner",
        "snack",
        "diet",
        "food",
    ]
    if any(token in lowered for token in meal_tokens):
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
        extracted["meal_logged"] = True

    completion_day_match_alt = re.search(r"\bday\s*(\d{1,2})\s*(?:done|completed|finished)\b", lowered)
    if completion_day_match_alt:
        extracted["workout_completed"] = True
        extracted["workout_day_number"] = int(completion_day_match_alt.group(1))
        extracted["meal_logged"] = True

    completion_day_match_over = re.search(r"\bday\s*(\d{1,2})\s*(?:is\s+)?over\b", lowered)
    if completion_day_match_over:
        extracted["workout_completed"] = True
        extracted["workout_day_number"] = int(completion_day_match_over.group(1))
        extracted["meal_logged"] = True

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
    user_id: str | None = None,
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
        raw = _generate_ai_response_with_memory(prompt, user_id=user_id)
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


def _extract_structured_logs_with_ai(message: str, user_id: str | None = None) -> Dict[str, Any]:
    text = (message or "").strip()
    if not text:
        return {}

    def _looks_like_meal_log(raw_text: str) -> bool:
        lowered_text = (raw_text or "").lower()
        if not lowered_text:
            return False
        meal_hints = [
            "ate",
            "had",
            "eaten",
            "meal",
            "breakfast",
            "lunch",
            "dinner",
            "snack",
            "diet",
            "food",
        ]
        return any(token in lowered_text for token in meal_hints)

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
        raw = _generate_ai_response_with_memory(prompt, user_id=user_id)
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

        # Deterministic fallback: if message clearly contains meal-log cues,
        # persist meal data even when model extraction is incomplete.
        if _looks_like_meal_log(text):
            if not isinstance(cleaned.get("meal_text"), str) or not cleaned.get("meal_text", "").strip():
                cleaned["meal_text"] = text
            cleaned["meal_logged"] = True

        if cleaned:
            return cleaned
    except Exception:
        pass

    return _extract_structured_logs(text)


def _llm_agent_brain(
    user_id: str | None,
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
        raw = _generate_ai_response_with_memory(prompt, user_id=user_id)
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
    user_id: str | None,
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
        raw = _generate_ai_response_with_memory(prompt, user_id=user_id)
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
            repair_raw = _generate_ai_response_with_memory(repair_prompt, user_id=user_id)
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
            plan_repair_raw = _generate_ai_response_with_memory(plan_repair_prompt, user_id=user_id)
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
            recovery_text = (_generate_ai_text_response_with_memory(recovery_prompt, user_id=user_id) or "").strip()
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


def _food_reality_adapter(goal: str, message: str, user_id: str | None = None) -> Dict[str, Any]:
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
        raw = _generate_ai_response_with_memory(prompt, user_id=user_id)
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


def _parse_date_key(raw: str) -> date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _compute_activity_streak(logs: List[Dict[str, Any]]) -> int:
    dated_logs: Dict[date, Dict[str, Any]] = {}
    for item in logs:
        d = _parse_date_key(item.get("date"))
        if d is None:
            continue
        dated_logs[d] = item

    if not dated_logs:
        return 0

    streak = 0
    cursor = _utc_now().date()
    while True:
        current = dated_logs.get(cursor)
        if not current:
            break

        minutes = current.get("workout_minutes", 0)
        worked = bool(current.get("workout_completed", False)) or (isinstance(minutes, (int, float)) and int(minutes) > 0)
        meal = bool(current.get("meal_logged", False)) or bool((current.get("meal_text") or "").strip())
        if not (worked or meal):
            break

        streak += 1
        cursor = cursor - timedelta(days=1)

    return streak


def _build_7d_trends(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_date: Dict[date, Dict[str, Any]] = {}
    for item in logs:
        d = _parse_date_key(item.get("date"))
        if d is None:
            continue
        by_date[d] = item

    today = _utc_now().date()
    start = today - timedelta(days=6)
    trends: List[Dict[str, Any]] = []

    for offset in range(7):
        d = start + timedelta(days=offset)
        item = by_date.get(d, {})
        minutes = item.get("workout_minutes", 0)
        workout_completed = bool(item.get("workout_completed", False)) or (isinstance(minutes, (int, float)) and int(minutes) > 0)
        meal_logged = bool(item.get("meal_logged", False)) or bool((item.get("meal_text") or "").strip())

        trends.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "workout_completed": workout_completed,
                "meal_logged": meal_logged,
                "workout_minutes": int(minutes) if isinstance(minutes, (int, float)) and int(minutes) > 0 else 0,
            }
        )

    return trends


def _fetch_recent_agent_events(user_id: str, max_entries: int = 300) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("agentEvents")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(max_entries)
        .get()
    )
    return [doc.to_dict() or {} for doc in docs]


def _fetch_recent_shopping_plans(user_id: str, max_entries: int = 100) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("nutritionShoppingPlans")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(max_entries)
        .get()
    )
    return [doc.to_dict() or {} for doc in docs]


def get_agent_metrics(user_id: str) -> Dict[str, Any]:
    all_logs = _fetch_all_daily_logs(user_id)
    progress_summary = _refresh_progress_summary(user_id, logs=all_logs)
    trends_7d = _build_7d_trends(all_logs)

    today = _utc_now().date()
    last_7_start = today - timedelta(days=6)

    logs_7d: List[Dict[str, Any]] = []
    for item in all_logs:
        d = _parse_date_key(item.get("date"))
        if d is None:
            continue
        if d < last_7_start or d > today:
            continue
        logs_7d.append(item)

    workout_days_7d = _count_workout_days(logs_7d)
    meal_days_7d = _count_meal_logged_days(logs_7d)
    logs_present_7d = len({item.get("date") for item in logs_7d if isinstance(item.get("date"), str)})

    events = _fetch_recent_agent_events(user_id)
    plan_refreshes_7d = 0
    for event in events:
        created = event.get("createdAt")
        created_date = created.date() if hasattr(created, "date") else None
        if created_date and created_date < last_7_start:
            continue
        actions = event.get("actions") if isinstance(event.get("actions"), list) else []
        if "plans_refreshed" in actions:
            plan_refreshes_7d += 1

    shopping_plans = _fetch_recent_shopping_plans(user_id)
    confirmations_7d = 0
    for item in shopping_plans:
        confirmed_at = item.get("confirmedAt")
        confirmed_date = confirmed_at.date() if hasattr(confirmed_at, "date") else None
        if confirmed_date is None or confirmed_date < last_7_start:
            continue
        if str(item.get("status", "")).lower() == "confirmed":
            confirmations_7d += 1

    streak = _compute_activity_streak(all_logs)
    adherence_7d = round((workout_days_7d + meal_days_7d) / 14.0, 2)

    return {
        "progress_summary": progress_summary,
        "metrics": {
            "active_streak_days": streak,
            "adherence_rate_7d": adherence_7d,
            "workout_days_7d": workout_days_7d,
            "meal_log_days_7d": meal_days_7d,
            "days_with_any_log_7d": logs_present_7d,
            "plan_refreshes_7d": plan_refreshes_7d,
            "shopping_confirmations_7d": confirmations_7d,
        },
        "trends_7d": trends_7d,
    }


def get_proactive_recommendations(user_id: str, persist_event: bool = True) -> Dict[str, Any]:
    all_logs = _fetch_all_daily_logs(user_id)
    metrics_payload = get_agent_metrics(user_id)
    metrics = metrics_payload.get("metrics", {})
    progress_summary = metrics_payload.get("progress_summary", {})
    trends_7d = metrics_payload.get("trends_7d", [])

    recommendations: List[Dict[str, Any]] = []

    latest_date = None
    if all_logs:
        latest_date = _parse_date_key(all_logs[0].get("date"))

    if latest_date is None or (_utc_now().date() - latest_date).days >= 2:
        recommendations.append(
            {
                "type": "checkin",
                "priority": "high",
                "title": "Send a quick daily check-in",
                "reason": "No recent logs found in the last 48 hours.",
                "suggested_message": "I missed yesterday. Give me a short recovery plan for today.",
            }
        )

    if int(metrics.get("meal_log_days_7d", 0)) <= 2:
        recommendations.append(
            {
                "type": "nutrition_logging",
                "priority": "high",
                "title": "Improve meal logging consistency",
                "reason": "Meal logs are low this week; tracking meals improves nutrition adaptation.",
                "suggested_message": "Today I ate ... for breakfast/lunch/dinner.",
            }
        )

    if int(metrics.get("workout_days_7d", 0)) <= 2:
        recommendations.append(
            {
                "type": "workout_recovery",
                "priority": "medium",
                "title": "Run a low-friction workout day",
                "reason": "Workout frequency is low this week.",
                "suggested_message": "Give me a 20-minute easy workout to get back on track.",
            }
        )

    if int(metrics.get("shopping_confirmations_7d", 0)) == 0:
        recommendations.append(
            {
                "type": "pantry",
                "priority": "low",
                "title": "Sync pantry and shopping items",
                "reason": "No recent shopping confirmations detected.",
                "suggested_message": "I am missing these items: ...",
            }
        )

    if persist_event:
        # Persist proactive signal for observability.
        _record_agent_event(
            user_id,
            {
                "summary": "Proactive check generated",
                "actions": ["proactive_check_generated"],
                "decision": {
                    "proactive_recommendation_count": len(recommendations),
                },
                "progress_summary": progress_summary,
            },
        )

    return {
        "ok": True,
        "metrics": metrics,
        "progress_summary": progress_summary,
        "trends_7d": trends_7d,
        "recommendations": recommendations,
    }


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


def _craving_swap_recommendations(goal: str, message: str, travel_days: int, user_id: str | None = None) -> Dict[str, Any]:
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
        raw = _generate_ai_response_with_memory(prompt, user_id=user_id)
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


_TODAY_PLAN_CACHE: Dict[str, Dict[str, Any]] = {}


def preprocess_message(message: str) -> str:
    return (message or "").strip().lower()


def is_completion(msg: str) -> bool:
    completion_words = ["done", "completed", "finished"]
    return (
        any(word in msg for word in completion_words)
        and ("workout" in msg or "today" in msg or "plan" in msg)
    )


def is_missing_ingredients(msg: str) -> bool:
    phrases = [
        "i don't have",
        "i dont have",
        "i am missing",
        "i'm missing",
        "out of",
    ]
    return any(phrase in msg for phrase in phrases)


def is_today_plan_request(msg: str) -> bool:
    phrases = [
        "today plan",
        "plan for today",
        "what's today's plan",
        "what is today's plan",
        "what to do today",
        "what should i do today",
        "today's workout",
        "today workout",
        "what for today",
    ]

    return any(phrase in msg for phrase in phrases)


def detect_intent(message: str) -> str:
    msg = preprocess_message(message)

    if not msg:
        return "general_chat"

    # Strict intent order (single-intent only):
    # 1) completion, 2) missing, 3) travel, 4) today plan, 5) progress, else general.
    if is_completion(msg):
        return "completion_update"

    if is_missing_ingredients(msg):
        return "missing_ingredients"

    if "travel" in msg or "travelling" in msg:
        return "travel_update"

    if is_today_plan_request(msg):
        return "today_plan_request"

    if "progress" in msg or "stats" in msg:
        return "progress_query"

    return "general_chat"


def extract_items_from_message(message: str) -> List[str]:
    msg = preprocess_message(message)
    if not msg:
        return []

    stopwords = ["today", "plan", "workout", "meal"]

    patterns = [
        r"i don't have (.+)",
        r"i dont have (.+)",
        r"i am missing (.+)",
        r"i'm missing (.+)",
        r"out of (.+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, msg)
        if not match:
            continue

        items_str = (match.group(1) or "").strip()
        if not items_str:
            return []

        items = re.split(r",|and", items_str)

        cleaned: List[str] = []
        seen = set()
        for item in items:
            normalized = item.strip()
            normalized = normalized.replace("'", "")
            normalized = re.sub(r"[^a-z\s]", " ", normalized).strip()
            normalized = re.sub(r"\s+", " ", normalized)

            if len(normalized) <= 2:
                continue
            if normalized in stopwords:
                continue
            if any(word in normalized for word in ["plan", "today"]):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)

        return cleaned

    return []


def _fetch_pantry_current(user_id: str) -> Dict[str, Any]:
    snap = db.collection("users").document(user_id).collection("pantry").document("current").get()
    if not snap.exists:
        return {}
    return snap.to_dict() or {}


def update_pantry_missing(uid: str, items: List[str]) -> None:
    pantry_ref = db.collection("users").document(uid).collection("pantry").document("current")
    pantry_doc = pantry_ref.get()
    pantry_data = pantry_doc.to_dict() if pantry_doc.exists else {}

    normalized_items = []
    for item in items:
        key = str(item or "").strip().lower()
        if not key:
            continue
        pantry_data[key] = False
        normalized_items.append(key)

    # Keep legacy array fields in sync for backward compatibility.
    existing_unavailable = pantry_data.get("unavailable_items") if isinstance(pantry_data.get("unavailable_items"), list) else []
    unavailable_set = {str(x).strip().lower() for x in existing_unavailable if str(x).strip()}
    unavailable_set.update(normalized_items)
    pantry_data["unavailable_items"] = sorted(unavailable_set)
    pantry_data["updatedAt"] = firestore.SERVER_TIMESTAMP

    pantry_ref.set(pantry_data, merge=True)


def _find_affected_meals(plan: List[Dict[str, Any]], missing_items: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(plan, list) or not missing_items:
        return []

    affected: List[Dict[str, Any]] = []
    meal_keys = ["breakfast", "lunch", "snacks", "dinner"]
    for day in plan:
        if not isinstance(day, dict):
            continue
        day_label = str(day.get("day", "Day"))
        for key in meal_keys:
            meal_items = day.get(key) if isinstance(day.get(key), list) else []
            meal_blob = " ".join([str(x).lower() for x in meal_items])
            hit = [item for item in missing_items if item.lower() in meal_blob]
            if hit:
                affected.append(
                    {
                        "day": day_label,
                        "meal": key,
                        "missing_items": hit,
                    }
                )

    return affected


def _mock_estimated_cost(items: List[str]) -> int:
    if not items:
        return 0
    price_map = {
        "eggs": 90,
        "rice": 140,
        "milk": 60,
        "paneer": 120,
        "curd": 60,
        "oats": 95,
        "dal": 130,
        "banana": 60,
    }
    return int(sum(price_map.get(item, 80) for item in items))


def _default_handler_payload(intent: str, response: str, why: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "intent": intent,
        "response": response,
        "data": data or {},
        "why_this_action": why,
    }


def handle_missing_ingredients(uid: str, message: str) -> Dict[str, Any]:
    items = [str(x).strip().lower() for x in extract_items_from_message(message) if str(x).strip()]
    current_nutrition = _fetch_latest_plan(uid, "nutritionPlans")
    nutrition_plan = current_nutrition.get("plan") if isinstance(current_nutrition.get("plan"), list) else []
    pantry_current = _fetch_pantry_current(uid)

    available_items = pantry_current.get("available_items") if isinstance(pantry_current.get("available_items"), list) else []
    preferred_providers = pantry_current.get("preferred_providers") if isinstance(pantry_current.get("preferred_providers"), list) else []

    if not items:
        return _default_handler_payload(
            "missing_ingredients",
            "I can create a shopping draft, but I could not confidently extract missing items. Please list them like: eggs, rice, curd.",
            "Extraction fallback triggered because no clear missing ingredient tokens were found.",
            {
                "items": [],
                "requires_user_confirmation": True,
            },
        )

    affected_meals = _find_affected_meals(nutrition_plan, items)
    impacted_days = len({x.get("day") for x in affected_meals if isinstance(x, dict)})
    coverage_days = max(1, impacted_days or min(3, len(nutrition_plan) if nutrition_plan else 3))

    # pantry/current is the source of truth for availability in UI.
    update_pantry_missing(uid, items)

    shopping_plan = build_nutrition_shopping_plan(
        unavailable_items=items,
        available_items=available_items,
        preferred_providers=preferred_providers,
    )
    provider_links = [
        {
            "provider": x.get("provider"),
            "cart_url": x.get("cart_url"),
        }
        for x in (shopping_plan.get("provider_plans") if isinstance(shopping_plan.get("provider_plans"), list) else [])
    ]

    estimated_cost = _mock_estimated_cost(items)

    draft_ref = (
        db.collection("users")
        .document(uid)
        .collection("nutritionShoppingPlans")
        .document()
    )
    draft_ref.set(
        {
            "status": "pending",
            "intent": "missing_ingredients",
            "items": items,
            "missing_items": items,
            "affected_meals": affected_meals,
            "coverage_days": coverage_days,
            "estimated_cost": estimated_cost,
            "shopping_plan": shopping_plan,
            "provider_links": provider_links,
            "requires_user_confirmation": True,
            "created_at": firestore.SERVER_TIMESTAMP,
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    impact_text = (
        f"I found {len(affected_meals)} affected meal slot(s) in your current nutrition plan. "
        if affected_meals
        else "I did not find direct meal collisions, but these items can still impact nutrition consistency. "
    )
    response = (
        f"Noted missing items: {', '.join(items)}. {impact_text}"
        f"I prepared a pending shopping draft (estimated cost: Rs {estimated_cost}) with provider links. "
        "Do you want me to mark this shopping plan as confirmed?"
    )

    return _default_handler_payload(
        "missing_ingredients",
        response,
        "Missing ingredient intent matched deterministic rules; shopping draft created without auto-confirmation.",
        {
            "items": items,
            "coverage_days": coverage_days,
            "estimated_cost": estimated_cost,
            "provider_links": provider_links,
            "affected_meals": affected_meals,
            "shopping_plan_id": draft_ref.id,
            "shopping_plan": shopping_plan,
            "requires_user_confirmation": True,
        },
    )


def handle_completion_update(uid: str, message: str) -> Dict[str, Any]:
    profile = {**_fetch_user_profile(uid), **_fetch_latest_health_record(uid)}
    today_log = _fetch_daily_log(uid)
    progress_summary = _fetch_progress_summary(uid)

    planned_minutes = today_log.get("workout_minutes") if isinstance(today_log.get("workout_minutes"), (int, float)) else None
    if not isinstance(planned_minutes, (int, float)) or int(planned_minutes) <= 0:
        planned_minutes = _resolve_allotted_workout_minutes(profile)

    day_number = today_log.get("workout_day_number") if isinstance(today_log.get("workout_day_number"), (int, float)) else None
    if not isinstance(day_number, (int, float)) or int(day_number) <= 0:
        day_number = int(progress_summary.get("total_workout_days", 0)) + 1

    _upsert_daily_log(
        uid,
        {
            "workout_completed": True,
            "workout_minutes": int(planned_minutes),
            "workout_day_number": int(day_number),
            "adherence_status": "good",
        },
    )

    all_logs = _fetch_all_daily_logs(uid)
    refreshed_summary = _refresh_progress_summary(uid, logs=all_logs)
    streak = _compute_activity_streak(all_logs)

    response = (
        f"Great work. Today's workout is marked complete ({int(planned_minutes)} minutes). "
        f"Current active streak: {streak} day(s). Keep momentum tomorrow."
    )

    return _default_handler_payload(
        "completion_update",
        response,
        "Completion intent matched; daily log and progress summary were updated deterministically.",
        {
            "streak_days": streak,
            "total_workout_minutes": refreshed_summary.get("total_workout_minutes", 0),
            "progress_summary": refreshed_summary,
            "daily_log_date": _today_key(),
        },
    )


def _resolve_today_plan_payload(uid: str) -> Dict[str, Any]:
    today_key = _today_key()
    cached = _TODAY_PLAN_CACHE.get(uid)
    if cached and cached.get("date") == today_key:
        return cached.get("payload", {})

    workout = _fetch_latest_plan(uid, "workoutPlans")
    nutrition = _fetch_latest_plan(uid, "nutritionPlans")
    progress_summary = _fetch_progress_summary(uid)

    total_workout_days = int(progress_summary.get("total_workout_days", 0) or 0)
    cycle_week = (total_workout_days // 7) + 1
    cycle_day = (total_workout_days % 7) + 1
    day_label = f"Day {cycle_day}"

    workout_plan = workout.get("plan") if isinstance(workout.get("plan"), list) else []
    nutrition_plan = nutrition.get("plan") if isinstance(nutrition.get("plan"), list) else []

    workout_today = next((d for d in workout_plan if str(d.get("day", "")).strip().lower() == day_label.lower()), None)
    nutrition_today = next((d for d in nutrition_plan if str(d.get("day", "")).strip().lower() == day_label.lower()), None)

    if workout_today is None and workout_plan:
        workout_today = workout_plan[min(cycle_day - 1, len(workout_plan) - 1)]
    if nutrition_today is None and nutrition_plan:
        nutrition_today = nutrition_plan[min(cycle_day - 1, len(nutrition_plan) - 1)]

    payload = {
        "target_week": cycle_week,
        "target_day": cycle_day,
        "workout_today": workout_today or {},
        "nutrition_today": nutrition_today or {},
        "progress_summary": progress_summary,
        "workout_plan_id": workout.get("id"),
        "nutrition_plan_id": nutrition.get("id"),
    }
    _TODAY_PLAN_CACHE[uid] = {"date": today_key, "payload": payload}
    return payload


def handle_today_plan(uid: str) -> Dict[str, Any]:
    payload = _resolve_today_plan_payload(uid)
    today_log = _fetch_daily_log(uid)
    workout_minutes = today_log.get("workout_minutes") if isinstance(today_log.get("workout_minutes"), (int, float)) else 0
    workout_done = bool(today_log.get("workout_completed", False)) or int(workout_minutes) > 0

    if workout_done:
        response = (
            f"You're already done with today's workout ({int(workout_minutes)} minutes). Great job! "
            "You can focus on recovery, hydration, or light stretching."
        )
        payload = {
            **payload,
            "today_workout_completed": True,
            "today_workout_minutes": int(workout_minutes),
        }
    else:
        response = (
            f"Today's target is Week {payload.get('target_week')} Day {payload.get('target_day')}. "
            "I have included your workout and meal structure in the response data."
        )
        payload = {
            **payload,
            "today_workout_completed": False,
            "today_workout_minutes": int(workout_minutes),
        }

    return _default_handler_payload(
        "today_plan_request",
        response,
        "Today-plan intent matched; response built from stored plans and progress summary without LLM routing.",
        payload,
    )


def _build_light_travel_workout_plan(base_plan: List[Dict[str, Any]], travel_days: int) -> Dict[str, Any]:
    normalized = _normalize_workout_plan(base_plan)
    td = max(1, min(7, int(travel_days)))
    for idx in range(td):
        day = normalized[idx]
        day["warmup"] = ["10-minute light walk"]
        day["exercises"] = []
        day["cooldown"] = ["5-minute breathing + mobility"]
        day["tip"] = "Travel mode active: keep movement light and focus on sleep/hydration."
    return {"plan": normalized}


def handle_travel_update(uid: str, message: str) -> Dict[str, Any]:
    travel_days = _extract_travel_days(message)
    if travel_days <= 0:
        travel_days = 3

    travel_ref = db.collection("users").document(uid).collection("travelState").document("current")
    travel_ref.set(
        {
            "active": True,
            "travel_days": travel_days,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "note": "Auto-detected travel update from chat.",
        },
        merge=True,
    )

    _upsert_daily_log(
        uid,
        {
            "travel_disruption": True,
            "travel_days": travel_days,
        },
    )

    current_workout = _fetch_latest_plan(uid, "workoutPlans")
    current_nutrition = _fetch_latest_plan(uid, "nutritionPlans")
    base_workout_plan = current_workout.get("plan") if isinstance(current_workout.get("plan"), list) else []
    base_nutrition_plan = current_nutrition.get("plan") if isinstance(current_nutrition.get("plan"), list) else []

    if not base_workout_plan:
        profile = {**_fetch_user_profile(uid), **_fetch_latest_health_record(uid)}
        goal = str(profile.get("goal") or "general fitness")
        base_workout_plan = generate_workout_plan(_build_workout_input(profile, goal)).get("plan", [])
    if not base_nutrition_plan:
        profile = {**_fetch_user_profile(uid), **_fetch_latest_health_record(uid)}
        goal = str(profile.get("goal") or "general fitness")
        base_nutrition_plan = generate_nutrition_plan(_build_nutrition_input(profile, goal)).get("plan", [])

    travel_workout = _build_light_travel_workout_plan(base_workout_plan, travel_days)
    travel_nutrition = {"plan": _normalize_nutrition_plan(base_nutrition_plan)}

    profile = {**_fetch_user_profile(uid), **_fetch_latest_health_record(uid)}
    goal = str(profile.get("goal") or "general fitness")
    workout_doc_id = _save_plan_revision(
        uid,
        "workoutPlans",
        "plan",
        travel_workout,
        "travel_mode_light_adjustment",
        goal,
    )
    nutrition_doc_id = _save_plan_revision(
        uid,
        "nutritionPlans",
        "plan",
        travel_nutrition,
        "travel_mode_nutrition_maintain",
        goal,
    )

    _record_agent_event(
        uid,
        {
            "type": "travel_update",
            "summary": f"Travel mode activated for {travel_days} day(s)",
            "actions": ["travel_mode_activated", "plans_refreshed"],
            "decision": {
                "travel_days": travel_days,
                "workout_plan_id": workout_doc_id,
                "nutrition_plan_id": nutrition_doc_id,
            },
        },
    )

    response = (
        f"Travel mode is now active for {travel_days} day(s). "
        "I shifted your workouts to light movement and preserved nutrition structure for consistency."
    )
    return _default_handler_payload(
        "travel_update",
        response,
        "Travel intent matched and duration extracted; travel-safe plan adjustment stored in Firestore.",
        {
            "travel_days": travel_days,
            "travel_mode": True,
            "workout_plan_id": workout_doc_id,
            "nutrition_plan_id": nutrition_doc_id,
        },
    )


def handle_progress_query(uid: str) -> Dict[str, Any]:
    summary = _fetch_progress_summary(uid)
    if not summary:
        all_logs = _fetch_all_daily_logs(uid)
        summary = _refresh_progress_summary(uid, logs=all_logs)

    workout_days = int(summary.get("total_workout_days", 0) or 0)
    meal_days = int(summary.get("total_meal_log_days", 0) or 0)
    minutes = int(summary.get("total_workout_minutes", 0) or 0)

    if workout_days >= 5 and meal_days >= 5:
        insight = "Strong consistency trend. Keep this cadence."
    elif workout_days == 0 and meal_days == 0:
        insight = "No recent tracked consistency yet. Start with one small action today."
    else:
        insight = "Progress is building. Increase consistency one step at a time."

    response = (
        f"Progress snapshot: {workout_days} workout day(s), {meal_days} meal-log day(s), "
        f"{minutes} total workout minute(s). {insight}"
    )

    return _default_handler_payload(
        "progress_query",
        response,
        "Progress intent matched; summary returned directly from progressStats without LLM routing.",
        {
            "workout_days": workout_days,
            "meal_log_days": meal_days,
            "total_minutes": minutes,
            "insight": insight,
            "progress_summary": summary,
        },
    )


def handle_general_chat(uid: str, message: str) -> Dict[str, Any]:
    prompt = (
        "You are Lifeline Coach. Respond concisely and helpfully in 2-4 sentences. "
        f"User message: {message.strip() or 'Hello'}"
    )
    try:
        reply = (_generate_ai_text_response_with_memory(prompt, user_id=uid) or "").strip()
    except Exception:
        reply = "I am here to help. Share your goal or today's challenge, and I will guide your next step."

    if not reply:
        reply = "I am here to help. Share your goal or today's challenge, and I will guide your next step."

    return _default_handler_payload(
        "general_chat",
        reply,
        "No deterministic specialized intent matched, so concise coaching chat response was used.",
        {},
    )


def run_agent_router(uid: str, message: str) -> Dict[str, Any]:
    intent = detect_intent(message)
    items = extract_items_from_message(message) if intent == "missing_ingredients" else None

    print(f"MESSAGE: {message}")
    print(f"INTENT: {intent}")
    print(f"EXTRACTED ITEMS: {items}")

    if intent == "missing_ingredients":
        if not items:
            return handle_general_chat(uid, message)
        return handle_missing_ingredients(uid, message)
    if intent == "completion_update":
        return handle_completion_update(uid, message)
    if intent == "today_plan_request":
        return handle_today_plan(uid)
    if intent == "travel_update":
        return handle_travel_update(uid, message)
    if intent == "progress_query":
        return handle_progress_query(uid)
    return handle_general_chat(uid, message)


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

    intent = detect_intent(request.message or "")
    trace.append(
        AgentStep(
            name="intent_detect",
            status="ok",
            detail="Deterministic intent routing complete",
            output={
                "intent": intent,
                "message": (request.message or "")[:200],
            },
        )
    )

    handler_payload = run_agent_router(request.user_id, request.message or "")
    trace.append(
        AgentStep(
            name="intent_handler",
            status="ok",
            detail=f"Handled intent: {intent}",
            output={
                "intent": handler_payload.get("intent"),
                "why_this_action": handler_payload.get("why_this_action"),
            },
        )
    )

    actions.append(f"intent:{intent}")
    data = handler_payload.get("data") if isinstance(handler_payload.get("data"), dict) else {}
    decision = {
        "intent": handler_payload.get("intent", intent),
        "why_this_action": handler_payload.get("why_this_action", "Deterministic route matched."),
    }

    summary = str(handler_payload.get("response") or "Response generated.").strip()[:160]
    ai_reply = str(handler_payload.get("response") or "").strip() or "I processed your request."

    progress_summary = data.get("progress_summary") if isinstance(data.get("progress_summary"), dict) else {}
    current_plans = {
        "workout": _fetch_latest_plan(request.user_id, "workoutPlans"),
        "nutrition": _fetch_latest_plan(request.user_id, "nutritionPlans"),
    }

    response = AgentResponse(
        summary=summary,
        conversation_id=conversation_id,
        ai_reply=ai_reply,
        actions=actions,
        nudges=[],
        observed_state={"intent": intent},
        decision=decision,
        current_plans=current_plans,
        progress_summary=progress_summary,
        structured_logs={},
        plan_updates={},
        weekly_reflection={},
        trace=trace,
    )

    _record_agent_event(
        request.user_id,
        {
            "conversation_id": conversation_id,
            "mode": request.mode,
            "message": request.message,
            "summary": summary,
            "ai_reply": ai_reply,
            "intent": intent,
            "type": "agent_router",
            "actions": actions,
            "decision": {
                **decision,
                "data_keys": list(data.keys()),
            },
            "data": data,
        },
    )

    _append_conversation_message(
        request.user_id,
        conversation_id,
        "assistant",
        ai_reply,
        payload={
            "summary": summary,
            "actions": actions,
            "decision": decision,
            "data": data,
            "trace": [step.model_dump() for step in trace],
        },
    )

    return response
