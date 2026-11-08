"""Evidence lifecycle, limited demo fixtures, staff decisions and consent."""

import asyncio
import hashlib
import io
import os
import re
from datetime import date, timedelta
from typing import Literal
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from . import main as m
from .evidence import assess
from .kyc_pipeline import analyse

router = APIRouter()
OCR_SLOTS = asyncio.Semaphore(2)
REASONS = {
    "SITE_SURVEY",
    "PRICE",
    "INELIGIBLE",
    "KYC_REVIEW",
    "NO_GSTIN",
    "INSTALLATION",
    "OTHER",
}


def has(db, jid, purpose):
    return bool(
        m.row(
            db,
            "SELECT journey_id FROM agreements WHERE journey_id=? AND purpose=?",
            (jid, purpose),
        )
    )


def grant(db, jid, purpose):
    db.execute(
        "INSERT OR REPLACE INTO agreements VALUES(?,?,?)", (jid, purpose, m.now())
    )
    db.execute(
        "INSERT INTO consents VALUES(?,?,?,?,?)",
        (m.uid("consent"), jid, purpose, "notice-2", m.now()),
    )


def auto_handoff(db, jid, reason):
    if not has(db, jid, "partner"):
        return
    if not m.row(
        db,
        "SELECT id FROM handoffs WHERE journey_id=? AND reason=? AND status='NEW'",
        (jid, reason),
    ):
        source = db.execute(
            "SELECT source FROM journeys WHERE id=?", (jid,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO handoffs VALUES(?,?,?,?,?,?,?)",
            (m.uid("handoff"), jid, reason, 1, source, "NEW", m.now()),
        )


@router.post("/api/kyc/consent")
def consent(journey_id: str = Form(...), accepted: bool = Form(...)):
    if not accepted:
        raise HTTPException(400, "Explicit consent required")
    with m.con(write=True) as db:
        grant(db, journey_id, "kyc")
    return {"consented": True, "purpose": "kyc", "version": "notice-2"}


@router.post("/api/journeys/{jid}/consent/partner")
def partner_consent(jid: str, accepted: bool = Form(...)):
    if not accepted:
        raise HTTPException(400, "Explicit consent required")
    with m.con(write=True) as db:
        grant(db, jid, "partner")
        k = m.row(db, "SELECT status FROM kyc WHERE journey_id=?", (jid,))
        if k and k["status"] == "REVIEW_REQUIRED":
            auto_handoff(db, jid, "KYC_REVIEW")
        p = m.parse(
            db.execute("SELECT profile FROM journeys WHERE id=?", (jid,)).fetchone()[0]
        )
        q = m.row(
            db,
            "SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1",
            (jid,),
        )
        if not p.get("gstin"):
            auto_handoff(db, jid, "NO_GSTIN")
        if q and not q["valid"]:
            auto_handoff(db, jid, "INELIGIBLE")
        if q and p.get("budget") and q["recurring_paise"] > int(p["budget"]) * 100:
            auto_handoff(db, jid, "PRICE")
    return {"consented": True, "purpose": "partner"}


@router.delete("/api/journeys/{jid}/consent/{purpose}")
def withdraw(jid: str, purpose: Literal["partner", "kyc"]):
    with m.con(write=True) as db:
        db.execute(
            "DELETE FROM agreements WHERE journey_id=? AND purpose=?", (jid, purpose)
        )
        db.execute(
            "DELETE FROM consents WHERE journey_id=? AND purpose=?", (jid, purpose)
        )
        tables = (
            ["handoffs"]
            if purpose == "partner"
            else [
                "kyc",
                "documents",
                "document_hashes",
                "bindings",
                "claims",
                "review_audit",
            ]
        )
        for table in tables:
            db.execute(f"DELETE FROM {table} WHERE journey_id=?", (jid,))
        db.execute(
            "INSERT INTO consents VALUES(?,?,?,?,?)",
            (m.uid("withdrawal"), jid, purpose + " withdrawn", "notice-2", m.now()),
        )
        if purpose == "kyc":
            db.execute(
                "DELETE FROM agreements WHERE journey_id=? AND (purpose LIKE 'mock-esign:%' OR purpose='mock-digilocker')",
                (jid,),
            )
            db.execute(
                "DELETE FROM consents WHERE journey_id=? AND (purpose LIKE 'mock-esign:%' OR purpose='mock-digilocker')",
                (jid,),
            )
            db.execute(
                "DELETE FROM handoffs WHERE journey_id=? AND reason='KYC_REVIEW'",
                (jid,),
            )
            p = m.parse(
                db.execute(
                    "SELECT profile FROM journeys WHERE id=?", (jid,)
                ).fetchone()[0]
            )
            for field in ("gstin", "udyam", "udyam_number", "legal_name"):
                p.pop(field, None)
            db.execute("UPDATE journeys SET profile=? WHERE id=?", (m.j(p), jid))
    return {"withdrawn": purpose}


def public_doc(d):
    return {
        **d,
        "gstin_candidates": [m.mask_kyc_value(x) for x in d["gstin_candidates"]],
        "udyam_candidates": [m.mask_kyc_value(x) for x in d["udyam_candidates"]],
        "gstin_suggestion": m.mask_kyc_value(d["gstin_suggestion"]),
    }


async def ingest(jid, raw):
    digest = hashlib.sha256(raw).hexdigest()
    with m.con(write=True) as db:
        if not has(db, jid, "kyc"):
            raise HTTPException(403, "Consent required before upload")
        if db.execute("SELECT 1 FROM orders WHERE journey_id=?", (jid,)).fetchone():
            raise HTTPException(409, "Submitted order is locked")
        existing = m.row(
            db,
            "SELECT d.* FROM documents d JOIN document_hashes h ON h.doc_id=d.id WHERE h.journey_id=? AND digest=?",
            (jid, digest),
        )
        if existing:
            if not existing["extracted"]:
                raise HTTPException(409, "OCR in progress")
            return {
                "document_id": existing["id"],
                "extracted": public_doc(m.parse(existing["extracted"])),
            }
        if (
            db.execute(
                "SELECT COUNT(*) FROM documents WHERE journey_id=?", (jid,)
            ).fetchone()[0]
            >= 12
        ):
            raise HTTPException(429, "Twelve-document limit per journey")
        m.access.limit(("ocr", jid), 18, 3600)
        did = m.uid("doc")
        db.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?)",
            (did, jid, "document", "", 0, m.now()),
        )
        db.execute("INSERT INTO document_hashes VALUES(?,?,?)", (did, jid, digest))
        m.invalidate(db, jid, "Evidence changed; verify current documents")
    try:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if (
                    im.format not in ("JPEG", "PNG")
                    or im.width * im.height > 16_000_000
                ):
                    raise ValueError()
                im.verify()
        except Exception:
            raise HTTPException(415, "Valid JPG/PNG up to 16 megapixels required")
        async with OCR_SLOTS:
            data = await run_in_threadpool(analyse, raw, m.GST, m.TODAY)
        fields = dict(data["fields"])
        if fields.get("gstin_raw"):
            fields["gstin_raw"] = m.mask_kyc_value(fields["gstin_raw"])
        d = {
            k: data[k]
            for k in (
                "gstin_candidates",
                "udyam_candidates",
                "gstin_suggestion",
                "doc_type",
                "quality",
                "flags",
            )
        }
        d.update(
            fields=fields,
            text_preview=" · ".join(
                m.redact_chat(str(fields[k]))
                for k in (
                    "trade_name",
                    "enterprise_name",
                    "billed_to",
                    "bill_date",
                    "business",
                    "name",
                )
                if fields.get(k)
            )
            or "See the extracted fields and masked identifiers.",
            ocr_engine="Tesseract",
            requires_user_review=True,
        )
        with m.con(write=True) as db:
            if (
                not has(db, jid, "kyc")
                or not db.execute(
                    "UPDATE documents SET extracted=? WHERE id=?", (m.j(d), did)
                ).rowcount
            ):
                raise HTTPException(403, "Consent or journey expired during OCR")
        return {"document_id": did, "extracted": public_doc(d)}
    except BaseException as exc:
        with m.con(write=True) as db:
            db.execute("DELETE FROM documents WHERE id=?", (did,))
            db.execute("DELETE FROM document_hashes WHERE doc_id=?", (did,))
        if isinstance(exc, (HTTPException, asyncio.CancelledError)):
            raise
        raise HTTPException(503, "Local OCR could not process this image") from exc


