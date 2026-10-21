import sqlite3
from coupon_sizes import validate_size
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent / "data" / "coupons.db"

SEED_COUPONS = []

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS coupon_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coupon_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            value TEXT NOT NULL CHECK(value IN ('1st','Égal','2nd')),
            FOREIGN KEY(coupon_id) REFERENCES coupons(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_coupon_items_coupon
            ON coupon_items(coupon_id, position);
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        con.execute("PRAGMA foreign_keys = ON")

def seed_if_empty():
    """Compatibility no-op: never recreate a purged database or seed manual data."""
    return None

def add_coupon(sequence, note=""):
    validate_size(len(sequence))
    if any(v not in ("1st", "Égal", "2nd") for v in sequence):
        raise ValueError("Résultat de match invalide.")
    init_db()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO coupons(created_at, note) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="seconds"), note.strip())
        )
        coupon_id = cur.lastrowid
        for pos, value in enumerate(sequence, start=1):
            con.execute(
                "INSERT INTO coupon_items(coupon_id, position, value) VALUES (?, ?, ?)",
                (coupon_id, pos, value)
            )
        return coupon_id

def load_coupons():
    init_db()
    with connect() as con:
        rows = con.execute("""
            SELECT c.id, c.created_at, c.note, i.position, i.value
            FROM coupons c
            JOIN coupon_items i ON i.coupon_id = c.id
            ORDER BY c.id ASC, i.position ASC
        """).fetchall()
    by_id = {}
    for r in rows:
        item = by_id.setdefault(r["id"], {
            "id": r["id"],
            "created_at": r["created_at"],
            "note": r["note"],
            "sequence": []
        })
        item["sequence"].append(r["value"])
    return list(by_id.values())

def delete_coupon(coupon_id):
    init_db()
    with connect() as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("DELETE FROM coupons WHERE id=?", (int(coupon_id),))

def clear_all(keep_seed_flag=True):
    init_db()
    with connect() as con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("DELETE FROM coupon_items")
        con.execute("DELETE FROM coupons")
        if not keep_seed_flag:
            con.execute("DELETE FROM meta WHERE key='seeded'")

def import_coupons(sequences, note="Import CSV"):
    sequences = list(sequences)
    for seq in sequences:
        validate_size(len(seq))
        if any(v not in ("1st", "Égal", "2nd") for v in seq):
            raise ValueError("Résultat de match invalide.")
    for seq in sequences:
        add_coupon(seq, note=note)
    return len(sequences)
