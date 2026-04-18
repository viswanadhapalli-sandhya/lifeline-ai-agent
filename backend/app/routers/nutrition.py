from fastapi import APIRouter, HTTPException
from firebase_admin import firestore

from app.core.firebase_client import db
from app.schemas.nutrition import NutritionRequest
from app.schemas.nutrition_shopping import (
    NutritionShoppingConfirmRequest,
    NutritionShoppingPlanRequest,
    PantrySyncRequest,
)
from app.services.nutrition_shopping_service import build_nutrition_shopping_plan
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


@router.post("/pantry/sync")
def sync_pantry(request: PantrySyncRequest):
    pantry_ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("pantry")
        .document("current")
    )

    unavailable = [str(x).strip().lower() for x in request.unavailable_items if str(x).strip()]
    available = [str(x).strip().lower() for x in request.available_items if str(x).strip()]

    pantry_ref.set(
        {
            "available_items": available,
            "unavailable_items": unavailable,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return {
        "ok": True,
        "available_items": available,
        "unavailable_items": unavailable,
    }


@router.post("/shopping/plan")
def build_shopping_plan(request: NutritionShoppingPlanRequest):
    payload = build_nutrition_shopping_plan(
        unavailable_items=request.unavailable_items,
        available_items=request.available_items,
        preferred_providers=request.preferred_providers,
    )

    if not payload.get("cart_items"):
        return {
            "ok": True,
            "message": "No unavailable items found. No cart needed.",
            **payload,
        }

    pantry_ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("pantry")
        .document("current")
    )
    pantry_ref.set(
        {
            "available_items": payload.get("pantry", {}).get("available_items", []),
            "unavailable_items": payload.get("pantry", {}).get("unavailable_items", []),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    shopping_ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("nutritionShoppingPlans")
        .document()
    )
    shopping_ref.set(
        {
            "cart_items": payload.get("cart_items", []),
            "provider_plans": payload.get("provider_plans", []),
            "pantry": payload.get("pantry", {}),
            "status": "pending_confirmation",
            "requires_user_confirmation": True,
            "source": "nutrition_shopping_agent",
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {
        "ok": True,
        "shopping_plan_id": shopping_ref.id,
        **payload,
    }


@router.post("/shopping/confirm")
def confirm_shopping_plan(request: NutritionShoppingConfirmRequest):
    ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("nutritionShoppingPlans")
        .document(request.shopping_plan_id)
    )
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Shopping plan not found")

    current = snap.to_dict() or {}
    provider_plans = current.get("provider_plans") if isinstance(current.get("provider_plans"), list) else []
    provider_exists = any(
        isinstance(item, dict) and str(item.get("provider", "")).lower() == request.provider.lower()
        for item in provider_plans
    )
    if not provider_exists:
        raise HTTPException(status_code=400, detail="Provider not found in shopping plan")

    status = "confirmed" if request.action == "place_order" else "cancelled"

    ref.set(
        {
            "status": status,
            "confirmed_provider": request.provider.lower(),
            "confirmation_action": request.action,
            "confirmedAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    if status == "confirmed":
        plan_items = current.get("items") if isinstance(current.get("items"), list) else []
        if not plan_items:
            cart_items = current.get("cart_items") if isinstance(current.get("cart_items"), list) else []
            plan_items = [
                str(item.get("item", "")).strip().lower()
                for item in cart_items
                if isinstance(item, dict) and str(item.get("item", "")).strip()
            ]

        pantry_ref = (
            db.collection("users")
            .document(request.user_id)
            .collection("pantry")
            .document("current")
        )
        pantry_snap = pantry_ref.get()
        pantry_data = pantry_snap.to_dict() if pantry_snap.exists else {}

        unavailable = pantry_data.get("unavailable_items") if isinstance(pantry_data.get("unavailable_items"), list) else []
        unavailable_set = {str(x).strip().lower() for x in unavailable if str(x).strip()}

        for item in plan_items:
            key = str(item or "").strip().lower()
            if not key:
                continue
            pantry_data[key] = True
            if key in unavailable_set:
                unavailable_set.remove(key)

        pantry_data["unavailable_items"] = sorted(unavailable_set)
        pantry_data["updatedAt"] = firestore.SERVER_TIMESTAMP
        pantry_ref.set(pantry_data, merge=True)

    return {
        "ok": True,
        "shopping_plan_id": request.shopping_plan_id,
        "status": status,
        "provider": request.provider.lower(),
        "next_step": "Open the provider cart links and place the order after review.",
    }
