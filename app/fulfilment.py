"""Authenticated fulfilment, simulated notifications, assisted sales and recovery."""

from datetime import timedelta
from typing import Literal
from fastapi import APIRouter, Form, HTTPException, Request
from pydantic import BaseModel, Field
from . import main as m
from .onboarding import auto_handoff, grant, has
from .propensity import handoff_priority, live_score, recommended_action, rescuable

router = APIRouter()
STEPS = (
    "ORDER_PLACED",
    "SERVICEABILITY_PENDING",
    "INSTALLATION_SCHEDULED",
    "PROVISIONING",
    "ACTIVATED",
)
TRANS = {
    s: {STEPS[i + 1], "ON_HOLD", "CANCELLED", "FAILED"}
    for i, s in enumerate(STEPS[:-1])
}
TRANS["ON_HOLD"] = {"SERVICEABILITY_PENDING", "INSTALLATION_SCHEDULED", "CANCELLED"}


def notify(db, jid, channel, message):
    db.execute(
        "INSERT INTO notices(journey_id,channel,message,created_at) VALUES(?,?,?,?)",
        (jid, channel, message, m.now()),
    )


def order_view(db, oid):
    o = m.row(db, "SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        raise HTTPException(404, "Order not found")
    f = (
        m.row(db, "SELECT expected,scheduled FROM fulfilment WHERE order_id=?", (oid,))
        or {}
    )
    return {
        **o,
        "order_id": oid,
        "expected_activation": f.get("expected"),
        "install_date": f.get("scheduled"),
        "steps": STEPS,
        "events": [
            dict(r)
            for r in db.execute(
                "SELECT status,details,created_at FROM order_events WHERE order_id=? ORDER BY id",
                (oid,),
            )
        ],
        "notifications": [
            dict(r)
            for r in db.execute(
                "SELECT channel,message,created_at FROM notices WHERE journey_id=? ORDER BY id",
                (o["journey_id"],),
            )
        ],
        "simulated": True,
    }


@router.post("/api/orders")
def createorder(b: m.OrderInput):
    with m.con(write=True) as db:
        q = m.row(
            db,
            "SELECT * FROM quotes WHERE id=? AND journey_id=?",
            (b.quote_id, b.journey_id),
        )
        existing = m.row(
            db,
            "SELECT id FROM orders WHERE quote_id=? AND journey_id=?",
            (b.quote_id, b.journey_id),
        )
        if existing:
            return {**order_view(db, existing["id"]), "idempotent": True}
        latest = db.execute(
            "SELECT id FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1",
            (b.journey_id,),
        ).fetchone()
        if not q or not q["accepted"] or not q["valid"] or latest[0] != b.quote_id:
            raise HTTPException(409, "Latest accepted quote required")
        terms = m.parse(
            db.execute(
                "SELECT payload FROM quote_terms WHERE quote_id=?", (b.quote_id,)
            ).fetchone()[0]
        )
        if (
            terms["expires_at"] <= m.now()
            or terms.get("valid_until", "9999") < m.TODAY.isoformat()
            or not terms["site_feasible"]
        ):
            raise HTTPException(409, "Quote expired or site not feasible")
        p = m.parse(
            db.execute(
                "SELECT profile FROM journeys WHERE id=?", (b.journey_id,)
            ).fetchone()[0]
        )
        k = m.row(
            db,
            "SELECT k.*,b.quote_id,b.snapshot FROM kyc k JOIN bindings b USING(journey_id) WHERE k.journey_id=?",
            (b.journey_id,),
        )
        claim = m.row(
            db, "SELECT gstin FROM claims WHERE journey_id=?", (b.journey_id,)
        )
        if not (
            p.get("confirmed")
            and has(db, b.journey_id, "kyc")
            and k
            and k["status"] == "VERIFIED"
            and k["quote_id"] == q["id"]
            and k["snapshot"] == m.facts(p)
            and claim
            and claim["gstin"] == k["gstin"] == p.get("gstin")
            and k["udyam"] == p.get("udyam_number")
        ):
            raise HTTPException(
                409, "Current profile, verified identity, consent and quote must match"
            )
        if m.check_bundle(p, m.parse(q["items"])):
            raise HTTPException(409, "Bundle is no longer eligible")
        if not b.payment_confirmed:
            raise HTTPException(400, "Authorize simulated checkout")
        earliest = m.TODAY + timedelta(
            days=max(item["install_days"] for item in terms["items"])
        )
        slot = b.install_date or earliest
        if slot < m.TODAY or slot > m.TODAY + timedelta(days=90):
            raise HTTPException(422, "Choose a slot within 90 days of the fixture date")
        if b.payment_result == "failed":
            notify(
                db,
                b.journey_id,
                b.channel,
                "Simulated payment failed. Retry the same quote.",
            )
            return {"status": "PAYMENT_FAILED", "retryable": True}
        oid = m.uid("order")
        expected = max(slot, earliest).isoformat()
        db.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?)",
            (oid, b.journey_id, q["id"], "ORDER_PLACED", m.now()),
        )
        db.execute(
            "INSERT INTO fulfilment VALUES(?,?,?,?)",
            (b.journey_id, oid, expected, slot.isoformat()),
        )
        db.execute(
            "INSERT INTO order_events(order_id,status,details,simulated,created_at) VALUES(?,?,?,?,?)",
            (oid, "ORDER_PLACED", "Simulated payment accepted", 1, m.now()),
        )
        db.execute(
            "UPDATE journeys SET stage=? WHERE id=?", ("ORDER_SUBMITTED", b.journey_id)
        )
        notify(
            db,
            b.journey_id,
            b.channel,
            f"Installation slot {slot}. Expected activation {expected}.",
        )
        return order_view(db, oid)


