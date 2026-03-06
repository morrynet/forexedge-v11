"""
ForexEdge — Database Migration System
Run: python migrations.py  OR  called automatically from app.py startup.

Each migration is a list of SQL statements tagged with a version number.
Already-applied migrations are skipped via the schema_version table.
"""
import os, logging, datetime
log = logging.getLogger("forexedge.migrations")

USE_PG = bool(os.environ.get("DATABASE_URL"))
PH = "%s" if USE_PG else "?"

def _db():
    if USE_PG:
        import psycopg2, psycopg2.extras
        url = os.environ["DATABASE_URL"].replace("postgres://","postgresql://",1)
        return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    import sqlite3
    path = os.environ.get("DB_PATH","saas.db")
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _col(t):
    """BOOLEAN/AUTOINCREMENT compat."""
    if USE_PG:
        return t.replace("INTEGER PRIMARY KEY AUTOINCREMENT","SERIAL PRIMARY KEY") \
                .replace(" INTEGER DEFAULT 1"," BOOLEAN DEFAULT TRUE") \
                .replace(" INTEGER DEFAULT 0"," BOOLEAN DEFAULT FALSE")
    return t

# ─── Migration definitions ────────────────────────────────────────────────────
# Format: (version_int, description, [sql_statements])
MIGRATIONS = [

    (1, "Initial schema", [
        _col("""CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            category TEXT DEFAULT 'EA', filename TEXT, filedata BLOB,
            active INTEGER DEFAULT 1, price REAL DEFAULT 20.0,
            created_at TEXT)"""),
        _col("""CREATE TABLE IF NOT EXISTS purchases (
            id TEXT PRIMARY KEY, product_id TEXT, email TEXT,
            txn_id TEXT UNIQUE, download_token TEXT UNIQUE,
            downloads_used INTEGER DEFAULT 0, max_downloads INTEGER DEFAULT 3,
            confirmed INTEGER DEFAULT 0, amount_usd REAL DEFAULT 0,
            coupon_code TEXT, referral_code TEXT, ip_address TEXT,
            created_at TEXT, confirmed_at TEXT, token_expires_at TEXT)"""),
        _col("""CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
            expiry TEXT NOT NULL, account TEXT, sub_id TEXT,
            plan TEXT DEFAULT 'monthly', email TEXT, created_at TEXT)"""),
        _col("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT,
            event_type TEXT, payload TEXT, created_at TEXT)"""),
        _col("""CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
            discount_pct INTEGER DEFAULT 0, discount_usd REAL DEFAULT 0,
            max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1, expires_at TEXT, created_at TEXT)"""),
        _col("""CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
            name TEXT, email TEXT, paypal_email TEXT,
            commission_usd REAL DEFAULT 5.0,
            total_sales INTEGER DEFAULT 0, total_earned REAL DEFAULT 0,
            active INTEGER DEFAULT 1, created_at TEXT)"""),
        _col("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT,
            action TEXT NOT NULL, target TEXT, detail TEXT,
            ip_address TEXT, created_at TEXT)"""),
    ]),

    (2, "Add payouts table", [
        _col("""CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referral_code TEXT NOT NULL, paypal_email TEXT,
            period TEXT NOT NULL, amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            notes TEXT, paid_at TEXT, created_at TEXT)"""),
    ]),

    (3, "Add indexes for performance", [
        "CREATE INDEX IF NOT EXISTS idx_purchases_txn    ON purchases(txn_id)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_email  ON purchases(email)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_ref    ON purchases(referral_code)",
        "CREATE INDEX IF NOT EXISTS idx_purchases_conf   ON purchases(confirmed,confirmed_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_action     ON audit_log(action,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_licenses_key     ON licenses(key)",
        "CREATE INDEX IF NOT EXISTS idx_payouts_period   ON payouts(period,status)",
    ]),

    (4, "Add download_token index for fast lookups", [
        "CREATE INDEX IF NOT EXISTS idx_purchases_token ON purchases(download_token)",
    ]),

    (5, "Add referral active column if missing (safe ALTER)", [
        # SQLite doesn't support IF NOT EXISTS on columns — use try/except in code
    ]),

    # Future migrations go here as new tuples.
    # (6, "Add something", ["ALTER TABLE products ADD COLUMN ..."])
]


def run_migrations(verbose: bool = True):
    """Apply all pending migrations in order."""
    conn = _db()
    try:
        cur = conn.cursor()

        # Ensure version table exists
        cur.execute("""CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at TEXT
        )""")
        conn.commit()

        # Get current version
        cur.execute("SELECT COALESCE(MAX(version),0) AS v FROM schema_version")
        row = cur.fetchone()
        current = dict(row)["v"] if row else 0

        applied = 0
        for version, desc, statements in MIGRATIONS:
            if version <= current:
                continue

            if verbose:
                log.info("Migration %d: %s", version, desc)

            for sql in statements:
                if not sql.strip():
                    continue
                try:
                    cur.execute(sql)
                except Exception as e:
                    # Some ALTER TABLE statements may fail if column already exists
                    # (e.g. when re-running on existing DB). Log and continue.
                    log.warning("Migration %d stmt warning: %s | sql=%s", version, e, sql[:80])

            cur.execute(
                "INSERT INTO schema_version(version,description,applied_at) VALUES(?,?,?)"
                if not USE_PG else
                "INSERT INTO schema_version(version,description,applied_at) VALUES(%s,%s,%s)",
                (version, desc, datetime.datetime.utcnow().isoformat())
            )
            conn.commit()
            applied += 1

        if verbose and applied:
            log.info("Applied %d migration(s). DB at version %d.", applied, version)
        elif verbose:
            log.info("DB up to date (version %d).", current)

    finally:
        conn.close()

    return applied


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    n = run_migrations()
    print(f"Done — {n} migration(s) applied.")
    sys.exit(0)
