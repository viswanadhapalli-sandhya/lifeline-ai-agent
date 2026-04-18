from pydantic import BaseModel

class WorkoutRequest(BaseModel):
    user_id: str
    goal: str              # weight loss, muscle gain
    location: str          # home / gym
    time_per_day: int      # minutes
    fitness_level: str     # beginner / intermediate
    equipment: str | None  # dumbbells, none