@router.post("/api/kyc/documents")
async def document(journey_id: str = Form(...), file: UploadFile = File(...)):
    with m.con() as db:
        if not has(db, journey_id, "kyc"):
            raise HTTPException(403, "Consent required before upload")
    raw = await file.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(413, "Maximum image size is 4 MB")
    return await ingest(journey_id, raw)


@router.post("/api/kyc/documents/{did}/confirm")
def confirm(did: str):
    with m.con(write=True) as db:
        doc = m.row(
            db,
            "SELECT journey_id,extracted,confirmed FROM documents WHERE id=?",
            (did,),
        )
        if not doc or not doc["extracted"]:
            raise HTTPException(409, "Document is unavailable")
        if not doc["confirmed"]:
            m.invalidate(db, doc["journey_id"], "Confirmed evidence changed")
            db.execute("UPDATE documents SET confirmed=1 WHERE id=?", (did,))
    return {"confirmed": True}


SAMPLE_LEADS = dict(
    zip(
        (f"K{i:02}" for i in range(1, 13)),
        (
            "L00045",
            "L01287",
            "L03571",
            "L01014",
            "L04014",
            "L00115",
            "L03349",
            "L03473",
            "L04779",
            "L04525",
            "L03485",
            "L03558",
        ),
    )
)


