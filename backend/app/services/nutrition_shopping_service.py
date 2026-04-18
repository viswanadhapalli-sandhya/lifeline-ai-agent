from __future__ import annotations

import json
import random
import re
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Set
from urllib.parse import quote_plus

from firebase_admin import firestore

from app.core.firebase_client import db


DEFAULT_PROVIDERS = ["blinkit", "zepto", "swiggy_instamart", "amazon", "bigbasket"]

_PROVIDER_URLS = {
    "amazon": "https://www.amazon.in/s?k={query}",
    "blinkit": "https://blinkit.com/s/?q={query}",
    "swiggy_instamart": "https://www.swiggy.com/instamart/search?query={query}",
    "zepto": "https://www.zeptonow.com/search?query={query}",
    "bigbasket": "https://www.bigbasket.com/ps/?q={query}",
}

_PROVIDER_ALIASES = {
    "amazon": "amazon",
    "amazon fresh": "amazon",
    "blinkit": "blinkit",
    "instamart": "swiggy_instamart",
    "swiggy": "swiggy_instamart",
    "swiggy instamart": "swiggy_instamart",
    "zepto": "zepto",
    "bigbasket": "bigbasket",
    "bb": "bigbasket",
}

_ITEM_DEFAULT_QTY = {
    "oats": "1 pack",
    "eggs": "12 pcs",
    "curd": "500 g",
    "paneer": "500 g",
    "chicken": "500 g",
    "rice": "2 kg",
    "dal": "1 kg",
    "roti": "whole wheat flour 2 kg",
    "atta": "2 kg",
    "milk": "1 L",
    "fruits": "1 kg assorted",
    "banana": "1 dozen",
    "apple": "1 kg",
    "nuts": "250 g",
    "roasted chana": "500 g",
    "makhana": "250 g",
}

_PRICE_CATALOG_PATH = Path(__file__).with_name("nutrition_price_catalog.json")
_DEFAULT_ITEM_PRICE = 110
_DEFAULT_ITEM_COVERAGE_DAYS = 2
_MAX_COVERAGE_DAYS = 30

_SWAP_SUGGESTIONS = {
    "almonds": {"to": "peanuts", "save": 240},
    "makhana": {"to": "roasted chana", "save": 90},
    "apple": {"to": "banana", "save": 120},
}

_PROVIDER_COST_FACTORS = {
    "zepto": 0.98,
    "blinkit": 1.0,
    "swiggy_instamart": 1.02,
    "bigbasket": 0.95,
    "amazon": 0.9,
}

_PROVIDER_DELIVERY_MINUTES = {
    "zepto": 10,
    "blinkit": 8,
    "swiggy_instamart": 12,
    "bigbasket": 180,
    "amazon": 1440,
}

_PROVIDER_SELECTION_REASON = "Chosen for best balance of cost and delivery speed"

_PROTEIN_TOKENS = {
    "eggs", "egg", "paneer", "chicken", "fish", "tofu", "dal", "lentil", "curd", "milk", "nuts", "peanut"
}
_GRAIN_TOKENS = {
    "rice", "roti", "chapati", "atta", "oats", "poha", "millet", "quinoa", "bread"
}

_ITEM_SUBSTITUTES = {
    "eggs": "paneer bhurji",
    "chicken": "paneer curry",
    "fish": "tofu stir fry",
    "paneer": "boiled chana",
    "rice": "millet khichdi",
    "oats": "poha",
    "milk": "curd",
}

CITY_TIERS = {
    "hyderabad": "tier1",
    "bangalore": "tier1",
    "mumbai": "tier1",
    "delhi": "tier1",
    "vizag": "tier2",
    "vijayawada": "tier2",
}

TIER_MULTIPLIER = {
    "tier1": 1.15,
    "tier2": 1.0,
    "tier3": 0.9,
}

ITEM_CATEGORY = {
    "rice": "staple",
    "dal": "staple",
    "atta": "staple",
    "oats": "staple",
    "paneer": "protein",
    "chicken": "protein",
    "eggs": "protein",
    "fish": "protein",
    "tofu": "protein",
    "curd": "dairy",
    "milk": "dairy",
    "apple": "fruit",
    "banana": "fruit",
    "fruits": "fruit",
    "almonds": "nuts",
    "nuts": "nuts",
    "peanuts": "nuts",
    "makhana": "nuts",
    "roasted chana": "nuts",
    "broccoli": "vegetable",
    "spinach": "vegetable",
    "sweet potato": "vegetable",
    "vegetables": "vegetable",
}

