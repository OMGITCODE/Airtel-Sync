"""Coverage for the expanded, deterministic recommendation engine."""
import csv
import os
import pathlib
import random
import tempfile

import pytest
from fastapi import HTTPException

os.environ["ONEJOURNEY_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "recommendations.sqlite3")

from app.main import CAT, GST, calculate, candidates, check_bundle, normalize_need_list, unmet_needs
from app.needs import ALLOWED_NEEDS, NEED_SKUS

DATA = pathlib.Path(__file__).parents[1] / "app" / "data"
ACTIVE_GSTIN = next(record["gstin"] for record in GST.values() if record["status"] == "Active")


def profile(**updates):
    result = {
        "employees": 6,
        "sites": 1,
        "city_tier": 2,
        "budget": 0,
        "gstin": "",
        "needs": ["internet"],
        "must": [],
    }
    result.update(updates)
    return result


def quantities(option):
    return {item["product_id"]: item["quantity"] for item in option["items"]}


def test_every_catalogue_sku_is_mapped_to_a_customer_need():
    assert {sku for skus in NEED_SKUS.values() for sku in skus} == set(CAT)


def test_multi_site_services_use_the_confirmed_site_count():
    p = profile(employees=12, sites=3, city_tier=1, gstin=ACTIVE_GSTIN,
                needs=["internet", "backup", "central_management"])
    options = candidates(p)
    matching = [option for option in options if {"P09", "P10"} <= quantities(option).keys()]
    assert matching
    for option in matching:
        assert quantities(option)["P09"] == 3
        assert quantities(option)["P10"] == 3
        assert calculate(p, option["items"])["valid"]


def test_budget_fit_alternative_is_available_for_a_micro_business():
    p = profile(employees=2, budget=1500, needs=["internet", "mobile"])
    options = candidates(p)
    assert any(calculate(p, option["items"])["recurring_paise"] <= 150000 for option in options)


def test_manufacturer_can_get_requested_dedicated_line_firewall_and_machine_monitoring():
    p = profile(employees=25, city_tier=1, gstin=ACTIVE_GSTIN,
                needs=["internet", "dedicated_line", "firewall", "iot"])
    assert any({"P05", "P21", "P24"} <= quantities(option).keys() for option in candidates(p))


def test_dedicated_line_is_not_an_unrequested_upgrade():
    options = candidates(profile(employees=25, gstin=ACTIVE_GSTIN, needs=["internet", "email"]))
    assert options
    assert all(not ({"P05", "P06"} & quantities(option).keys()) for option in options)


def test_non_connectivity_needs_do_not_add_unrequested_internet():
    options = candidates(profile(needs=["mobile"]))
    assert options
    assert all(not ({"P01", "P02", "P03", "P04", "P05", "P06"} & quantities(option).keys()) for option in options)


def test_unknown_form_values_are_not_silently_discarded_and_aliases_are_normalized():
    assert normalize_need_list(["Wi-Fi", "WhatsApp Business"], "Business priorities") == ["internet", "whatsapp"]
    with pytest.raises(HTTPException):
        normalize_need_list(["made-up priority"], "Business priorities")


def test_assumed_line_quantity_is_disclosed():
    option = candidates(profile(employees=4, needs=["internet", "mobile"]))[0]
    assert any("assumed 1 (line)" in note and "Postpaid" in note for note in option["assumptions"])


def test_must_keep_need_is_never_removed_when_a_valid_option_can_include_it():
    p = profile(gstin=ACTIVE_GSTIN, needs=["internet", "calling", "email"], must=["calling"])
    assert any("P12" in quantities(option) for option in candidates(p))


def test_real_lead_profiles_never_receive_invalid_options_or_hidden_unmet_needs():
    rows = list(csv.DictReader((DATA / "smb_leads.csv").open(encoding="utf-8")))
    random.Random(7).shuffle(rows)
    for row in rows[:600]:
        registry_status = GST.get(row["gstin"], {}).get("status") if row["has_gstin"] == "Y" else None
        rng = random.Random(row["lead_id"])
        needs = ["internet"] + rng.sample([need for need in ALLOWED_NEEDS if need != "internet"], rng.randint(0, 5))
        p = profile(
            employees=int(row["employee_count"]),
            sites=int(row["num_sites"]),
            city_tier=int(row["city_tier"]),
            gstin=row["gstin"] if registry_status == "Active" else "",
            needs=needs,
            budget=rng.choice([0, 2000, 5000]),
        )
        for option in candidates(p):
            assert not check_bundle(p, option["items"]), (row["lead_id"], option)
            assert calculate(p, option["items"])["valid"]
            included = {item["product_id"] for item in option["items"]}
            missing = {need for need in needs if not included.intersection(NEED_SKUS[need])}
            assert {item["need"] for item in unmet_needs(p, option["items"])} == missing, (row["lead_id"], missing)
