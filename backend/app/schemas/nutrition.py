from pydantic import BaseModel
from typing import Optional

class NutritionRequest(BaseModel):
    user_id: str
    goal: str
    diet: str
    activity: str
    allergies: Optional[str] = None
