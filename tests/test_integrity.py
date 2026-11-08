from concurrent.futures import ThreadPoolExecutor
import shutil
from fastapi.testclient import TestClient
import pytest
from app import main as m
from app.onboarding import SAMPLE_LEADS

client = TestClient(m.app)
staff = {"X-Partner-Key": "test-partner"}
ops = {"X-Ops-Key": "test-ops"}
real_ocr = pytest.mark.skipif(
    not shutil.which("tesseract"), reason="Tesseract is required for specimen OCR"
)


def create():
    r = client.post("/api/journeys", json={}).json()
    return r["id"], {"Authorization": "Bearer " + r["token"]}


def prepare(k):
    jid, h = create()
    assert (
        client.post(f"/api/journeys/{jid}/demo-profile/{k}", headers=h).status_code
        == 200
    )
    q = client.post(
        "/api/quotes",
        headers=h,
        json={"journey_id": jid, "items": [{"product_id": "P01", "quantity": 1}]},
    ).json()
    assert q["valid"], q
    assert (
        client.post("/api/quotes/" + q["quote_id"] + "/accept", headers=h).status_code
        == 200
    )
    client.post(
        "/api/kyc/consent", headers=h, data={"journey_id": jid, "accepted": "true"}
    ).raise_for_status()
    return jid, h, q


def upload(k, jid, h, skip=None):
    source = m.DATA / "kyc_documents"
    for path in sorted(source.glob(k + "_*.png")):
        if skip and skip in path.name:
            continue
        r = client.post(
            "/api/kyc/documents",
            headers=h,
            data={"journey_id": jid},
            files={"file": (path.name, path.read_bytes(), "image/png")},
        )
        assert r.status_code == 200, r.text
        client.post(
            "/api/kyc/documents/" + r.json()["document_id"] + "/confirm", headers=h
        ).raise_for_status()


def check(k, jid, h):
    lead = next(r for r in m.LEADS if r["lead_id"] == SAMPLE_LEADS[k])
    return client.post(
        "/api/kyc/verify",
        headers=h,
        json={
            "journey_id": jid,
            "gstin": lead["gstin"],
            "udyam_number": lead["udyam_number"],
            "require_all_documents": False,
        },
    ).json()


