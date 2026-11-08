"""Small, reproducible logistic model for prioritising fictional challenge leads."""

from __future__ import annotations

import csv
import math
import random
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA_FILE = Path(__file__).resolve().parent / "data" / "smb_leads.csv"
CATEGORICAL = (
    "lead_source",
    "business_type",
    "segment",
    "city_tier",
    "annual_turnover_band",
    "preferred_language",
    "current_internet",
    "current_provider",
)
NUMERIC = ("employee_count", "num_sites", "monthly_telecom_spend_inr")
BINARY = ("has_gstin", "has_udyam")
SEED = 202606


def _number(value: str) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _yes(value: str) -> float:
    return float(str(value).strip().casefold() in {"y", "yes", "1", "true"})


def _auc(labels: np.ndarray, scores: np.ndarray) -> float:
    pairs = sorted(zip(scores.tolist(), labels.tolist()), key=lambda item: item[0])
    positives = int(labels.sum())
    negatives = len(labels) - positives
    if not positives or not negatives:
        return 0.5
    positive_rank_sum = 0.0
    index = 0
    while index < len(pairs):
        end = index + 1
        while end < len(pairs) and pairs[end][0] == pairs[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2
        positive_rank_sum += average_rank * sum(label for _, label in pairs[index:end])
        index = end
    return (positive_rank_sum - positives * (positives + 1) / 2) / (
        positives * negatives
    )


def _vector(
    row: dict[str, str],
    levels: dict[str, list[str]],
    means: np.ndarray,
    scales: np.ndarray,
) -> list[float]:
    result = [1.0]
    for field in CATEGORICAL:
        value = row.get(field, "")
        result.extend(float(value == category) for category in levels[field][1:])
    numeric = [_number(row.get(field, "")) for field in NUMERIC]
    result.extend((np.asarray(numeric, dtype=float) - means) / scales)
    result.extend(_yes(row.get(field, "")) for field in BINARY)
    return result


@lru_cache(maxsize=1)
def fit_and_score() -> dict:
    """Train on a fixed 80% sample and score the untouched 20% holdout."""
    with DATA_FILE.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    labels = np.asarray(
        [int(row.get("outcome") == "Activated") for row in rows], dtype=float
    )
    shuffled = list(range(len(rows)))
    random.Random(SEED).shuffle(shuffled)
    holdout_count = max(1, round(len(rows) * 0.20))
    holdout = shuffled[:holdout_count]
    training = shuffled[holdout_count:]
    levels = {
        field: sorted({row.get(field, "") for row in (rows[i] for i in training)})
        for field in CATEGORICAL
    }
    raw_numeric = np.asarray(
        [
            [_number(rows[index].get(field, "")) for field in NUMERIC]
            for index in training
        ]
    )
    means = raw_numeric.mean(axis=0)
    scales = raw_numeric.std(axis=0)
    scales[scales == 0] = 1.0
    matrix = np.asarray(
        [_vector(row, levels, means, scales) for row in rows], dtype=float
    )
    x_train, y_train = matrix[training], labels[training]
    x_holdout, y_holdout = matrix[holdout], labels[holdout]

    weights = np.zeros(matrix.shape[1], dtype=float)
    prevalence = min(max(float(y_train.mean()), 1e-5), 1 - 1e-5)
    weights[0] = math.log(prevalence / (1 - prevalence))
    for _ in range(320):
        logits = np.clip(x_train @ weights, -30, 30)
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        gradient = (x_train.T @ (probabilities - y_train)) / len(y_train)
        gradient[1:] += 0.015 * weights[1:]
        weights -= 0.35 * gradient

    all_scores = 1.0 / (1.0 + np.exp(-np.clip(matrix @ weights, -30, 30)))
    holdout_scores = all_scores[holdout]
    return {
        "synthetic": True,
        "model": "logistic-regression-v1",
        "seed": SEED,
        "rows": len(rows),
        "training_rows": len(training),
        "heldout_rows": len(holdout),
        "heldout_auc": round(_auc(y_holdout, holdout_scores), 3),
        "positive_label": "Activated",
        "features": list(CATEGORICAL + NUMERIC + BINARY),
        "scores": all_scores.tolist(),
        "records": rows,
        "_fit": (levels, means, scales, weights),
    }


def handoff_priority(limit: int = 8) -> dict:
    result = fit_and_score()
    eligible = [
        index
        for index, row in enumerate(result["records"])
        if row.get("outcome") == "Dropped" and rescuable(row)
    ]
    selected = sorted(
        eligible, key=lambda index: result["scores"][index], reverse=True
    )[:limit]
    prospects = []
    for index in selected:
        row = result["records"][index]
        prospects.append(
            {
                "lead_id": row["lead_id"],
                "lead_source": row["lead_source"],
                "business_type": row["business_type"],
                "city": row["city"],
                "stage_reached": row["stage_reached"],
                "drop_reason": row["drop_reason"],
                "action": recommended_action(row["drop_reason"]),
                "propensity_score": round(result["scores"][index], 3),
            }
        )
    return {
        key: value
        for key, value in result.items()
        if key not in {"scores", "records", "_fit"}
    } | {"prospects": prospects}


def rescuable(row):
    reason = row.get("drop_reason", "").casefold()
    return not any(
        word in reason
        for word in (
            "wrong number",
            "invalid number",
            "unreachable",
            "not interested",
            "not reachable",
            "could not reach",
            "cannot reach",
            "uncontactable",
            "could not be reached",
        )
    )


def recommended_action(reason):
    text = reason.casefold()
    if "feasible" in text or "site_survey" in text:
        return "Check Biz 5G AirFiber 100 coverage and offer a site survey."
    if "install" in text:
        return "Offer an installation slot and a simulated status update."
    if "payment" in text:
        return "Retry the simulated payment on the accepted quote."
    if "price" in text or "competitor" in text:
        return "Compare the 24-month price with the shorter contract."
    if "gst" in text:
        return "Offer GSTIN-free products and assisted registration support."
    if "kyc" in text or "document" in text or "paperwork" in text:
        return "Arrange a consented document review."
    return "Resume discovery with the customer and confirm their needs."


def live_score(profile, source):
    levels, means, scales, weights = fit_and_score()["_fit"]
    segments = {
        "prof": "Professional offices",
        "micro": "Micro retail & services",
        "multi": "Small multi-site businesses",
        "mfg": "Small manufacturers",
    }
    languages = {
        "en": "English",
        "hi": "Hindi",
        "mr": "Marathi",
        "ta": "Tamil",
        "te": "Telugu",
        "gu": "Gujarati",
        "kn": "Kannada",
    }
    record = dict(
        profile,
        lead_source=source,
        segment=segments.get(profile.get("segment"), profile.get("segment", "")),
        preferred_language=languages.get(profile.get("language"), ""),
        city_tier=str(profile.get("city_tier") or ""),
        employee_count=profile.get("employees") or means[0],
        num_sites=profile.get("sites") or means[1],
        monthly_telecom_spend_inr=means[2],
        has_gstin="Y" if profile.get("gstin") else "N",
        has_udyam="Y" if profile.get("udyam_number") else "N",
    )
    logit = np.clip(np.dot(_vector(record, levels, means, scales), weights), -30, 30)
    return round(float(1 / (1 + np.exp(-logit))), 3)
