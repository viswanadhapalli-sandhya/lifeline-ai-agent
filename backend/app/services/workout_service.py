import json
from app.core.groq_client import generate_ai_response
from app.core.firebase_client import db
from app.services.risk_engine import simple_risk_engine


# --------------------------------------------------
# FETCH USER DATA FROM FIREBASE
# --------------------------------------------------

def get_user_from_firebase(user_id: str):
    doc = db.collection("users").document(user_id).get()

    if not doc.exists:
        raise ValueError("User not found in Firebase")

    return doc.to_dict()

import re
import json

def safe_json_loads(text: str):
    """
    Extracts the first valid JSON object from text and parses it safely.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON block
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError("AI response is not involved JSON")


# --------------------------------------------------
# ANALYZE USER (USING FIREBASE DATA)
# --------------------------------------------------

def analyze_user(user_id: str):
    user = get_user_from_firebase(user_id)
    risk = simple_risk_engine(user)

    prompt = f"""
You are Lifeline AI, a wellness assistant.
This is NOT medical advice.

User data:
Age: {user.get('age')}
Gender: {user.get('gender')}
BMI: {risk['bmi']}
Sleep: {user.get('sleep')}
Exercise: {user.get('exercise')}
Stress: {user.get('stress')}
Smoking: {user.get('smoking')}
Alcohol: {user.get('alcohol')}
Medical history: {user.get('medical')}

Risk score: {risk['risk_score']} / 100
Risk level: {risk['risk_level']}
Contributing factors: {risk['contributing_factors']}

Return STRICT JSON only with keys:
risk_summary
risk_factors
workout_plan_summary
nutrition_plan_summary
daily_spark
"""

    try:
        ai_response = generate_ai_response(prompt)
        return json.loads(ai_response)

    except Exception:
        return {
            "risk_summary": f"Your health risk level is {risk['risk_level']}.",
            "risk_factors": risk["contributing_factors"],
            "workout_plan_summary": "Start with 30 minutes of walking daily.",
            "nutrition_plan_summary": "Eat balanced meals with fruits and vegetables.",
            "daily_spark": "Small consistent steps lead to lasting health.",
        }


# --------------------------------------------------
# WORKOUT PLAN PROMPT (DAY-WISE SAFE)
# --------------------------------------------------

def build_workout_prompt(data, day_number: int):
    return f"""
Generate ONLY Day {day_number} of a workout plan.
Respond in STRICT JSON only.
No markdown. No explanations.

User details:
- Goal: {data.goal}
- Location: {data.location}
- Time per day: {data.time_per_day} minutes
- Fitness level: {data.fitness_level}
- Equipment: {data.equipment or "none"}

Return JSON in this EXACT format:
{{
  "day": "Day {day_number}",
  "warmup": ["..."],
  "exercises": [
    {{
      "name": "",
      "sets": 0,
      "reps": "",
      "rest": ""
    }}
  ],
  "cooldown": ["..."],
  "tip": ""
}}
"""



# --------------------------------------------------
# GENERATE FULL 7-DAY WORKOUT PLAN
# --------------------------------------------------

def generate_workout_plan(data):
    print("🔥 NEW WORKOUT SERVICE RUNNING 🔥")

    weekly_plan = []

    for day in range(1, 8):   # 🔁 LOOP FOR 7 DAYS
        try:
            prompt = build_workout_prompt(data, day)
            ai_response = generate_ai_response(prompt).strip()

            # Remove markdown if present
            if ai_response.startswith("```"):
                ai_response = ai_response.replace("```json", "").replace("```", "").strip()

            day_plan = json.loads(ai_response)
            weekly_plan.append(day_plan)

        except Exception as e:
            print(f"❌ ERROR GENERATING DAY {day}:", e)

            # Fallback for this specific day
            weekly_plan.append({
                "day": f"Day {day}",
                "warmup": ["5 min walking"],
                "exercises": [
                    {
                        "name": "Bodyweight Squats",
                        "sets": 3,
                        "reps": "12",
                        "rest": "60 sec"
                    }
                ],
                "cooldown": ["Light stretching"],
                "tip": "Consistency matters more than intensity."
            })

    return {
        "plan": weekly_plan
    }
