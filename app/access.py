"""Scoped credentials for the local demo. No browser receives staff keys from the server."""

import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from threading import Lock
from fastapi import HTTPException, Request

PUBLIC = {"/", "/privacy", "/api/catalogue", "/api/needs", "/api/health"}
_buckets = {}
_lock = Lock()


def limit(key, count, seconds):
    now = time.monotonic()
    with _lock:
        for old in list(_buckets):
            if _buckets[old][1] <= now:
                del _buckets[old]
        bucket, _ = _buckets.setdefault(key, (deque(), now + seconds))
        while bucket and bucket[0] <= now - seconds:
            bucket.popleft()
        if len(bucket) >= count:
            raise HTTPException(
                429,
                "Request limit reached. Try again later.",
                headers={"Retry-After": str(seconds)},
            )
        bucket.append(now)
        _buckets[key] = (bucket, now + seconds)


def issue(c, jid):
    nonce = secrets.token_hex(16)
    c.execute("INSERT INTO tokens VALUES(?,?)", (jid, nonce))
    return signed(c, jid, nonce)


def signed(c, jid, nonce):
    secret = c.execute(
        "SELECT value FROM settings WHERE name='signing_key'"
    ).fetchone()[0]
    mac = hmac.new(
        secret.encode(), (jid + ":" + nonce).encode(), hashlib.sha256
    ).hexdigest()
    return jid + "." + mac


def staff(request, role):
    secret = os.getenv("ONEJOURNEY_" + role.upper() + "_KEY", "")
    other = "OPS" if role == "partner" else "PARTNER"
    if not secret or secret == os.getenv("ONEJOURNEY_" + other + "_KEY"):
        raise HTTPException(
            503, "Configure distinct partner and operations keys on the server."
        )
    if not hmac.compare_digest(
        request.headers.get("X-" + role + "-Key", "").encode(), secret.encode()
    ):
        raise HTTPException(403, role.capitalize() + " credential required")


def authorizer(connect):
    async def guard(request: Request):
        path = request.url.path
        if not path.startswith("/api/"):
            return
        peer = request.client.host if request.client else "local"
        if path in PUBLIC:
            return
        if path == "/api/journeys" and request.method == "POST":
            limit(
                ("starts", peer),
                int(os.getenv("ONEJOURNEY_JOURNEYS_PER_HOUR", "30")),
                3600,
            )
            return
        if path.startswith("/api/mock/ops/"):
            return staff(request, "ops")
        if path.startswith("/api/partners/") or path == "/api/metrics":
            return staff(request, "partner")
        # Verify the token before reading identity, documents, or changing any state.
        auth = request.headers.get("authorization", "").split()
        if len(auth) != 2 or auth[0].lower() != "bearer" or len(auth[1]) > 150:
            raise HTTPException(401, "A journey access token is required")
        jid = auth[1].partition(".")[0]
        with connect() as db:
            record = db.execute(
                "SELECT t.nonce FROM tokens t JOIN journeys j ON j.id=t.journey_id WHERE j.id=? AND julianday('now')-julianday(j.created_at)<7",
                (jid,),
            ).fetchone()
            if not record or not hmac.compare_digest(
                auth[1].encode(), signed(db, jid, record[0]).encode()
            ):
                raise HTTPException(401, "Expired or invalid journey token")
            owner = request.path_params.get("jid")
            for arg, table in [
                ("qid", "quotes"),
                ("did", "documents"),
                ("oid", "orders"),
            ]:
                if arg in request.path_params:
                    r = db.execute(
                        f"SELECT journey_id FROM {table} WHERE id=?",
                        (request.path_params[arg],),
                    ).fetchone()
                    owner = r[0] if r else ""
            if owner is None:
                if path in ("/api/demo", "/api/demo/document"):
                    owner = jid
                elif "form" in request.headers.get("content-type", ""):
                    owner = (await request.form()).get("journey_id")
                elif request.method != "GET":
                    body = await request.json()
                    owner = body.get("journey_id") if isinstance(body, dict) else None
            if owner != jid:
                raise HTTPException(403, "This resource does not belong to the journey")
        request.state.journey_id = jid

    return guard


class BodyLimit:
    """Bound streamed request bodies before multipart parsing or OCR."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            peer = (scope.get("client") or ("local",))[0]
            try:
                limit(
                    ("requests", peer),
                    int(os.getenv("ONEJOURNEY_REQUESTS_PER_MIN", "240")),
                    60,
                )
            except HTTPException as error:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", b"60"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Request limit reached"}',
                    }
                )
                return
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT"}:
            return await self.app(scope, receive, send)
        maximum = (
            4 * 1024 * 1024 + 65536 if scope["path"] == "/api/kyc/documents" else 65536
        )
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > maximum:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Request body too large"}',
                    }
                )
                return
            body.extend(chunk)
            if not message.get("more_body"):
                break
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        return await self.app(scope, replay, send)