@real_ocr
@pytest.mark.parametrize(
    "k,expected",
    [
        ("K03", "VERIFIED"),
        ("K08", "VERIFIED"),
        ("K09", "VERIFIED"),
        ("K01", "ACTION_REQUIRED"),
        ("K02", "VERIFIED"),
        ("K04", "ACTION_REQUIRED"),
        ("K05", "ACTION_REQUIRED"),
        ("K06", "REVIEW_REQUIRED"),
        ("K07", "REVIEW_REQUIRED"),
        ("K10", "REVIEW_REQUIRED"),
        ("K11", "ACTION_REQUIRED"),
        ("K12", "VERIFIED"),
    ],
)
def test_real_applicant(k, expected):
    jid, h, q = prepare(k)
    upload(k, jid, h)
    result = check(k, jid, h)
    assert result["status"] == expected, result
    if k == "K10":
        assert "Signatory" in " ".join(result["reasons"])
        assert not client.get("/api/partners/reviews", headers=staff).json()
        client.post(
            f"/api/journeys/{jid}/consent/partner", headers=h, data={"accepted": "true"}
        ).raise_for_status()
        case = client.get("/api/partners/reviews", headers=staff).json()[0]
        decision = {
            "revision": case["revision"],
            "decision": "approve",
            "rationale": "Mock reviewer independently confirmed the authorized representative.",
        }
        client.post(
            f"/api/partners/reviews/{jid}", headers=staff, json=decision
        ).raise_for_status()
        assert (
            client.post(
                f"/api/partners/reviews/{jid}", headers=staff, json=decision
            ).status_code
            == 409
        )
        assert (
            client.get(f"/api/partners/reviews/{jid}/audit", headers=staff).json()[0][
                "decision"
            ]
            == "approve"
        )
        client.post(
            "/api/orders",
            headers=h,
            json={
                "journey_id": jid,
                "quote_id": q["quote_id"],
                "payment_confirmed": True,
            },
        ).raise_for_status()
    if k in ("K08", "K09"):
        assert "office admin" in " ".join(result["notes"])
    if k == "K06":
        assert "checksum-invalid" in " ".join(result["reasons"])
    if k == "K12":
        b = {"journey_id": jid, "quote_id": q["quote_id"], "payment_confirmed": True}
        swap = client.patch(
            f"/api/journeys/{jid}/profile",
            headers=h,
            json={"profile": {"gstin": next(iter(m.GST))}},
        )
        assert swap.status_code == 409, swap.text
        failed = client.post(
            "/api/orders", headers=h, json={**b, "payment_result": "failed"}
        ).json()
        assert failed["retryable"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(
                pool.map(
                    lambda _: client.post("/api/orders", headers=h, json=b).json(),
                    range(4),
                )
            )
        assert len({r["order_id"] for r in receipts}) == 1
        order = receipts[0]
        assert (
            order["expected_activation"]
            and order["notifications"]
            and len(order["steps"]) == 5
        )
        url = f"/api/mock/ops/orders/{order['order_id']}/events"
        assert (
            client.post(
                url, headers=h, json={"status": "SERVICEABILITY_PENDING"}
            ).status_code
            == 403
        )
        assert (
            client.post(url, headers=ops, json={"status": "ACTIVATED"}).status_code
            == 409
        )
        for stage in order["steps"][1:]:
            assert (
                client.post(url, headers=ops, json={"status": stage}).status_code == 200
            )
        measured = client.get("/api/partners/kpis", headers=staff).json()
        assert (
            measured["activated"] == 1
            and measured["mean_days_journey_to_activation"] >= 0
        )
        second, hh, qq = prepare(k)
        assert (
            client.get("/api/orders/" + order["order_id"], headers=hh).status_code
            == 403
        )
        with m.con() as db:
            doc = db.execute(
                "SELECT id FROM documents WHERE journey_id=?", (jid,)
            ).fetchone()[0]
        assert (
            client.post(
                "/api/kyc/documents/" + doc + "/confirm", headers=hh
            ).status_code
            == 403
        )
        upload(k, second, hh)
        assert "reused" in " ".join(check(k, second, hh)["reasons"])
        assert (
            client.post(
                "/api/orders",
                headers=hh,
                json={**b, "journey_id": second, "quote_id": qq["quote_id"]},
            ).status_code
            == 409
        )


@real_ocr
def test_k11_omitting_bill_and_false_flag_cannot_verify():
    jid, h, q = prepare("K11")
    upload("K11", jid, h, skip="address_proof")
    r = check("K11", jid, h)
    assert (
        r["status"] == "ACTION_REQUIRED"
        and "Missing document: address proof" in r["reasons"]
    )


def test_scope_masking_and_role_isolation():
    jid, h, q = prepare("K02")
    other, hh = create()
    for path in [f"/api/journeys/{jid}", f"/api/journeys/{jid}/recommendations"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=hh).status_code == 403
    assert client.delete(f"/api/journeys/{jid}/data", headers=hh).status_code == 403
    assert (
        client.post("/api/quotes/" + q["quote_id"] + "/accept", headers=hh).status_code
        == 403
    )
    assert client.get("/api/partners/handoffs", headers=h).status_code == 403
    assert client.get("/api/partners/leads", headers=ops).status_code == 403
    assert client.get("/api/metrics", headers=h).status_code == 403
    gst = next(iter(m.GST))
    r = client.patch(
        f"/api/journeys/{jid}/profile", headers=h, json={"profile": {"gstin": gst}}
    )
    assert (
        gst not in r.text
        and gst not in client.get(f"/api/journeys/{jid}", headers=h).text
    )
    assert (
        client.post(
            "/api/handoffs",
            headers=h,
            json={"journey_id": jid, "permission": True, "reason": gst},
        ).status_code
        == 422
    )
    client.post(
        "/api/handoffs",
        headers=h,
        json={"journey_id": jid, "permission": True, "reason": "PRICE"},
    ).raise_for_status()
    assert gst not in client.get("/api/partners/handoffs", headers=staff).text
    client.delete(f"/api/journeys/{jid}/consent/partner", headers=h).raise_for_status()
    assert client.get("/api/partners/handoffs", headers=staff).json() == []
    client.delete(f"/api/journeys/{jid}/consent/kyc", headers=h).raise_for_status()
    assert (
        client.post(
            f"/api/journeys/{jid}/registry", headers=h, json={"gstin": gst}
        ).status_code
        == 403
    )


def test_limits_and_fixture_switch(monkeypatch):
    monkeypatch.setenv("ONEJOURNEY_JOURNEYS_PER_HOUR", "2")
    jid, h = create()
    create()
    assert client.post("/api/journeys", json={}).status_code == 429
    url = f"/api/journeys/{jid}/specimens/"
    assert client.post(url + "K01", headers=h).status_code == 403
    assert client.post(url + "K13", headers=h).status_code == 422
    monkeypatch.setenv("ONEJOURNEY_DEMO_MODE", "0")
    assert client.post(url + "K12", headers=h).status_code == 404


@real_ocr
def test_changed_facts_and_quote_invalidate_evidence():
    jid, h, q = prepare("K12")
    upload("K12", jid, h)
    assert check("K12", jid, h)["status"] == "VERIFIED"
    different = client.patch(
        f"/api/journeys/{jid}/profile", headers=h, json={"profile": {"employees": 101}}
    )
    assert different.json()["kyc_invalidated"]
    order = {"journey_id": jid, "quote_id": q["quote_id"], "payment_confirmed": True}
    assert client.post("/api/orders", headers=h, json=order).status_code == 409
    lead = next(r for r in m.LEADS if r["lead_id"] == SAMPLE_LEADS["K12"])
    client.patch(
        f"/api/journeys/{jid}/profile",
        headers=h,
        json={"profile": {"employees": int(lead["employee_count"])}},
    ).raise_for_status()
    assert check("K12", jid, h)["status"] == "VERIFIED"
    newer = client.post(
        "/api/quotes",
        headers=h,
        json={"journey_id": jid, "items": [{"product_id": "P01"}], "term_months": 24},
    ).json()
    client.post(
        "/api/quotes/" + newer["quote_id"] + "/accept", headers=h
    ).raise_for_status()
    assert (
        client.post(
            "/api/orders", headers=h, json={**order, "quote_id": newer["quote_id"]}
        ).status_code
        == 409
    )


def test_quote_terms_coverage_expiry_and_rescue():
    jid, h, q = prepare("K02")
    template = {"journey_id": jid, "items": [{"product_id": "P01"}], "term_months": 24}
    offer = client.post("/api/quotes", headers=h, json=template).json()
    assert (
        offer["recurring_paise"] < q["recurring_paise"]
        and offer["monthly_with_gst_paise"]
        == offer["recurring_paise"] + offer["gst_paise"]
    )
    assert not client.post(
        "/api/quotes",
        headers=h,
        json={**template, "site_code": "WIRELESS", "site_feasible": True},
    ).json()["valid"]
    fresh = client.post("/api/quotes", headers=h, json=template).json()
    with m.con(write=True) as db:
        terms = {**fresh, "expires_at": "2000-01-01"}
        db.execute(
            "UPDATE quote_terms SET payload=? WHERE quote_id=?",
            (m.j(terms), fresh["quote_id"]),
        )
    assert (
        client.post(
            "/api/quotes/" + fresh["quote_id"] + "/accept", headers=h
        ).status_code
        == 409
    )
    queue = client.get("/api/partners/leads", headers=staff).json()
    assert queue["prospects"] and all(
        not any(
            s in lead["drop_reason"].lower()
            for s in ("wrong", "unreachable", "not interested")
        )
        for lead in queue["prospects"]
    )
    resumed = client.post(
        "/api/partners/leads/" + queue["prospects"][0]["lead_id"] + "/resume",
        headers=staff,
    ).json()
    assert resumed["token"] and not resumed["profile"]["gstin"]
    for language in ("ta", "te", "kn", "gu"):
        assert (
            client.post("/api/journeys", json={"language": language}).status_code == 200
        )


def test_document_cap_and_rate_limit(monkeypatch):
    jid, h, q = prepare("K02")
    with m.con(write=True) as db:
        db.executemany(
            "INSERT INTO documents VALUES(?,?,?,?,?,?)",
            [(str(i), jid, "fixture", "{}", 0, m.now()) for i in range(12)],
        )
    r = client.post(
        "/api/kyc/documents",
        headers=h,
        data={"journey_id": jid},
        files={"file": ("image.png", b"dummy", "image/png")},
    )
    assert r.status_code == 429
    monkeypatch.setenv("ONEJOURNEY_REQUESTS_PER_MIN", "1")
    assert client.get("/api/catalogue").status_code == 429


def test_negative_review_and_revocation_are_audited():
    jid, h, q = prepare("K10")
    client.post(
        f"/api/journeys/{jid}/consent/partner", headers=h, data={"accepted": "true"}
    ).raise_for_status()
    with m.con(write=True) as db:
        db.execute(
            "INSERT INTO kyc VALUES(?,?,?,?,?,?)",
            (jid, "REVIEW_REQUIRED", '["Mismatch"]', "test", "", m.now()),
        )
        db.execute(
            "INSERT INTO bindings VALUES(?,?,?,?)", (jid, q["quote_id"], "{}", "r1")
        )
    r = client.post(
        f"/api/partners/reviews/{jid}",
        headers=staff,
        json={
            "revision": "r1",
            "decision": "reject",
            "rationale": "Conflicting applicant details.",
        },
    )
    assert r.json()["status"] == "ACTION_REQUIRED"
    assert (
        client.get(f"/api/partners/reviews/{jid}/audit", headers=staff).json()[0][
            "decision"
        ]
        == "reject"
    )
    client.delete(f"/api/journeys/{jid}/consent/partner", headers=h).raise_for_status()
    assert (
        client.get(f"/api/partners/reviews/{jid}/audit", headers=staff).status_code
        == 403
    )


@pytest.mark.parametrize(
    ("language", "message", "business"),
    [
        ("ta", "என் கடை இணையம்", "retail"),
        ("te", "మా దుకాణం ఇంటర్నెట్", "retail"),
        ("kn", "ನನ್ನ ಅಂಗಡಿ ಅಂತರ್ಜಾಲ", "retail"),
        ("gu", "મારી દુકાન ઇન્ટરનેટ", "retail"),
    ],
)
def test_offline_language_capture(language, message, business, monkeypatch):
    monkeypatch.setattr(
        m, "local_runtime_status", lambda: {"status": "runtime_unavailable"}
    )
    r = client.post("/api/journeys", json={"language": language}).json()
    result = client.post(
        f"/api/journeys/{r['id']}/advisor",
        headers={"Authorization": "Bearer " + r["token"]},
        json={"text": message},
    ).json()
    assert result["mode"] == "guided" and result["updates"]["business_type"] == business
    assert (
        "internet" in result["updates"]["needs"]
        and result["message"] == m.languages.QUESTIONS[language][1]
    )


def test_autohandoff_uses_live_model_and_restricts_grievances():
    jid, h, q = prepare("K02")
    client.post(
        f"/api/journeys/{jid}/consent/partner", headers=h, data={"accepted": "true"}
    ).raise_for_status()
    shared = client.get("/api/partners/handoffs", headers=staff).json()
    assert any(r["reason"] == "NO_GSTIN" and 0 <= r["score"] <= 1 for r in shared)
    client.post(
        f"/api/journeys/{jid}/grievances",
        headers=h,
        json={"message": "Please call 98765 43210 about withdrawal."},
    ).raise_for_status()
    assert client.get("/api/mock/ops/grievances", headers=h).status_code == 403
    reply = client.get("/api/mock/ops/grievances", headers=ops)
    assert "98765" not in reply.text and "[phone removed]" in reply.text
