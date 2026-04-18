from fastapi import APIRouter
from firebase_admin import firestore

from app.core.firebase_client import db
from app.schemas.workout_schema import WorkoutRequest
from app.services.workout_service import generate_workout_plan

router = APIRouter(
    prefix="/workouts",
    tags=["Workouts"]
)

@router.post("/generate")
def generate_workout(request: WorkoutRequest):
    payload = generate_workout_plan(request)

    doc_ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("workoutPlans")
        .document()
    )
    doc_ref.set(
        {
            "plan": payload.get("plan", []),
            "goal": request.goal,
            "source": "workout_endpoint",
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {
        **payload,
        "saved": True,
        "plan_id": doc_ref.id,
    }
