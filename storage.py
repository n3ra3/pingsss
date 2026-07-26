"""Persistence layer. Uses Postgres when DATABASE_URL is set, else local SQLite.

Kept intentionally small: one global connection guarded by a lock, which is
plenty for a single-user bot with two background threads.
"""
import os
import threading

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("SQLITE_PATH", "data.db")

_lock = threading.Lock()
_conn = None
PH = "%s" if IS_PG else "?"


def _connect():
    global _conn
    if IS_PG:
        import psycopg
        _conn = psycopg.connect(DATABASE_URL, autocommit=True)
    else:
        import sqlite3
        _conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    return _conn


def _commit():
    if not IS_PG:
        _conn.commit()


def init_db():
    _connect()
    serial = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if IS_PG else "REAL"
    ts = "TIMESTAMPTZ DEFAULT now()" if IS_PG else "TEXT DEFAULT CURRENT_TIMESTAMP"
    with _lock:
        cur = _conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS items (
                id {serial},
                chat_id BIGINT,
                appid TEXT,
                market_hash_name TEXT,
                name TEXT,
                url TEXT,
                order_price_cents INTEGER,
                margin_pct {real},
                last_alert_cents INTEGER,
                created_at {ts}
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )""")
        # migration for DBs created before last_alert_cents existed
        try:
            cur.execute("ALTER TABLE items ADD COLUMN last_alert_cents INTEGER")
            _commit()
        except Exception:
            pass
        _commit()


def add_item(chat_id, appid, market_hash_name, name, url, order_price_cents, margin_pct):
    with _lock:
        cur = _conn.cursor()
        sql = (f"INSERT INTO items (chat_id, appid, market_hash_name, name, url, "
               f"order_price_cents, margin_pct) VALUES "
               f"({PH},{PH},{PH},{PH},{PH},{PH},{PH})")
        params = (chat_id, appid, market_hash_name, name, url,
                  order_price_cents, margin_pct)
        if IS_PG:
            cur.execute(sql + " RETURNING id", params)
            new_id = cur.fetchone()[0]
        else:
            cur.execute(sql, params)
            new_id = cur.lastrowid
        _commit()
        return new_id


def _row_to_item(row):
    return {
        "id": row[0], "chat_id": row[1], "appid": row[2],
        "market_hash_name": row[3], "name": row[4], "url": row[5],
        "order_price_cents": row[6], "margin_pct": row[7],
        "last_alert_cents": row[8],
    }


_COLS = ("id, chat_id, appid, market_hash_name, name, url, "
         "order_price_cents, margin_pct, last_alert_cents")


def list_items(chat_id):
    with _lock:
        cur = _conn.cursor()
        cur.execute(f"SELECT {_COLS} FROM items WHERE chat_id={PH} ORDER BY id",
                    (chat_id,))
        return [_row_to_item(r) for r in cur.fetchall()]


def all_items():
    with _lock:
        cur = _conn.cursor()
        cur.execute(f"SELECT {_COLS} FROM items ORDER BY id")
        return [_row_to_item(r) for r in cur.fetchall()]


def remove_item(item_id, chat_id):
    with _lock:
        cur = _conn.cursor()
        cur.execute(f"DELETE FROM items WHERE id={PH} AND chat_id={PH}",
                    (item_id, chat_id))
        _commit()
        return cur.rowcount > 0


def update_last_alert(item_id, cents):
    """Remember the price we last alerted for this item (None = out of range)."""
    with _lock:
        cur = _conn.cursor()
        cur.execute(f"UPDATE items SET last_alert_cents={PH} WHERE id={PH}",
                    (cents, item_id))
        _commit()


def set_meta(key, value):
    with _lock:
        cur = _conn.cursor()
        if IS_PG:
            cur.execute(
                f"INSERT INTO meta (key, value) VALUES ({PH},{PH}) "
                f"ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, str(value)))
        else:
            cur.execute(
                f"INSERT OR REPLACE INTO meta (key, value) VALUES ({PH},{PH})",
                (key, str(value)))
        _commit()


def get_meta(key, default=None):
    with _lock:
        cur = _conn.cursor()
        cur.execute(f"SELECT value FROM meta WHERE key={PH}", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def count_items():
    with _lock:
        cur = _conn.cursor()
        cur.execute("SELECT COUNT(*) FROM items")
        return cur.fetchone()[0]
