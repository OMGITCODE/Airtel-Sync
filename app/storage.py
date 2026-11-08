"""SQLite migrations for access, verification evidence and the order lifecycle."""

import json
import secrets

DEPENDENTS = (
    "tokens",
    "claims",
    "bindings",
    "review_audit",
    "document_hashes",
    "agreements",
    "quote_terms",
    "fulfilment",
    "notices",
    "grievances",
    "archived_orders",
    "milestones",
)


def migrate(c):
    c.executescript(
        """
    CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tokens(journey_id TEXT PRIMARY KEY,nonce TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS claims(gstin TEXT PRIMARY KEY,journey_id TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS document_hashes(doc_id TEXT PRIMARY KEY,journey_id TEXT NOT NULL,digest TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS hash_lookup ON document_hashes(digest);
    CREATE TABLE IF NOT EXISTS bindings(journey_id TEXT PRIMARY KEY,quote_id TEXT,snapshot TEXT,revision TEXT);
    CREATE TABLE IF NOT EXISTS review_audit(id INTEGER PRIMARY KEY,journey_id TEXT,revision TEXT,decision TEXT,rationale TEXT,actor TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS agreements(journey_id TEXT,purpose TEXT,recorded_at TEXT,PRIMARY KEY(journey_id,purpose));
    CREATE TABLE IF NOT EXISTS quote_terms(quote_id TEXT PRIMARY KEY,journey_id TEXT,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fulfilment(journey_id TEXT PRIMARY KEY,order_id TEXT,expected TEXT,scheduled TEXT);
    CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY,journey_id TEXT,channel TEXT,message TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS milestones(id INTEGER PRIMARY KEY,journey_id TEXT,kind TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS grievances(id TEXT PRIMARY KEY,journey_id TEXT,message TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS archived_orders(id TEXT PRIMARY KEY,journey_id TEXT,payload TEXT);
    """
    )
    c.execute(
        "INSERT OR IGNORE INTO settings VALUES('signing_key',?)",
        (secrets.token_hex(32),),
    )
    # Keep a complete snapshot of each legacy duplicate, including its event stream.
    for r in c.execute(
        "SELECT * FROM orders WHERE rowid NOT IN (SELECT MIN(rowid) FROM orders GROUP BY quote_id)"
    ).fetchall():
        events = [
            dict(e)
            for e in c.execute(
                "SELECT * FROM order_events WHERE order_id=?", (r["id"],)
            )
        ]
        c.execute(
            "INSERT INTO archived_orders VALUES(?,?,?)",
            (
                r["id"],
                r["journey_id"],
                json.dumps({"order": dict(r), "events": events}),
            ),
        )
        c.execute("DELETE FROM order_events WHERE order_id=?", (r["id"],))
        c.execute("DELETE FROM orders WHERE id=?", (r["id"],))
    columns = {r[1] for r in c.execute("PRAGMA table_info(review_audit)")}
    if "basis" not in columns:
        c.execute("ALTER TABLE review_audit ADD COLUMN basis TEXT DEFAULT '{}'")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS one_order_per_quote ON orders(quote_id)"
    )
    c.execute(
        "UPDATE kyc SET status='ACTION_REQUIRED',reasons='[\"Verify again to bind evidence to the current quote\"]' WHERE status='VERIFIED' AND journey_id NOT IN (SELECT journey_id FROM bindings)"
    )