@router.post("/api/journeys/{jid}/demo-profile/{specimen}")
def sample_profile(jid: str, specimen: str):
    if os.getenv("ONEJOURNEY_DEMO_MODE") != "1":
        raise HTTPException(404, "Demo fixtures disabled")
    if not re.fullmatch(r"K(0[1-9]|1[0-2])", specimen):
        raise HTTPException(422, "Choose K01â€“K12")
    lead = next(r for r in m.LEADS if r["lead_id"] == SAMPLE_LEADS[specimen])
    with m.con(write=True) as db:
        if db.execute("SELECT 1 FROM quotes WHERE journey_id=?", (jid,)).fetchone():
            raise HTTPException(409, "Choose an applicant before creating a quote")
        p = m.parse(
            db.execute("SELECT profile FROM journeys WHERE id=?", (jid,)).fetchone()[0]
        )
        p.update(
            name=lead["business_name"],
            business_type=lead["business_type"],
            employees=int(lead["employee_count"]),
            sites=int(lead["num_sites"]),
            city_tier=int(lead["city_tier"]),
            budget=4500,
            needs=["internet"],
            must=["internet"],
            confirmed=True,
        )
        db.execute("UPDATE journeys SET profile=? WHERE id=?", (m.j(p), jid))
        return {"demo": True, "profile": m.public_profile(p)}


@router.post("/api/journeys/{jid}/specimens/{specimen}")
async def load_specimen(jid: str, specimen: str):
    if os.getenv("ONEJOURNEY_DEMO_MODE") != "1":
        raise HTTPException(404, "Demo specimens disabled")
    if not re.fullmatch(r"K(0[1-9]|1[0-2])", specimen):
        raise HTTPException(422, "Choose K01â€“K12")
    files = sorted((m.DATA / "kyc_documents").glob(specimen + "_*.png"))
    documents = [await ingest(jid, f.read_bytes()) for f in files]
    return {"demo": True, "specimen": specimen, "documents": documents}


@router.post("/api/journeys/{jid}/digilocker/{specimen}")
async def mock_vault(jid: str, specimen: str, authorized: bool = Form(...)):
    if os.getenv("ONEJOURNEY_DEMO_MODE") != "1":
        raise HTTPException(404, "Mock vault disabled")
    if not re.fullmatch(r"K(0[1-9]|1[0-2])", specimen):
        raise HTTPException(422, "Invalid specimen")
    if not authorized:
        raise HTTPException(403, "Authorize mock vault release")
    with m.con(write=True) as db:
        if not has(db, jid, "kyc"):
            raise HTTPException(403, "KYC consent required")
        grant(db, jid, "mock-digilocker")
    return await load_specimen(jid, specimen)


