from fastapi import APIRouter
from app.schemas.workout_schema import WorkoutRequest
from app.services.workout_service import generate_workout_plan

router = APIRouter(
    prefix="/workouts",
    tags=["Workouts"]
)

@router.post("/generate")
def generate_workout(request: WorkoutRequest):
    return generate_workout_plan(request)
