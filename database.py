import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "restaurant.db")

CREATE_CUSTOMERS_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number    TEXT    NOT NULL UNIQUE,
    first_name      TEXT    NOT NULL,
    last_name       TEXT    NOT NULL,
    default_address TEXT,
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_CARTS_SQL = """
CREATE TABLE IF NOT EXISTS carts (
    cart_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number       TEXT    NOT NULL,
    order_type         TEXT    NOT NULL CHECK(order_type IN ('pickup', 'delivery')),
    customer_name      TEXT    NOT NULL,
    delivery_address   TEXT,
    status             TEXT    NOT NULL DEFAULT 'active'
                                CHECK(status IN ('active', 'confirmed', 'cancelled')),
    clover_order_id    TEXT,
    confirmed_at       TEXT,
    scheduled_for      TEXT,
    scheduled_status   TEXT    NOT NULL DEFAULT 'not_scheduled'
                                CHECK(scheduled_status IN
                                      ('not_scheduled','pending','released','cancelled')),
    scheduled_timezone TEXT    NOT NULL DEFAULT 'America/New_York',
    coupon_type            TEXT,                  -- 'percent' | 'flat' | NULL
    coupon_value           REAL    NOT NULL DEFAULT 0.0,
    coupon_description     TEXT,                  -- what the caller said, e.g. "10% off"
    raw_delivery_address   TEXT,                  -- exactly what the caller said
    address_confidence     TEXT    NOT NULL DEFAULT 'high',  -- 'high' | 'low'
    created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

CREATE_CART_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS cart_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cart_id        INTEGER NOT NULL REFERENCES carts(cart_id),
    item_name      TEXT    NOT NULL,
    size           TEXT,
    quantity       INTEGER NOT NULL CHECK(quantity >= 1),
    unit_price     REAL    NOT NULL CHECK(unit_price >= 0),
    line_total     REAL    NOT NULL,
    modifiers_json TEXT    NOT NULL DEFAULT '[]',
    notes          TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def _migrate(conn) -> None:
    """
    Add columns that did not exist in earlier schema versions.
    Uses PRAGMA table_info so it is safe to run on any existing DB.
    """
    cart_cols = {row[1] for row in conn.execute("PRAGMA table_info(carts)").fetchall()}
    if "clover_order_id" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN clover_order_id TEXT")
    if "confirmed_at" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN confirmed_at TEXT")
    if "scheduled_for" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN scheduled_for TEXT")
    if "scheduled_status" not in cart_cols:
        conn.execute(
            "ALTER TABLE carts ADD COLUMN scheduled_status TEXT "
            "NOT NULL DEFAULT 'not_scheduled'"
        )
    if "scheduled_timezone" not in cart_cols:
        conn.execute(
            "ALTER TABLE carts ADD COLUMN scheduled_timezone TEXT "
            "NOT NULL DEFAULT 'America/New_York'"
        )
    if "coupon_type" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN coupon_type TEXT")
    if "coupon_value" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN coupon_value REAL NOT NULL DEFAULT 0.0")
    if "coupon_description" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN coupon_description TEXT")
    if "raw_delivery_address" not in cart_cols:
        conn.execute("ALTER TABLE carts ADD COLUMN raw_delivery_address TEXT")
    if "address_confidence" not in cart_cols:
        conn.execute(
            "ALTER TABLE carts ADD COLUMN address_confidence TEXT "
            "NOT NULL DEFAULT 'high'"
        )


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(CREATE_CUSTOMERS_SQL)
        conn.execute(CREATE_CARTS_SQL)
        conn.execute(CREATE_CART_ITEMS_SQL)
        _migrate(conn)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
