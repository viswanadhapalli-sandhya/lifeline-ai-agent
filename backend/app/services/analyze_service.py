import json
from app.core.groq_client import generate_ai_response
from app.services.risk_engine import simple_risk_engine

def analyze_user(req):
    risk = simple_risk_engine(req)

    prompt = f"""
You are Lifeline AI, a wellness assistant.
This is NOT medical advice.

User data:
Age: {req.age}
Gender: {req.gender}
BMI: {risk['bmi']}
Sleep: {req.sleep}
Exercise: {req.exercise}
Stress: {req.stress}
Smoking: {req.smoking}
Alcohol: {req.alcohol}
Medical history: {req.medical}

Risk score: {risk['risk_score']} / 100
Risk level: {risk['risk_level']}
Contributing factors: {risk['contributing_factors']}

Return STRICT JSON only with keys:
risk_summary (string)
risk_factors (array of strings)
workout_plan_summary (string)
nutrition_plan_summary (string)
daily_spark (string)
"""

    try:
        ai_response = generate_ai_response(prompt)
        return json.loads(ai_response)

    except Exception:
        # Safe fallback (demo-proof)
        return {
            "risk_summary": f"Your health risk level is {risk['risk_level']}.",
            "risk_factors": risk["contributing_factors"],
            "workout_plan_summary": "Start with 30 minutes of walking daily.",
            "nutrition_plan_summary": "Eat balanced meals with fruits and vegetables.",
            "daily_spark": "Small consistent steps lead to lasting health.",
        }
