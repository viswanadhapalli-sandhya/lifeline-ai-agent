import json
from app.core.groq_client import generate_ai_response

import re

def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())
        raise

def build_nutrition_prompt(data, day_number: int):
    return f"""
You are a certified Indian nutritionist.

Generate ONLY Day {day_number} of an INDIAN diet plan.
Use common Indian household foods only.
Avoid western dishes.
Respond in STRICT JSON only.
No markdown. No explanations.

User details:
- Goal: {data.goal}
- Dietary preference: {data.diet}
- Activity level: {data.activity}
- Allergies: {data.allergies or "none"}

Return JSON in this EXACT format:
{{
  "day": "Day {day_number}",
  "breakfast": ["..."],
  "lunch": ["..."],
  "snacks": ["..."],
  "dinner": ["..."],
  "tip": ""
}}
"""


def build_weekly_nutrition_prompt(data):
    return f"""
You are a certified Indian nutritionist.

Generate a complete 7-day INDIAN diet plan.
Use common Indian household foods only.
Avoid western dishes.
Respond in STRICT JSON only.
No markdown. No explanations.

User details:
- Goal: {data.goal}
- Dietary preference: {data.diet}
- Activity level: {data.activity}
- Allergies: {data.allergies or "none"}

Return JSON in this EXACT format:
{{
  "plan": [
    {{
      "day": "Day 1",
      "breakfast": ["..."],
      "lunch": ["..."],
      "snacks": ["..."],
      "dinner": ["..."],
      "tip": ""
    }}
  ]
}}
"""


def _normalize_weekly_nutrition_payload(payload):
    plan = payload.get("plan") if isinstance(payload, dict) else None
    if not isinstance(plan, list) or not plan:
        return None

    normalized = []
    for idx, day in enumerate(plan[:7], start=1):
        if not isinstance(day, dict):
            continue
        normalized.append(
            {
                "day": str(day.get("day") or f"Day {idx}"),
                "breakfast": day.get("breakfast") if isinstance(day.get("breakfast"), list) else ["Vegetable oats / idli"],
                "lunch": day.get("lunch") if isinstance(day.get("lunch"), list) else ["Rice, dal, seasonal vegetables"],
                "snacks": day.get("snacks") if isinstance(day.get("snacks"), list) else ["Fruit or roasted chana"],
                "dinner": day.get("dinner") if isinstance(day.get("dinner"), list) else ["Chapati with sabzi"],
                "tip": str(day.get("tip") or "Eat mindfully and stay hydrated."),
            }
        )

    if len(normalized) < 7:
        return None

    return {"plan": normalized}

def generate_nutrition_plan(data):
    print("🥗 NEW NUTRITION SERVICE RUNNING 🥗")

    try:
        weekly_prompt = build_weekly_nutrition_prompt(data)
        weekly_response = generate_ai_response(weekly_prompt).strip()
        if weekly_response.startswith("```"):
            weekly_response = weekly_response.replace("```json", "").replace("```", "").strip()

        weekly_payload = _normalize_weekly_nutrition_payload(safe_json_loads(weekly_response))
        if weekly_payload:
            return weekly_payload
    except Exception as e:
        print("⚠️ WEEKLY NUTRITION FAST PATH FAILED:", e)

    weekly_plan = []

    for day in range(1, 8):   # 🔁 LOOP FOR 7 DAYS
        try:
            prompt = build_nutrition_prompt(data, day)
            ai_response = generate_ai_response(prompt).strip()

            # Remove markdown if present
            if ai_response.startswith("```"):
                ai_response = ai_response.replace("```json", "").replace("```", "").strip()

            day_plan = safe_json_loads(ai_response)
            weekly_plan.append(day_plan)

        except Exception as e:
            print(f"❌ NUTRITION ERROR DAY {day}:", e)

            # Fallback for THIS DAY only
            weekly_plan.append({
                "day": f"Day {day}",
                "breakfast": ["Vegetable oats / idli"],
                "lunch": ["Rice, dal, seasonal vegetables"],
                "snacks": ["Fruit or roasted chana"],
                "dinner": ["Chapati with sabzi"],
                "tip": "Eat mindfully and stay hydrated."
            })

    return {
        "plan": weekly_plan
    }