@router.get("/api/orders/{oid}")
def getorder(oid: str):
    with m.con() as db:
        return order_view(db, oid)


@router.post("/api/mock/ops/orders/{oid}/events")
def event(oid: str, b: m.OpsInput):
    with m.con(write=True) as db:
        o = m.row(db, "SELECT * FROM orders WHERE id=?", (oid,))
        if not o:
            raise HTTPException(404, "Order missing")
        if b.status not in TRANS.get(o["status"], set()):
            raise HTTPException(409, "Invalid status transition")
        db.execute("UPDATE orders SET status=? WHERE id=?", (b.status, oid))
        db.execute(
            "INSERT INTO order_events(order_id,status,details,simulated,created_at) VALUES(?,?,?,?,?)",
            (
                oid,
                b.status,
                m.redact_chat(b.details) or "Simulated operations update",
                1,
                m.now(),
            ),
        )
        channel = db.execute(
            "SELECT channel FROM notices WHERE journey_id=? ORDER BY id DESC LIMIT 1",
            (o["journey_id"],),
        ).fetchone()
        notify(
            db,
            o["journey_id"],
            channel[0] if channel else "SMS",
            f'Order update: {b.status.replace("_"," ").lower()}.',
        )
        if b.status in {"ACTIVATED", "CANCELLED", "FAILED"}:
            db.execute("DELETE FROM claims WHERE journey_id=?", (o["journey_id"],))
        if b.status == "ACTIVATED":
            db.execute(
                "INSERT INTO milestones(journey_id,kind,created_at) VALUES(?,?,?)",
                (o["journey_id"], "activated", m.now()),
            )
        return order_view(db, oid)


@router.post("/api/handoffs")
def handoff(b: m.HandoffInput):
    if not b.permission:
        raise HTTPException(403, "Customer authorization required")
    with m.con(write=True) as db:
        grant(db, b.journey_id, "partner")
        auto_handoff(db, b.journey_id, b.reason)
    return {"queued": True}


