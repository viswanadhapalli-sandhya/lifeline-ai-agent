import json
import re
from datetime import datetime, timezone
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


def _upsert_daily_log(user_id: str, updates: Dict[str, Any], date_key: str | None = None) -> None:
    day = date_key or _today_key()
    ref = db.collection("users").document(user_id).collection("dailyLogs").document(day)
    updates = {
        **updates,
        "date": day,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    ref.set(updates, merge=True)


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

    weight_match = re.search(r"(\d+(?:\.\d+)?)\s?kg", lowered)
    if weight_match:
        extracted["weight_kg"] = float(weight_match.group(1))

    workout_match = re.search(r"(\d+)\s?(?:min|mins|minutes).{0,15}(?:workout|exercise|walk|run)", lowered)
    if workout_match:
        extracted["workout_minutes"] = int(workout_match.group(1))

    if any(token in lowered for token in ["ate", "breakfast", "lunch", "dinner", "snack"]):
        extracted["meal_text"] = text

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
    ]
    if any(token in lowered for token in positive_follow_tokens):
        extracted["adherence_status"] = "good"

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
    "missed_workout_items": ["..."],
  "meal_text": string or "",
    "adherence_status": "good" | "poor" | "neutral",
    "travel_disruption": boolean,
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

        missed_workout_items = payload.get("missed_workout_items")
        if isinstance(missed_workout_items, list):
            cleaned["missed_workout_items"] = [str(x).strip() for x in missed_workout_items if str(x).strip()][:5]

        meal_text = payload.get("meal_text")
        if isinstance(meal_text, str) and meal_text.strip():
            cleaned["meal_text"] = meal_text.strip()

        adherence_status = payload.get("adherence_status")
        if adherence_status in {"good", "poor", "neutral"}:
            cleaned["adherence_status"] = adherence_status

        travel_disruption = payload.get("travel_disruption")
        if isinstance(travel_disruption, bool):
            cleaned["travel_disruption"] = travel_disruption

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

        if workout_preview:
            day = workout_preview[0] or {}
            day_name = day.get("day", "Day 1")
            warmup = day.get("warmup") if isinstance(day.get("warmup"), list) else []
            exercises = day.get("exercises") if isinstance(day.get("exercises"), list) else []
            cooldown = day.get("cooldown") if isinstance(day.get("cooldown"), list) else []

            lines.append(f"Workout ({day_name}):")
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
            day = nutrition_preview[0] or {}
            day_name = day.get("day", "Day 1")
            breakfast = day.get("breakfast") if isinstance(day.get("breakfast"), list) else []
            lunch = day.get("lunch") if isinstance(day.get("lunch"), list) else []
            snacks = day.get("snacks") if isinstance(day.get("snacks"), list) else []
            dinner = day.get("dinner") if isinstance(day.get("dinner"), list) else []

            lines.append(f"Meals ({day_name}):")
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

    should_force_plan_format = bool(
        user_intent.get("today_plan_query")
        or (plan_updates.get("workout") is not None)
        or (plan_updates.get("nutrition") is not None)
        or bool(decision.get("user_requests_restructure", False))
        or bool(decision.get("compensation_requested", False))
    )

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


def _summarize_response(decision: Dict[str, Any], nudges: List[str], plan_updates: Dict[str, Any]) -> str:
    parts = []
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
    if structured_logs:
        _upsert_daily_log(request.user_id, structured_logs)
        actions.append("daily_log_updated")
        trace.append(
            AgentStep(
                name="auto_log",
                status="ok",
                detail="Parsed and stored chat-based logs",
                output=structured_logs,
            )
        )
    else:
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
    today_plan_query = bool(llm_brain.get("today_plan_query", False))
    log_summary_query = bool(llm_brain.get("log_summary_query", False))
    travel_disruption = bool(llm_brain.get("travel_disruption", False)) or bool(structured_logs.get("travel_disruption", False))
    compensation_requested = bool(llm_brain.get("compensation_requested", False)) or bool(structured_logs.get("compensation_request", False))
    completion_update = bool(llm_brain.get("completion_update", False)) or (
        inferred_completion_from_logs and not today_plan_query
    )
    food_adapter_needed = llm_brain.get("needs_food_adaptation", False) or ("only have" in message_l)
    food_adapter = _food_reality_adapter(goal, request.message or "") if food_adapter_needed else {}

    llm_wants_refresh = llm_brain.get("should_refresh_plan", False)
    if compensation_requested:
        llm_wants_refresh = True
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
        "compensation_requested": compensation_requested,
        "user_requests_restructure": llm_brain.get("user_requests_restructure", False),
        "should_initialize_plan": not has_existing_plans,
        "should_refresh_plan": (
            llm_wants_refresh
            or bool(drift.get("should_adapt_plan"))
            or request.mode == "plan"
            or not has_existing_plans
            or bool(food_adapter)
            or recovery_mode.get("enabled", False)
        ),
        "weekly_reflection_requested": llm_brain.get("weekly_reflection_requested", False)
        or request.mode == "weekly_reflection"
        or "weekly" in (request.message or "").lower(),
    }
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

        if drift.get("status") == "behind":
            workout_input.time_per_day = min(90, max(20, workout_input.time_per_day + 10))

        plan_updates["workout"] = generate_workout_plan(workout_input)
        plan_updates["nutrition"] = generate_nutrition_plan(nutrition_input)

        if decision.get("should_initialize_plan"):
            reason = "initial_agent_plan"
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
        },
        decision=decision,
        actions=actions,
        nudges=nudges,
        plan_updates=plan_updates,
        current_plans={
            "workout": current_workout,
            "nutrition": current_nutrition,
        },
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
            "structured_logs": structured_logs,
            "plan_updates": plan_updates,
            "weekly_reflection": weekly,
            "trace": [step.model_dump() for step in trace],
        },
    )

    return response