def identifiers(docs, p, b):
    cert = next(
        (x for x in reversed(docs) if x.get("doc_type") == "gst_certificate"), {}
    )
    ud = next(
        (x for x in reversed(docs) if x.get("doc_type") == "udyam_certificate"), {}
    )

    def resolve(entered, values, fallback):
        if not entered:
            return fallback or next(iter(values), "")
        return next(
            (v for v in values if m.mask_kyc_value(v) == entered),
            entered.upper().strip(),
        )

    suggestion = cert.get("gstin_suggestion")
    gst = resolve(b.gstin, cert.get("gstin_candidates", []), p.get("gstin", ""))
    accepted = bool(
        suggestion
        and b.gstin
        and b.gstin.upper() in (suggestion, m.mask_kyc_value(suggestion))
    )
    return (
        suggestion if accepted else gst,
        m.canonical_udyam(
            resolve(
                b.udyam_number,
                ud.get("udyam_candidates", []),
                p.get("udyam_number", ""),
            )
        )
        or "",
        accepted,
    )


@router.post("/api/kyc/verify")
def verify(b: m.VerifyKyc):
    jid = b.journey_id
    with m.con(write=True) as db:
        if db.execute("SELECT 1 FROM orders WHERE journey_id=?", (jid,)).fetchone():
            raise HTTPException(409, "Submitted order is locked")
        if not has(db, jid, "kyc"):
            raise HTTPException(403, "Grant KYC consent before verification")
        p = m.parse(
            db.execute("SELECT profile FROM journeys WHERE id=?", (jid,)).fetchone()[0]
        )
        ds = [
            m.parse(x[0])
            for x in db.execute(
                "SELECT extracted FROM documents WHERE journey_id=? AND confirmed=1 ORDER BY rowid",
                (jid,),
            )
        ]
        gst, ud, suggested = identifiers(ds, p, b)
        old = m.row(db, "SELECT status,gstin,udyam FROM kyc WHERE journey_id=?", (jid,))
        if (
            old
            and old["status"] == "VERIFIED"
            and (old["gstin"] != gst or old["udyam"] != ud)
        ):
            raise HTTPException(409, "Verified identity is locked")
        g = m.GST.get(gst)
        u = m.UDYAM.get(ud)
        q = m.row(
            db,
            "SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1",
            (jid,),
        )
        quote = q if q and q["accepted"] and q["valid"] else None
        owner = m.row(db, "SELECT journey_id FROM claims WHERE gstin=?", (gst,))
        reuse = db.execute(
            "SELECT 1 FROM document_hashes a JOIN document_hashes b ON a.digest=b.digest AND a.journey_id!=b.journey_id WHERE a.journey_id=? LIMIT 1",
            (jid,),
        ).fetchone()
        status, reasons, notes = assess(
            ds,
            gst,
            ud,
            g,
            u,
            [r for r in m.LEADS if gst and r["gstin"] == gst],
            m.TODAY,
            p,
            quote,
            has(db, jid, "kyc"),
            bool(reuse or (owner and owner["journey_id"] != jid)),
            suggested,
            b.legal_name,
        )
        if db.execute(
            "SELECT 1 FROM documents WHERE journey_id=? AND confirmed=0", (jid,)
        ).fetchone():
            status = "ACTION_REQUIRED"
            reasons.append("Confirm every uploaded document before verifying")
        if status != "ACTION_REQUIRED":
            p.update(gstin=gst, udyam_number=ud)
            db.execute("UPDATE journeys SET profile=? WHERE id=?", (m.j(p), jid))
            if not owner:
                db.execute("DELETE FROM claims WHERE journey_id=?", (jid,))
                db.execute("INSERT INTO claims VALUES(?,?)", (gst, jid))
        db.execute(
            "INSERT OR REPLACE INTO kyc VALUES(?,?,?,?,?,?)",
            (jid, status, m.j(reasons), gst, ud, m.now()),
        )
        revision = m.uid("review")
        db.execute(
            "INSERT OR REPLACE INTO bindings VALUES(?,?,?,?)",
            (jid, quote["id"] if quote else None, m.facts(p), revision),
        )
        if status == "REVIEW_REQUIRED":
            auto_handoff(db, jid, "KYC_REVIEW")
    return {
        "status": status,
        "reasons": reasons,
        "notes": notes,
        "revision": revision,
        "registry": {
            "gstin_status": g["status"] if g else "not_found",
            "udyam_found": bool(u),
        },
        "fixture_date": str(m.TODAY),
    }


class Decision(BaseModel):
    revision: str
    decision: Literal["approve", "reject"]
    rationale: str = Field(min_length=5, max_length=500)