CATEGORY_PRICES = {
    "vegetable": {"zepto": 48, "blinkit": 50, "swiggy_instamart": 52, "amazon": 45, "bigbasket": 47},
    "staple": {"zepto": 82, "blinkit": 85, "swiggy_instamart": 88, "amazon": 78, "bigbasket": 80},
    "protein": {"zepto": 130, "blinkit": 136, "swiggy_instamart": 140, "amazon": 124, "bigbasket": 128},
    "dairy": {"zepto": 70, "blinkit": 72, "swiggy_instamart": 74, "amazon": 66, "bigbasket": 68},
    "fruit": {"zepto": 95, "blinkit": 98, "swiggy_instamart": 101, "amazon": 90, "bigbasket": 93},
    "nuts": {"zepto": 165, "blinkit": 172, "swiggy_instamart": 178, "amazon": 158, "bigbasket": 162},
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_items(items: List[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()

    for raw in items or []:
        item = str(raw or "").strip().lower()
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return normalized


@lru_cache(maxsize=1)
def _load_price_catalog() -> Dict[str, Dict[str, int]]:
    try:
        raw = _PRICE_CATALOG_PATH.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(parsed, dict):
        return {}

    catalog: Dict[str, Dict[str, int]] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        normalized_key = key.strip().lower()
        if not normalized_key:
            continue

        price = value.get("price")
        coverage_days = value.get("coverage_days")
        if not isinstance(price, int) or price <= 0:
            continue

        catalog[normalized_key] = {
            "price": price,
            "coverage_days": coverage_days if isinstance(coverage_days, int) and coverage_days > 0 else _DEFAULT_ITEM_COVERAGE_DAYS,
        }

    return catalog


def _resolve_item_meta(item: str, catalog: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    normalized_item = str(item or "").strip().lower()
    if not normalized_item:
        return {"price": _DEFAULT_ITEM_PRICE, "coverage_days": _DEFAULT_ITEM_COVERAGE_DAYS}

    if normalized_item in catalog:
        return catalog[normalized_item]

    for key, value in catalog.items():
        if key in normalized_item:
            return value

    return {"price": _DEFAULT_ITEM_PRICE, "coverage_days": _DEFAULT_ITEM_COVERAGE_DAYS}


def _estimate_cost_and_coverage(unavailable: List[str], available: List[str]) -> Dict[str, Any]:
    catalog = _load_price_catalog()

    estimated_cost = 0
    cart_items_meta: Dict[str, Dict[str, int]] = {}
    for item in unavailable:
        meta = _resolve_item_meta(item, catalog)
        cart_items_meta[item] = meta
        estimated_cost += meta["price"]

    combined_items = _normalize_items([*available, *unavailable])
    total_coverage_points = 0
    for item in combined_items:
        total_coverage_points += _resolve_item_meta(item, catalog)["coverage_days"]

    coverage_days = min(_MAX_COVERAGE_DAYS, max(0, round(total_coverage_points / 3)))

    suggestions: List[str] = []
    unavailable_set = set(unavailable)
    for source, suggestion in _SWAP_SUGGESTIONS.items():
        if source in unavailable_set and suggestion["to"] not in unavailable_set:
            suggestions.append(
                f"Swap {source} -> {suggestion['to']} to save Rs {suggestion['save']}"
            )

    if estimated_cost > 1800:
        suggestions.append("Split purchases across two orders to avoid overbuying perishables")
    if not suggestions and unavailable:
        suggestions.append("Try store brand options for staples to reduce total basket cost")

    return {
        "missing_ingredients": unavailable,
        "estimated_cost": estimated_cost,
        "coverage_days": coverage_days,
        "suggestions": suggestions,
        "cost_breakdown": [
            {"item": item, "estimated_price": meta["price"]}
            for item, meta in cart_items_meta.items()
        ],
    }


def _normalize_providers(providers: List[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()

    source = providers if providers else DEFAULT_PROVIDERS

    for raw in source:
        key = str(raw or "").strip().lower()
        if not key:
            continue
        resolved = _PROVIDER_ALIASES.get(key)
        if not resolved or resolved in seen:
            continue
        if resolved not in _PROVIDER_URLS:
            continue
        seen.add(resolved)
        normalized.append(resolved)

    return normalized or DEFAULT_PROVIDERS.copy()


def _estimate_quantity(item: str) -> str:
    for token, qty in _ITEM_DEFAULT_QTY.items():
        if token in item:
            return qty
    return "1 unit"


def _flatten_nutrition_plan_items(plan: Any) -> List[str]:
    flattened: List[str] = []
    if not isinstance(plan, list):
        return flattened

    for day in plan:
        if not isinstance(day, dict):
            continue
        for meal_key in ("breakfast", "lunch", "snacks", "dinner"):
            values = day.get(meal_key)
            if not isinstance(values, list):
                continue
            for entry in values:
                text = str(entry or "").strip().lower()
                if text:
                    flattened.append(text)
    return flattened


def _extract_key_ingredients_from_plan(plan: Any) -> Set[str]:
    ingredients: Set[str] = set()
    for text in _flatten_nutrition_plan_items(plan):
        tokens = re.findall(r"[a-zA-Z]+", text.lower())
        for token in tokens:
            if token in _PROTEIN_TOKENS or token in _GRAIN_TOKENS:
                ingredients.add(token)
    return ingredients


def _fetch_latest_nutrition_plan(user_id: str) -> List[Dict[str, Any]]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("nutritionPlans")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )
    if not docs:
        return []
    payload = docs[0].to_dict() or {}
    plan = payload.get("plan")
    return plan if isinstance(plan, list) else []


def _fetch_pantry_unavailable(user_id: str) -> Set[str]:
    snap = (
        db.collection("users")
        .document(user_id)
        .collection("pantry")
        .document("current")
        .get()
    )
    if not snap.exists:
        return set()

    pantry = snap.to_dict() or {}
    unavailable = pantry.get("unavailable_items") if isinstance(pantry.get("unavailable_items"), list) else []
    unavailable_set = {str(item).strip().lower() for item in unavailable if str(item).strip()}

    for key, value in pantry.items():
        if isinstance(value, bool) and value is False:
            normalized = str(key).strip().lower()
            if normalized:
                unavailable_set.add(normalized)

    return unavailable_set


def proactive_shopping_check(user_id: str) -> Dict[str, Any]:
    nutrition_plan = _fetch_latest_nutrition_plan(user_id)
    key_ingredients = _extract_key_ingredients_from_plan(nutrition_plan)
    unavailable = _fetch_pantry_unavailable(user_id)

    missing_items = sorted(
        item for item in key_ingredients
        if any(item in unavailable_item or unavailable_item in item for unavailable_item in unavailable)
    )

    if missing_items:
        return {
            "type": "proactive_suggestion",
            "message": "You're missing key ingredients for upcoming meals. Want me to prepare a shopping list?",
            "items": missing_items,
        }

    return {
        "type": "proactive_suggestion",
        "message": "Your pantry looks ready for upcoming meals.",
        "items": [],
    }


def generate_reason(best_provider: Dict[str, Any], context: Dict[str, Any]) -> str:
    delivery_time = int(best_provider.get("delivery_time") or 0)
    cost = int(best_provider.get("cost") or 0)
    budget = context.get("budget")

    if delivery_time < 15:
        return "Chosen for fastest delivery to keep your meals on track"
    if isinstance(budget, int) and cost <= budget:
        return "Chosen to fit your budget"
    return "Balanced choice between cost and delivery speed"


def get_shopping_followup(user_id: str, shopping_plan_id: str) -> Dict[str, Any]:
    snap = (
        db.collection("users")
        .document(user_id)
        .collection("nutritionShoppingPlans")
        .document(shopping_plan_id)
        .get()
    )
    if not snap.exists:
        return {"message": "Shopping plan not found", "remaining_items": [], "completed": False}

    payload = snap.to_dict() or {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    added = payload.get("added_items") if isinstance(payload.get("added_items"), list) else []

    item_set = {str(item).strip().lower() for item in items if str(item).strip()}
    added_set = {str(item).strip().lower() for item in added if str(item).strip()}
    remaining = sorted(item_set - added_set)

    if item_set and remaining:
        return {
            "message": f"You've added {len(added_set)}/{len(item_set)} items. Want a substitute for the remaining items?",
            "remaining_items": remaining,
            "completed": False,
        }

    return {
        "message": "Great work. Shopping checklist is complete.",
        "remaining_items": [],
        "completed": True,
    }


def _fetch_shopping_plan(user_id: str, shopping_plan_id: str = "") -> Dict[str, Any]:
    plans_col = db.collection("users").document(user_id).collection("nutritionShoppingPlans")

    if str(shopping_plan_id or "").strip():
        snap = plans_col.document(shopping_plan_id).get()
        if not snap.exists:
            return {}
        payload = snap.to_dict() or {}
        payload["_id"] = snap.id
        return payload

    docs = plans_col.order_by("updatedAt", direction=firestore.Query.DESCENDING).limit(1).get()
    if not docs:
        return {}

    payload = docs[0].to_dict() or {}
    payload["_id"] = docs[0].id
    return payload


def get_agentic_healthcheck(user_id: str, shopping_plan_id: str = "") -> Dict[str, Any]:
    proactive = proactive_shopping_check(user_id)
    shopping_plan = _fetch_shopping_plan(user_id, shopping_plan_id)

    resolved_plan_id = str(shopping_plan.get("_id") or "").strip()
    followup = (
        get_shopping_followup(user_id, resolved_plan_id)
        if resolved_plan_id
        else {"message": "No shopping plan found for follow-up", "remaining_items": [], "completed": False}
    )

    items = shopping_plan.get("items") if isinstance(shopping_plan.get("items"), list) else []
    if not items:
        cart_items = shopping_plan.get("cart_items") if isinstance(shopping_plan.get("cart_items"), list) else []
        items = [
            str(item.get("item", "")).strip().lower()
            for item in cart_items
            if isinstance(item, dict) and str(item.get("item", "")).strip()
        ]

    added_items = shopping_plan.get("added_items") if isinstance(shopping_plan.get("added_items"), list) else []
    missing_items = sorted(
        {str(item).strip().lower() for item in items if str(item).strip()} -
        {str(item).strip().lower() for item in added_items if str(item).strip()}
    )

    best_provider = shopping_plan.get("best_provider") if isinstance(shopping_plan.get("best_provider"), dict) else {}
    budget = shopping_plan.get("budget")
    reason = shopping_plan.get("reason") if isinstance(shopping_plan.get("reason"), str) else ""
    if best_provider and not reason:
        reason = generate_reason(best_provider, {"budget": budget if isinstance(budget, int) else None})

    return {
        "ok": True,
        "shopping_plan_id": resolved_plan_id,
        "proactive_trigger": {
            "at_risk": bool(proactive.get("items")),
            "message": proactive.get("message", ""),
            "items": proactive.get("items", []),
        },
        "followup_loop": {
            "message": followup.get("message", ""),
            "remaining_items": followup.get("remaining_items", []),
            "completed": bool(followup.get("completed", False)),
            "added_items_count": len(added_items),
            "items_count": len(items),
        },
        "outcome_awareness": {
            "ready_to_adjust": bool(missing_items),
            "missing_items": missing_items,
            "message": (
                f"Plan can be adjusted because {len(missing_items)} item(s) are still missing"
                if missing_items
                else "No adjustment needed; all tracked items are covered"
            ),
        },
        "multi_objective_reasoning": {
            "best_provider": best_provider,
            "reason": reason,
            "budget": budget,
            "within_budget": shopping_plan.get("within_budget"),
            "estimated_cost": shopping_plan.get("estimated_cost"),
            "delivery_time": shopping_plan.get("delivery_time"),
        },
    }


def adjust_plan_for_missing_items(user_id: str) -> Dict[str, Any]:
    docs = (
        db.collection("users")
        .document(user_id)
        .collection("nutritionShoppingPlans")
        .order_by("updatedAt", direction=firestore.Query.DESCENDING)
        .limit(1)
        .get()
    )
    if not docs:
        return {"message": "No shopping activity found to adjust meals.", "missing_items": []}

    shopping_plan = docs[0].to_dict() or {}
    items = shopping_plan.get("items") if isinstance(shopping_plan.get("items"), list) else []
    if not items:
        cart_items = shopping_plan.get("cart_items") if isinstance(shopping_plan.get("cart_items"), list) else []
        items = [
            str(item.get("item", "")).strip().lower()
            for item in cart_items
            if isinstance(item, dict) and str(item.get("item", "")).strip()
        ]

    added_items = shopping_plan.get("added_items") if isinstance(shopping_plan.get("added_items"), list) else []
    item_set = {str(item).strip().lower() for item in items if str(item).strip()}
    added_set = {str(item).strip().lower() for item in added_items if str(item).strip()}
    missing = sorted(item_set - added_set)

    if not missing:
        return {"message": "No meal adjustment needed. All items were added.", "missing_items": []}

    current_plan = _fetch_latest_nutrition_plan(user_id)
    if not current_plan:
        return {"message": "Could not find nutrition plan to adjust.", "missing_items": missing}

    adjusted_plan: List[Dict[str, Any]] = []
    for day in current_plan:
        safe_day = day.copy() if isinstance(day, dict) else {}
        for meal_key in ("breakfast", "lunch", "snacks", "dinner"):
            meal_entries = safe_day.get(meal_key)
            if not isinstance(meal_entries, list):
                continue

            adjusted_entries: List[str] = []
            for entry in meal_entries:
                text = str(entry or "")
                lowered = text.lower()
                updated_text = text
                for miss in missing:
                    if miss in lowered:
                        replacement = _ITEM_SUBSTITUTES.get(miss, "seasonal vegetables")
                        updated_text = re.sub(re.escape(miss), replacement, updated_text, flags=re.IGNORECASE)
                adjusted_entries.append(updated_text)
            safe_day[meal_key] = adjusted_entries
        adjusted_plan.append(safe_day)

    plan_ref = (
        db.collection("users")
        .document(user_id)
        .collection("nutritionPlans")
        .document()
    )
    plan_ref.set(
        {
            "plan": adjusted_plan,
            "source": "outcome_adjusted_from_shopping",
            "adjustment": {
                "missing_items": missing,
            },
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )

    return {
        "message": f"I adjusted tomorrow's meals since {', '.join(missing)} were not added.",
        "missing_items": missing,
        "adjusted_plan_id": plan_ref.id,
    }


def estimate_price(item: str, provider: str, city: str) -> int:
    normalized_item = str(item or "").strip().lower()
    normalized_provider = str(provider or "").strip().lower()
    normalized_city = str(city or "").strip().lower()

    category = "vegetable"
    for token, resolved_category in ITEM_CATEGORY.items():
        if token in normalized_item:
            category = resolved_category
            break

    base_price = CATEGORY_PRICES.get(category, {}).get(normalized_provider, 50)
    tier = CITY_TIERS.get(normalized_city, "tier2")
    multiplier = TIER_MULTIPLIER.get(tier, 1.0)
    variation = random.uniform(-0.1, 0.1)

    return max(1, int(base_price * multiplier * (1 + variation)))


def extract_budget(message: str) -> int | None:
    match = re.search(r"\d+", str(message or ""))
    if match:
        return int(match.group())
    return None


def _score_provider(provider: Dict[str, Any], budget: int | None = None) -> float:
    cost_weight = 0.6
    time_weight = 0.4

    cost = float(provider.get("cost") or 0)
    delivery_time = float(provider.get("delivery_time") or 0)
    score = (cost * cost_weight) + (delivery_time * time_weight)

    if delivery_time > 120:
        score += 200

    if budget is not None and cost <= float(budget):
        score -= 50

    return score


def _get_provider_search_link(provider: str, item: str) -> str:
    normalized_provider = str(provider or "").strip().lower()
    query = quote_plus(str(item or "").strip() or "healthy groceries")

    if normalized_provider == "zepto":
        return f"https://www.zeptonow.com/search?q={query}"
    if normalized_provider == "blinkit":
        return f"https://blinkit.com/s/?q={query}"
    if normalized_provider in {"swiggy_instamart", "instamart"}:
        return f"https://www.swiggy.com/instamart/search?query={query}"
    if normalized_provider == "bigbasket":
        return f"https://www.bigbasket.com/ps/?q={query}"
    if normalized_provider == "amazon":
        return f"https://www.amazon.in/s?k={query}"

    pattern = _PROVIDER_URLS.get(normalized_provider, "")
    return pattern.format(query=query) if pattern else ""


def _build_provider_options(
    providers: List[str],
    unavailable: List[str],
    city: str,
) -> List[Dict[str, Any]]:
    provider_options: List[Dict[str, Any]] = []

    for provider in providers:
        item_links = []
        for item in unavailable:
            item_links.append(
                {
                    "item": item,
                    "url": _get_provider_search_link(provider=provider, item=item),
                }
            )

        provider_cost = 0
        for item in unavailable:
            provider_cost += estimate_price(item=item, provider=provider, city=city)

        cost_factor = _PROVIDER_COST_FACTORS.get(provider, 1.0)
        provider_cost = max(1, int(round(provider_cost * cost_factor)))
        delivery_time = _PROVIDER_DELIVERY_MINUTES.get(provider, 120)

        provider_options.append(
            {
                "name": provider,
                "cost": provider_cost,
                "delivery_time": delivery_time,
                "link": _get_provider_search_link(provider=provider, item=unavailable[0] if unavailable else "healthy groceries"),
                "item_links": item_links,
                "status": "cart_ready",
                "note": "Open links to review cart and complete checkout. Automatic checkout is disabled by design.",
            }
        )

    return provider_options


def build_nutrition_shopping_plan(
    unavailable_items: List[str],
    available_items: List[str],
    preferred_providers: List[str],
    city: str = "",
    user_message: str = "",
) -> Dict[str, Any]:
    unavailable = _normalize_items(unavailable_items)
    available = _normalize_items(available_items)
    providers = _normalize_providers(preferred_providers)

    cart_items: List[Dict[str, str]] = []
    for item in unavailable:
        cart_items.append(
            {
                "item": item,
                "quantity_hint": _estimate_quantity(item),
                "priority": "high",
            }
        )

    insights = _estimate_cost_and_coverage(unavailable=unavailable, available=available)
    budget = extract_budget(user_message)
    provider_options = _build_provider_options(
        providers=providers,
        unavailable=unavailable,
        city=city,
    )

    best_provider = (
        min(provider_options, key=lambda provider: _score_provider(provider, budget))
        if provider_options
        else None
    )
    alternatives = [provider for provider in provider_options if provider is not best_provider]
    reason = _PROVIDER_SELECTION_REASON
    within_budget = True

    if isinstance(best_provider, dict):
        reason = generate_reason(best_provider, {"budget": budget})

    if isinstance(best_provider, dict) and budget is not None:
        best_provider_cost = int(best_provider.get("cost") or 0)
        within_budget = best_provider_cost <= budget

        if not within_budget:
            reason = "Chosen for best balance of cost and delivery speed; adjusted suggestions for your budget"
            insights["suggestions"] = [
                *insights.get("suggestions", []),
                "Swap almonds -> peanuts",
                "Reduce premium items",
            ]

    return {
        "pantry": {
            "available_items": available,
            "unavailable_items": unavailable,
            "updated_at": _utc_now_iso(),
        },
        "cart_items": cart_items,
        "best_provider": best_provider,
        "alternatives": alternatives,
        "reason": reason,
        "missing_ingredients": insights["missing_ingredients"],
        "estimated_cost": best_provider["cost"] if isinstance(best_provider, dict) else insights["estimated_cost"],
        "delivery_time": best_provider["delivery_time"] if isinstance(best_provider, dict) else None,
        "budget": budget,
        "within_budget": within_budget,
        "city": str(city or "").strip().lower(),
        "coverage_days": insights["coverage_days"],
        "suggestions": insights["suggestions"],
        "cost_breakdown": insights["cost_breakdown"],
        "requires_user_confirmation": True,
    }