@router.get("/api/partners/handoffs")
def handoffs():
    with m.con() as db:
        rows = db.execute(
            """SELECT h.id,h.reason,h.status,h.created_at,h.source,j.profile FROM handoffs h JOIN journeys j ON j.id=h.journey_id
                           JOIN agreements a ON a.journey_id=h.journey_id AND a.purpose='partner' ORDER BY h.created_at DESC LIMIT 60"""
        ).fetchall()
    return sorted(
        [
            {
                "id": r["id"],
                "reason": (
                    r["reason"]
                    if r["reason"]
                    in (
                        "SITE_SURVEY",
                        "PRICE",
                        "INELIGIBLE",
                        "KYC_REVIEW",
                        "NO_GSTIN",
                        "INSTALLATION",
                        "OTHER",
                    )
                    else "OTHER"
                ),
                "source": m.redact_chat(r["source"]),
                "status": r["status"],
                "created_at": r["created_at"],
                "score": live_score(m.parse(r["profile"]), r["source"]),
                "action": recommended_action(r["reason"]),
            }
            for r in rows
        ],
        key=lambda r: r["score"],
        reverse=True,
    )


@router.get("/api/partners/leads")
@router.get("/api/partners/propensity")
def rescue():
    return handoff_priority(limit=12)


@router.post("/api/partners/leads/{lead_id}/resume")
def resume(lead_id: str, request: Request):
    m.access.limit(
        ("starts", request.client.host if request.client else "local"),
        int(m.os.getenv("ONEJOURNEY_JOURNEYS_PER_HOUR", "30")),
        3600,
    )
    r = next(
        (
            r
            for r in m.LEADS
            if r["lead_id"] == lead_id and r["outcome"] == "Dropped" and rescuable(r)
        ),
        None,
    )
    if not r:
        raise HTTPException(404, "No eligible rescue lead")
    result = m.newjourney(m.CreateJourney(source=r["lead_source"]))
    result["profile"].update(
        business_type=r["business_type"],
        employees=int(r["employee_count"]),
        sites=int(r["num_sites"]),
        city_tier=int(r["city_tier"]),
    )
    with m.con(write=True) as db:
        db.execute(
            "UPDATE journeys SET profile=? WHERE id=?",
            (m.j(result["profile"]), result["id"]),
        )
    return {
        **result,
        "previous_gstin": m.mask_kyc_value(r["gstin"]),
        "previous_udyam": m.mask_kyc_value(r["udyam_number"]),
    }


@router.get("/api/partners/kpis")
def live_kpis():
    with m.con() as db:
        started = db.execute("SELECT COUNT(*) FROM journeys").fetchone()[0]
        activated, days = db.execute(
            """SELECT COUNT(*),AVG(julianday(m.created_at)-julianday(j.created_at))
                            FROM milestones m JOIN journeys j ON j.id=m.journey_id WHERE m.kind='activated'"""
        ).fetchone()
    return {
        "journeys": started,
        "activated": activated,
        "activation_pct": round(100 * activated / started, 1) if started else 0,
        "mean_days_journey_to_activation": days,
        "scope": "Retained prototype journeys; timestamps measured on this device",
    }


@router.post("/api/journeys/{jid}/esign")
def esign(jid: str, accepted: bool = Form(...)):
    if not accepted:
        raise HTTPException(400, "Explicit signature required")
    with m.con(write=True) as db:
        q = m.row(
            db,
            "SELECT id,accepted FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1",
            (jid,),
        )
        if not q or not q["accepted"] or not has(db, jid, "kyc"):
            raise HTTPException(409, "Accepted quote and active KYC consent required")
        grant(db, jid, "mock-esign:" + q["id"])
    return {"signed_quote": q["id"], "simulated": True}


class Grievance(BaseModel):
    message: str = Field(min_length=5, max_length=500)


@router.post("/api/journeys/{jid}/grievances")
def grievance(jid: str, b: Grievance):
    ref = m.uid("grievance")
    with m.con(write=True) as db:
        db.execute(
            "INSERT INTO grievances VALUES(?,?,?,?)",
            (ref, jid, m.redact_chat(b.message), m.now()),
        )
    return {"reference": ref}


@router.get("/api/mock/ops/grievances")
def grievances():
    with m.con() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT id,message,created_at FROM grievances ORDER BY created_at DESC"
            )
        ]
