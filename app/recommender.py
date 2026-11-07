"""Explainable, rule-checked bundle recommendations over the supplied catalogue."""
from __future__ import annotations

from typing import Any, Callable

from .needs import NEED_SKUS

CONNECTIVITY = ("P04", "P01", "P02", "P03", "P05", "P06")
TIER_LABELS = {
    "value": "Value connectivity",
    "balanced": "Balanced fit",
    "performance": "Higher capacity",
    "dedicated": "Dedicated bandwidth",
    "required": "Required connection",
    "standalone": "Requested services",
}


def _quantity(product: dict[str, Any], profile: dict[str, Any]) -> tuple[int, str | None]:
    """Use confirmed counts when the catalogue unit matches; disclose every other default."""
    unit = str(product.get("unit") or "unit").casefold()
    if "site" in unit:
        return int(profile.get("sites") or 1), None
    if "user" in unit:
        return int(profile.get("employees") or 1), None
    return 1, f"quantity assumed 1 ({unit.replace('per ', '')}); confirm how many you need"


def _connectivity_variants(
    profile: dict[str, Any],
    wants_internet: bool,
    needs_dependency: bool,
    wants_dedicated: bool,
    catalogue: dict[str, dict[str, Any]],
    check_bundle: Callable,
    price_paise: Callable,
    required_connection_ids: set[str],
) -> list[tuple[str, str | None]]:
    sites = int(profile.get("sites") or 1)

    def eligible(pid: str) -> bool:
        if pid not in catalogue:
            return False
        return not check_bundle(profile, [{"product_id": pid, "quantity": sites}])

    available = [pid for pid in CONNECTIVITY if eligible(pid)]
    available.sort(key=lambda pid: price_paise(catalogue[pid]))
    if not available:
        return [("standalone", None)] if not wants_internet and not needs_dependency else []

    if not wants_internet:
        if wants_dedicated:
            dedicated = next((pid for pid in ("P05", "P06") if pid in available), None)
            return [("dedicated", dedicated)] if dedicated else []
        # A prerequisite is included only when a requested catalogue item requires one.
        if needs_dependency:
            preferred = [pid for pid in available if not required_connection_ids or pid in required_connection_ids]
            return [("required", preferred[0])] if preferred else []
        return [("standalone", None)]

    variants: list[tuple[str, str | None]] = [("value", available[0])]
    balanced = next((pid for pid in ("P02", "P01") if pid in available and pid != available[0]), None)
    if balanced:
        variants.append(("balanced", balanced))
    performance = next((pid for pid in ("P03",) if pid in available and pid not in {value for _, value in variants}), None)
    if performance:
        variants.append(("performance", performance))
    if wants_dedicated:
        dedicated = next((pid for pid in ("P05", "P06") if pid in available), None)
        if dedicated:
            variants.append(("dedicated", dedicated))
    return variants


def build_candidates(
    profile: dict[str, Any],
    catalogue: dict[str, dict[str, Any]],
    check_bundle: Callable,
    price_paise: Callable,
    limit: int = 3,
) -> list[dict[str, Any]]:
    selected_needs = profile.get("needs") or []
    defaulted_internet = not selected_needs
    wants = [need for need in (selected_needs or ["internet"]) if need in NEED_SKUS] or ["internet"]
    must = set(profile.get("must") or [])
    budget = int(profile.get("budget") or 0) * 100
    requested_product_ids = {
        pid for need in wants for pid in NEED_SKUS.get(need, [])
        if pid in catalogue and pid not in CONNECTIVITY
    }
    dependency_sets = [set(catalogue[pid].get("requires_one_of") or []) for pid in requested_product_ids]
    dependency_sets = [dependencies for dependencies in dependency_sets if dependencies]
    required_connection_ids = set.intersection(*dependency_sets) if dependency_sets else set()
    needs_dependency = bool(dependency_sets)
    variants = _connectivity_variants(
        profile,
        wants_internet="internet" in wants,
        needs_dependency=needs_dependency,
        wants_dedicated="dedicated_line" in wants,
        catalogue=catalogue,
        check_bundle=check_bundle,
        price_paise=price_paise,
        required_connection_ids=required_connection_ids,
    )

    options: list[dict[str, Any]] = []
    for tier, connection in variants:
        items: list[dict[str, Any]] = []
        assumptions: list[str] = ["No priority was selected; internet is shown as a starting option."] if defaulted_internet else []
        if connection:
            items.append({"product_id": connection, "quantity": int(profile.get("sites") or 1)})
            if "internet" not in wants and tier != "dedicated":
                assumptions.append(f"{catalogue[connection]['product_name']} is included because a requested service requires connectivity.")

        for need in wants:
            if need in ("internet", "dedicated_line"):
                continue
            for pid in NEED_SKUS.get(need, []):
                if pid in CONNECTIVITY or pid not in catalogue:
                    continue
                quantity, assumption = _quantity(catalogue[pid], profile)
                trial = items + [{"product_id": pid, "quantity": quantity}]
                if not [issue for issue in check_bundle(profile, trial) if issue["product_id"] == pid]:
                    items = trial
                    if assumption:
                        assumptions.append(f"{catalogue[pid]['product_name']}: {assumption}.")
                    break

        selected = {str(item["product_id"]).upper() for item in items}
        unmet = [
            need for need in wants
            if not selected.intersection(NEED_SKUS.get(need, []))
            and not (need == "dedicated_line" and selected.intersection(("P05", "P06")))
        ]
        if check_bundle(profile, items):
            continue
        monthly = sum(price_paise(catalogue[item["product_id"]]) * item["quantity"] for item in items)
        setup = sum(int(catalogue[item["product_id"]]["one_time_setup_inr"]) * 100 * item["quantity"] for item in items)
        options.append({
            "tier": tier,
            "label": TIER_LABELS[tier],
            "items": items,
            "assumptions": assumptions,
            "unmet": unmet,
            "must_unmet": [need for need in unmet if need in must],
            "fits": not budget or monthly <= budget,
            "monthly": monthly,
            "setup": setup,
        })

    # Prefer must-haves, then the most requested needs, then budget fit and lower total cost.
    options.sort(key=lambda option: (
        len(option["must_unmet"]),
        len(option["unmet"]),
        not option["fits"],
        option["setup"],
        option["monthly"],
    ))
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for option in options:
        key = tuple(sorted((item["product_id"], item["quantity"]) for item in option["items"]))
        if key in seen:
            continue
        seen.add(key)
        result.append({key: value for key, value in option.items() if key not in {"must_unmet", "fits", "monthly", "setup", "unmet"}})
        if len(result) >= max(1, limit):
            break
    return result
