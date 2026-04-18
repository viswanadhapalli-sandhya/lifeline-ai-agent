from app.core.groq_client import generate_ai_response
from firebase_admin import firestore
from app.core.firebase_client import db
import json


def _save_chat_history(user_id: str, user_message: str, assistant_payload: dict):
    db.collection("users").document(user_id).collection("chatHistory").add(
        {
            "message": user_message,
            "assistant": assistant_payload,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "source": "chat_endpoint",
        }
    )


def normalize_ai_response(ai_response: str):
    try:
        cleaned = ai_response.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
    except Exception:
        return {
            "message": ai_response.strip(),
            "suggestions": [],
            "encouragement": None,
        }

    message = (
        parsed.get("message")
        or parsed.get("response")
        or ""
    )

    suggestions = parsed.get("suggestions") or parsed.get("options") or []
    encouragement = parsed.get("encouragement")

    if not message.strip():
        message = "Here’s what I recommend based on your plan:"

    return {
        "message": message.strip(),
        "suggestions": suggestions,
        "encouragement": encouragement,
    }

def chat_with_user_context(user_id: str, message: str):

    # 🔹 Fetch latest workout plan
    workout_docs = (
        db.collection("users")
        .document(user_id)
        .collection("workoutPlans")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )

    workout = (
        workout_docs[0].to_dict().get("plan")
        if workout_docs
        else "No workout plan available yet."
    )

    # 🔹 Fetch latest nutrition plan
    nutrition_docs = (
        db.collection("users")
        .document(user_id)
        .collection("nutritionPlans")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )

    nutrition = (
        nutrition_docs[0].to_dict().get("plan")
        if nutrition_docs
        else "No nutrition plan available yet."
    )

    # 🔹 Build context-aware prompt
    prompt = f"""
You are Lifeline AI Coach 🤖

You are helping ONE specific user.
Answer in a friendly, simple, non-medical way.

User workout plan:
{workout}

User nutrition plan:
{nutrition}

User question:
{message}

Rules:
- Base answers on user's plans
- Do NOT repeat generic phrases
- Be specific, motivating, and practical
- Do NOT give medical diagnosis
"""

    ai_response = generate_ai_response(prompt)

    # If model returns JSON, parse it safely
    
    normalized = normalize_ai_response(ai_response)

    _save_chat_history(user_id=user_id, user_message=message, assistant_payload=normalized)

    return normalized


