from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing

DB_PATH = os.getenv("V11_STATE_DB", "v11_state.db")
_RESTOCK_FLAGS: dict[str, bool] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_presence (
  fingerprint TEXT PRIMARY KEY,
  last_seen REAL NOT NULL,
  last_price REAL,
  priority_class TEXT,
  priority_route TEXT,
  last_priority_at REAL,
  last_restock_alert REAL
);
CREATE TABLE IF NOT EXISTS benchmark_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  stage TEXT NOT NULL,
  ts REAL NOT NULL,
  store TEXT,
  title TEXT,
  price REAL,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_bench_fp_ts ON benchmark_events(fingerprint, ts);
CREATE INDEX IF NOT EXISTS idx_bench_stage_ts ON benchmark_events(stage, ts);
"""


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_state():
    with closing(_connect()) as con:
        con.executescript(SCHEMA)
        con.commit()


def observe_before_save(fp: str, deal) -> bool:
    """Detect a high-value restock from existing scan observations only.

    No network call is performed. A restock is eligible only when this product
    was previously sent as a private-priority review and has been absent long
    enough to avoid ordinary scan gaps.
    """
    init_state()
    now = time.time()
    min_gap = max(3600.0, float(os.getenv("RESTOCK_MIN_GAP_SECONDS", "21600")))
    cooldown = max(min_gap, float(os.getenv("RESTOCK_ALERT_COOLDOWN_SECONDS", "43200")))
    price = float(getattr(deal, "current_price", 0) or 0)
    with closing(_connect()) as con:
        row = con.execute("SELECT * FROM product_presence WHERE fingerprint=?", (fp,)).fetchone()
        restock = False
        if row is not None:
            gap = now - float(row["last_seen"] or 0)
            last_alert = float(row["last_restock_alert"] or 0)
            was_priority = bool(row["priority_class"] or row["priority_route"])
            restock = bool(was_priority and gap >= min_gap and (now - last_alert) >= cooldown)
        if row is None:
            con.execute(
                "INSERT INTO product_presence(fingerprint,last_seen,last_price) VALUES(?,?,?)",
                (fp, now, price),
            )
        else:
            con.execute(
                "UPDATE product_presence SET last_seen=?,last_price=?,last_restock_alert=CASE WHEN ? THEN ? ELSE last_restock_alert END WHERE fingerprint=?",
                (now, price, 1 if restock else 0, now, fp),
            )
        con.commit()
    if restock:
        _RESTOCK_FLAGS[fp] = True
    else:
        _RESTOCK_FLAGS.setdefault(fp, False)
    return bool(_RESTOCK_FLAGS.get(fp))


def is_restock(fp: str) -> bool:
    return bool(_RESTOCK_FLAGS.get(fp))


def clear_cycle_flag(fp: str):
    _RESTOCK_FLAGS.pop(fp, None)


def record_priority(fp: str, truth: dict | None):
    init_state()
    truth = truth or {}
    now = time.time()
    with closing(_connect()) as con:
        con.execute(
            """INSERT INTO product_presence(fingerprint,last_seen,priority_class,priority_route,last_priority_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(fingerprint) DO UPDATE SET
                 priority_class=excluded.priority_class,
                 priority_route=excluded.priority_route,
                 last_priority_at=excluded.last_priority_at""",
            (fp, now, str(truth.get("class") or ""), str(truth.get("route") or ""), now),
        )
        con.commit()


def record_event(fp: str, stage: str, deal=None, meta: dict | None = None):
    init_state()
    try:
        store = str(getattr(deal, "store", "") or "") if deal is not None else ""
        title = str(getattr(deal, "title", "") or "")[:500] if deal is not None else ""
        price = float(getattr(deal, "current_price", 0) or 0) if deal is not None else 0.0
        payload = json.dumps(meta or {}, ensure_ascii=False, separators=(",", ":"))[:5000]
        with closing(_connect()) as con:
            con.execute(
                "INSERT INTO benchmark_events(fingerprint,stage,ts,store,title,price,meta) VALUES(?,?,?,?,?,?,?)",
                (fp, str(stage), time.time(), store, title, price, payload),
            )
            con.commit()
    except Exception:
        # Telemetry must never break deal detection.
        return


def summary(limit: int = 20) -> dict:
    init_state()
    with closing(_connect()) as con:
        counts = {
            row["stage"]: int(row["c"])
            for row in con.execute("SELECT stage,COUNT(*) c FROM benchmark_events GROUP BY stage")
        }
        recent = [dict(x) for x in con.execute(
            "SELECT fingerprint,stage,ts,store,title,price FROM benchmark_events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )]
        restocks = int(con.execute("SELECT COUNT(*) c FROM product_presence WHERE last_restock_alert IS NOT NULL").fetchone()["c"])
    return {"event_counts": counts, "restock_alerts": restocks, "recent": recent}