def review_basis(db, jid, gstin):
    docs = [
        m.parse(r[0])
        for r in db.execute(
            "SELECT extracted FROM documents WHERE journey_id=? AND confirmed=1 ORDER BY rowid",
            (jid,),
        )
    ]
    selected = {d.get("doc_type"): d.get("fields", {}) for d in docs}
    return {
        "certificate_legal_name": selected.get("gst_certificate", {}).get("legal_name"),
        "registry_legal_name": m.GST.get(gstin, {}).get("legal_name"),
        "document_signatory": selected.get("signatory_id", {}).get("name"),
        "expected_applicants": [
            r["contact_name"] for r in m.LEADS if r["gstin"] == gstin
        ],
        "checksum_valid_as_read": selected.get("gst_certificate", {}).get(
            "gstin_checksum_valid_as_read"
        ),
    }


@router.get("/api/partners/reviews")
def reviews():
    with m.con() as db:
        rows = db.execute(
            """SELECT k.journey_id,k.gstin,k.udyam,k.reasons,b.revision,b.quote_id FROM kyc k JOIN bindings b USING(journey_id)
                            JOIN agreements a ON a.journey_id=k.journey_id AND a.purpose='partner' WHERE k.status='REVIEW_REQUIRED'"""
        )
        return [
            {
                **dict(r),
                "reasons": m.parse(r["reasons"]),
                "gstin": m.mask_kyc_value(r["gstin"]),
                "udyam": m.mask_kyc_value(r["udyam"]),
                "evidence": review_basis(db, r["journey_id"], r["gstin"]),
            }
            for r in rows
        ]


@router.post("/api/partners/reviews/{jid}")
def decide(jid: str, b: Decision):
    with m.con(write=True) as db:
        r = m.row(
            db,
            "SELECT k.*,b.snapshot,b.quote_id,b.revision FROM kyc k JOIN bindings b USING(journey_id) WHERE k.journey_id=?",
            (jid,),
        )
        if not has(db, jid, "partner") or not has(db, jid, "kyc"):
            raise HTTPException(403, "Both consents are required")
        if not r or r["status"] != "REVIEW_REQUIRED" or r["revision"] != b.revision:
            raise HTTPException(409, "Review case changed")
        if b.decision == "approve":
            owner = m.row(
                db, "SELECT journey_id FROM claims WHERE gstin=?", (r["gstin"],)
            )
            if owner and owner["journey_id"] != jid:
                raise HTTPException(409, "Another active journey holds this GSTIN")
            db.execute("INSERT OR IGNORE INTO claims VALUES(?,?)", (r["gstin"], jid))
        status = "VERIFIED" if b.decision == "approve" else "ACTION_REQUIRED"
        rationale = m.redact_chat(b.rationale.strip())
        if len(rationale) < 5:
            raise HTTPException(422, "Explain the decision")
        db.execute(
            "UPDATE kyc SET status=?,reasons=? WHERE journey_id=?",
            (status, m.j([rationale]), jid),
        )
        db.execute(
            "INSERT INTO review_audit(journey_id,revision,decision,rationale,actor,created_at,basis) VALUES(?,?,?,?,?,?,?)",
            (
                jid,
                r["revision"],
                b.decision,
                rationale,
                "authenticated partner",
                m.now(),
                m.j(
                    {
                        "quote_id": r["quote_id"],
                        "reasons": m.parse(r["reasons"]),
                        "evidence": review_basis(db, jid, r["gstin"]),
                    }
                ),
            ),
        )
    return {"status": status}


@router.get("/api/partners/reviews/{jid}/audit")
def audit(jid: str):
    with m.con() as db:
        if not has(db, jid, "partner"):
            raise HTTPException(403, "Partner consent required")
        return [
            dict(r)
            for r in db.execute(
                "SELECT revision,decision,rationale,actor,created_at,basis FROM review_audit WHERE journey_id=? ORDER BY id",
                (jid,),
            )
        ]


class Lookup(BaseModel):
    gstin: str = Field(max_length=32)


@router.post("/api/journeys/{jid}/registry")
def lookup(jid: str, b: Lookup):
    with m.con() as db:
        if not has(db, jid, "kyc"):
            raise HTTPException(403, "KYC consent required")
        g = m.GST.get(b.gstin.upper().strip())
        if not g:
            raise HTTPException(404, "No mock registry match")
        return {
            "gstin": m.mask_kyc_value(g["gstin"]),
            "name": g["legal_name"],
            "constitution": g["constitution"],
            "state": g["state"],
            "status": g["status"],
        }
