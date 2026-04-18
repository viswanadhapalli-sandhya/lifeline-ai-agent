from typing import List, Literal

from pydantic import BaseModel, Field


class PantrySyncRequest(BaseModel):
    user_id: str
    available_items: List[str] = Field(default_factory=list)
    unavailable_items: List[str] = Field(default_factory=list)


class NutritionShoppingPlanRequest(BaseModel):
    user_id: str
    unavailable_items: List[str] = Field(default_factory=list)
    available_items: List[str] = Field(default_factory=list)
    preferred_providers: List[str] = Field(default_factory=list)


class NutritionShoppingConfirmRequest(BaseModel):
    user_id: str
    shopping_plan_id: str
    provider: str
    action: Literal["place_order", "cancel"] = "place_order"
