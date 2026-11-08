"""Decide from confirmed evidence only. Missing prerequisites take precedence over review."""

import re
from datetime import date
from .kyc_pipeline import ADDRESS_PROOF_MAX_AGE_DAYS, gstin_valid

TYPES = ("gst_certificate", "udyam_certificate", "signatory_id", "address_proof")


def norm(value):
    return "".join(ch for ch in (value or "").casefold() if ch.isalnum())


def proprietor(legal, trade):
    match = re.match(r"(.+?)\s*\(prop\.?\s+(.+?)\)\s*$", legal, re.I)
    if not match:
        return None
    first, second = match.groups()
    # Fixtures use both person (Prop. business) and business (Prop. person).
    return first if norm(second) == norm(trade) else second


def assess(
    docs,
    gst,
    udyam,
    g,
    u,
    applicants,
    today,
    profile,
    quote,
    consent,
    duplicate,
    accepted_suggestion,
    legal_name,
):
    missing, review, notes = [], [], []
    selected = {doc["doc_type"]: doc for doc in docs if doc.get("doc_type") in TYPES}
    if not consent:
        missing.append("KYC consent is required")
    if not quote:
        missing.append("A current accepted quote is required")
    for kind in TYPES:
        if kind not in selected:
            missing.append("Missing document: " + kind.replace("_", " "))
    for kind, subject in (
        ("address_proof", "billed_to"),
        ("signatory_id", "business"),
        ("udyam_certificate", "enterprise_name"),
    ):
        if kind in selected and not selected[kind].get("fields", {}).get(subject):
            missing.append("Business name unreadable on " + kind.replace("_", " "))
    if not g or not gstin_valid(gst) or g["status"] != "Active":
        missing.append("Active, checksum-valid GST registration is required")
    if not u:
        missing.append("Valid Udyam registration is required")
    cert = selected.get("gst_certificate", {})
    if cert:
        if cert.get("gstin_suggestion") == gst and gst:
            if accepted_suggestion:
                review.append(
                    "The checksum-invalid document value differs from the registry; human review required"
                )
            else:
                missing.append("Confirm the suggested GSTIN to request human review")
        elif gst not in cert.get("gstin_candidates", []):
            review.append(
                "GST certificate identifier differs from the selected registration"
            )
        if g and norm(cert.get("fields", {}).get("legal_name")) != norm(
            g["legal_name"]
        ):
            review.append(
                "GST certificate legal name differs from the selected GST registry record; human review required"
            )
    ud = selected.get("udyam_certificate", {})
    if ud and udyam not in ud.get("udyam_candidates", []):
        missing.append("Udyam certificate must show the matching registration number")
    bill = selected.get("address_proof")
    if bill:
        try:
            age = (today - date.fromisoformat(bill["fields"]["bill_date"])).days
        except (KeyError, TypeError, ValueError):
            age = None
        if age is None or not 0 <= age <= ADDRESS_PROOF_MAX_AGE_DAYS:
            missing.append(
                "A readable address proof dated within the last 90 days is required"
            )
    sign = selected.get("signatory_id")
    if sign:
        fields = sign.get("fields", {})
        try:
            valid_until = date.fromisoformat(fields["valid_till"])
        except (KeyError, TypeError, ValueError):
            valid_until = None
        if valid_until is None or valid_until < today:
            missing.append("Current, readable signatory ID is required")
        name = norm(fields.get("name"))
        if (
            not name
            or not applicants
            or any(norm(r["contact_name"]) != name for r in applicants)
        ):
            review.append("Signatory name does not match the applicant on file")
        if g and g["constitution"].casefold() == "proprietorship":
            owner = proprietor(g["legal_name"], g["trade_name"])
            if not owner or norm(owner) != name:
                review.append("Signatory does not match the registered proprietor")
        if any(r["contact_role"].casefold() == "office admin" for r in applicants):
            notes.append(
                "Applicant designation is office admin; designation alone does not block verification."
            )
    if g:
        allowed = {norm(g["legal_name"]), norm(g["trade_name"])}
        if u and norm(u["enterprise_name"]) not in allowed:
            review.append("Registry business names differ")
        for doc in selected.values():
            for field in ("enterprise_name", "business", "billed_to", "trade_name"):
                val = doc.get("fields", {}).get(field)
                if val and norm(val) not in allowed:
                    review.append(
                        "Business name differs across the submitted documents"
                    )
        if legal_name and norm(legal_name) not in allowed:
            review.append("Entered legal name differs from the registry")
    for key in ("gstin", "udyam_number"):
        if profile.get(key) and profile[key] != (gst if key == "gstin" else udyam):
            missing.append("Profile identity differs from the verification request")
    if applicants and not any(
        all(
            str(profile.get(k)) == r[f]
            for k, f in (
                ("employees", "employee_count"),
                ("sites", "num_sites"),
                ("city_tier", "city_tier"),
            )
        )
        for r in applicants
    ):
        review.append(
            "Declared employees, sites or city tier differ from the matched synthetic application"
        )
    if duplicate:
        review.append("Document or registration reused across journeys")
    reasons = list(dict.fromkeys(missing + review))
    return (
        ("ACTION_REQUIRED" if missing else "REVIEW_REQUIRED" if review else "VERIFIED"),
        reasons,
        notes,
    )
