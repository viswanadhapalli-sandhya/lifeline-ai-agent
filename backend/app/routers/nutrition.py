from fastapi import APIRouter, HTTPException
from firebase_admin import firestore

from app.core.firebase_client import db
from app.schemas.nutrition import NutritionRequest
from app.schemas.nutrition_shopping import (
    NutritionShoppingConfirmRequest,
    NutritionShoppingFollowupRequest,
    NutritionShoppingHealthcheckRequest,
    NutritionShoppingPlanRequest,
    NutritionShoppingProgressRequest,
    NutritionShoppingUserRequest,
    PantrySyncRequest,
)
from app.services.nutrition_shopping_service import (
    adjust_plan_for_missing_items,
    build_nutrition_shopping_plan,
    get_agentic_healthcheck,
    get_shopping_followup,
    proactive_shopping_check,
)
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
        city=request.city,
        user_message=request.user_message,
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
            "items": [
                str(item.get("item", "")).strip().lower()
                for item in payload.get("cart_items", [])
                if isinstance(item, dict) and str(item.get("item", "")).strip()
            ],
            "added_items": [],
            "best_provider": payload.get("best_provider"),
            "alternatives": payload.get("alternatives", []),
            "reason": payload.get("reason", "Best balance of cost and delivery time"),
            "pantry": payload.get("pantry", {}),
            "missing_ingredients": payload.get("missing_ingredients", []),
            "estimated_cost": payload.get("estimated_cost", 0),
            "delivery_time": payload.get("delivery_time"),
            "budget": payload.get("budget"),
            "within_budget": payload.get("within_budget", True),
            "city": payload.get("city", ""),
            "coverage_days": payload.get("coverage_days", 0),
            "suggestions": payload.get("suggestions", []),
            "cost_breakdown": payload.get("cost_breakdown", []),
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


@router.post("/shopping/proactive-check")
def nutrition_proactive_shopping_check(request: NutritionShoppingUserRequest):
    payload = proactive_shopping_check(request.user_id)

    db.collection("users").document(request.user_id).collection("agentEvents").add(
        {
            "type": payload.get("type", "proactive_suggestion"),
            "action": "shopping_risk_detected" if payload.get("items") else "shopping_risk_clear",
            "priority": "high" if payload.get("items") else "low",
            "message": payload.get("message", ""),
            "why_this_action": "Pantry and upcoming meal ingredients indicate potential execution risk.",
            "confidence": 0.84,
            "items": payload.get("items", []),
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {"ok": True, **payload}


@router.post("/shopping/progress")
def update_shopping_progress(request: NutritionShoppingProgressRequest):
    ref = (
        db.collection("users")
        .document(request.user_id)
        .collection("nutritionShoppingPlans")
        .document(request.shopping_plan_id)
    )

    item_set = sorted({str(item).strip().lower() for item in request.items if str(item).strip()})
    added_set = sorted({str(item).strip().lower() for item in request.added_items if str(item).strip()})

    status = "completed" if item_set and len(added_set) >= len(item_set) else "in_progress"

    ref.set(
        {
            "items": item_set,
            "added_items": added_set,
            "status": status,
            "updatedAt": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return {
        "ok": True,
        "shopping_plan_id": request.shopping_plan_id,
        "status": status,
        "added_items_count": len(added_set),
        "items_count": len(item_set),
    }


@router.post("/shopping/followup")
def shopping_followup(request: NutritionShoppingFollowupRequest):
    payload = get_shopping_followup(request.user_id, request.shopping_plan_id)

    db.collection("users").document(request.user_id).collection("agentEvents").add(
        {
            "type": "proactive_suggestion",
            "action": "shopping_followup",
            "priority": "medium",
            "message": payload.get("message", ""),
            "why_this_action": "Partial checklist completion was detected, so a follow-up keeps shopping on track.",
            "confidence": 0.81,
            "remaining_items": payload.get("remaining_items", []),
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {"ok": True, **payload}


@router.post("/shopping/adjust-plan")
def shopping_adjust_plan(request: NutritionShoppingUserRequest):
    payload = adjust_plan_for_missing_items(request.user_id)

    db.collection("users").document(request.user_id).collection("agentEvents").add(
        {
            "type": "proactive_suggestion",
            "action": "nutrition_plan_adjusted_from_shopping",
            "priority": "high" if payload.get("missing_items") else "low",
            "message": payload.get("message", ""),
            "why_this_action": "Missing purchased items can break plan feasibility, so a safe plan adjustment was prepared.",
            "confidence": 0.83,
            "missing_items": payload.get("missing_items", []),
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {"ok": True, **payload}


@router.post("/shopping/agentic-healthcheck")
def shopping_agentic_healthcheck(request: NutritionShoppingHealthcheckRequest):
    payload = get_agentic_healthcheck(
        user_id=request.user_id,
        shopping_plan_id=request.shopping_plan_id,
    )

    db.collection("users").document(request.user_id).collection("agentEvents").add(
        {
            "type": "proactive_suggestion",
            "action": "shopping_agentic_healthcheck",
            "priority": "low",
            "message": "Agentic shopping healthcheck generated",
            "why_this_action": "A consolidated healthcheck helps explain the current agentic shopping state.",
            "confidence": 0.79,
            "shopping_plan_id": payload.get("shopping_plan_id", ""),
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return payload


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
    best_provider = current.get("best_provider") if isinstance(current.get("best_provider"), dict) else {}
    alternatives = current.get("alternatives") if isinstance(current.get("alternatives"), list) else []
    candidate_provider_names = []

    if isinstance(best_provider, dict):
        best_name = str(best_provider.get("name", "")).strip().lower()
        if best_name:
            candidate_provider_names.append(best_name)

    for item in alternatives:
        if not isinstance(item, dict):
            continue
        alt_name = str(item.get("name", "")).strip().lower()
        if alt_name:
            candidate_provider_names.append(alt_name)

    provider_exists = request.provider.lower() in set(candidate_provider_names)
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
