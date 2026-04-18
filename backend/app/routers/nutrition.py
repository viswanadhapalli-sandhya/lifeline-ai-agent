from fastapi import APIRouter
from firebase_admin import firestore

from app.core.firebase_client import db
from app.schemas.nutrition import NutritionRequest
from app.services.nutrition_service import generate_nutrition_plan

router = APIRouter(prefix="/nutrition", tags=["Nutrition"])

@router.post("/generate")
def generate_nutrition(request: NutritionRequest):
    payload = generate_nutrition_plan(request)

    doc_ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("nutritionPlans")
        .document()
    )
    doc_ref.set(
        {
            "plan": payload.get("plan", []),
            "goal": request.goal,
            "source": "nutrition_endpoint",
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {
        **payload,
        "saved": True,
        "plan_id": doc_ref.id,
    }
