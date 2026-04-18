from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import quote_plus


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


def build_nutrition_shopping_plan(
    unavailable_items: List[str],
    available_items: List[str],
    preferred_providers: List[str],
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

    provider_plans: List[Dict[str, Any]] = []
    combined_query = quote_plus(" ".join(unavailable[:8])) if unavailable else quote_plus("healthy groceries")

    for provider in providers:
        pattern = _PROVIDER_URLS[provider]
        item_links = []
        for item in unavailable:
            item_links.append(
                {
                    "item": item,
                    "url": pattern.format(query=quote_plus(item)),
                }
            )

        provider_plans.append(
            {
                "provider": provider,
                "cart_url": pattern.format(query=combined_query),
                "item_links": item_links,
                "status": "cart_ready",
                "note": "Open links to review cart and complete checkout. Automatic checkout is disabled by design.",
            }
        )

    return {
        "pantry": {
            "available_items": available,
            "unavailable_items": unavailable,
            "updated_at": _utc_now_iso(),
        },
        "cart_items": cart_items,
        "provider_plans": provider_plans,
        "requires_user_confirmation": True,
    }
