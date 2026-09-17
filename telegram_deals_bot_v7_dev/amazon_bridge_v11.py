from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
import statistics
from pathlib import Path

from models import Deal

log = logging.getLogger("amazon-bridge-v11")

SOURCE_DIR = Path(
    os.getenv("AMAZON_SOURCE_DIR")
    or str(Path.home() / "telegram_deals_bot_v1")
)
SOURCE_DB = SOURCE_DIR / "deals.db"
SOURCE_LOG = SOURCE_DIR / "amazon_radar.log"
STATE_FILE = Path(__file__).with_name(".amazon_bridge_v11_state.json")

POLL_SECONDS = max(15, int(os.getenv("AMAZON_BRIDGE_POLL_SECONDS", "30")))
MAX_SEND_PER_POLL = max(1, min(5, int(os.getenv("AMAZON_BRIDGE_MAX_SEND", "2"))))


def _num(value, default=0.0):
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _discount(current, reference):
    current = _num(current)
    reference = _num(reference)
    if reference > current > 0:
        return (reference - current) / reference * 100.0
    return 0.0


def _asin(payload):
    ext = str(payload.get("external_id") or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{10}", ext):
        return ext
    url = str(payload.get("url") or "")
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url, re.I)
    return m.group(1).upper() if m else ext or None


def _is_amazon(payload):
    store = str(payload.get("store") or "").lower()
    url = str(payload.get("url") or "").lower()
    return "amazon" in store or "amazon.eg" in url


def _connect_ro():
    con = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def _read_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "updated_at": str(data.get("updated_at") or ""),
            "fingerprint": str(data.get("fingerprint") or ""),
        }
    except Exception:
        return {"updated_at": "", "fingerprint": ""}


def _write_state(updated_at, fingerprint):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"updated_at": updated_at, "fingerprint": fingerprint},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def _source_cursor_now():
    sql = (
        "SELECT updated_at, fingerprint "
        "FROM pending_deals "
        "ORDER BY updated_at DESC, fingerprint DESC "
        "LIMIT 1"
    )
    with _connect_ro() as con:
        row = con.execute(sql).fetchone()
    if not row:
        return "", ""
    return str(row["updated_at"] or ""), str(row["fingerprint"] or "")


def _new_rows(cursor, limit=150):
    ts = cursor.get("updated_at") or ""
    fp = cursor.get("fingerprint") or ""
    sql = (
        "SELECT fingerprint,payload,status,created_at,updated_at "
        "FROM pending_deals "
        "WHERE updated_at > ? "
        "OR (updated_at = ? AND fingerprint > ?) "
        "ORDER BY updated_at ASC, fingerprint ASC "
        "LIMIT ?"
    )
    with _connect_ro() as con:
        rows = con.execute(sql, (ts, ts, fp, int(limit))).fetchall()
    return [dict(r) for r in rows]


def _latest_amazon_preview():
    sql = (
        "SELECT fingerprint,payload,status,updated_at "
        "FROM pending_deals "
        "ORDER BY updated_at DESC, fingerprint DESC "
        "LIMIT 100"
    )
    with _connect_ro() as con:
        rows = con.execute(sql).fetchall()

    for row in rows:
        try:
            p = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if not _is_amazon(p):
            continue
        return {
            "title": str(p.get("title") or "")[:100],
            "price": _num(p.get("current_price")),
            "discount": _num(p.get("discount_percent")),
            "image": bool(p.get("image_url")),
            "updated_at": row["updated_at"],
        }
    return None


def _recent_log_text(max_bytes=2_000_000):
    try:
        size = SOURCE_LOG.stat().st_size
        with SOURCE_LOG.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _price_verified_from_source_log(asin):
    if not asin:
        return False
    text = _recent_log_text()
    pattern = rf"V5 REVIEW\s+{re.escape(asin)}\b.*?\bAPI\s+200\b"
    return bool(re.search(pattern, text, re.I))


def _history_evidence(url, current):
    current = _num(current)
    if not url or current <= 0:
        return {"verified": False, "reference": None, "count": 0, "discount": 0.0}

    sql = (
        "SELECT current_price, seen_at "
        "FROM market_observations "
        "WHERE lower(store)='amazon' AND url=? "
        "ORDER BY seen_at DESC "
        "LIMIT 30"
    )
    with _connect_ro() as con:
        rows = con.execute(sql, (url,)).fetchall()

    prices = [_num(r["current_price"]) for r in rows if _num(r["current_price"]) > 0]
    prior = [p for p in prices if abs(p - current) > max(0.01, current * 0.002)]

    if len(prior) < 2:
        return {"verified": False, "reference": None, "count": len(prior), "discount": 0.0}

    reference = float(statistics.median(prior))
    disc = _discount(current, reference)
    verified = bool(reference > current * 1.05 and len(prior) >= 2)

    return {
        "verified": verified,
        "reference": reference if verified else None,
        "count": len(prior),
        "discount": disc if verified else 0.0,
    }


def _make_deal(payload, history, price_verified):
    current = _num(payload.get("current_price"))
    old = _num(payload.get("old_price"), 0.0) or None

    values = {
        "store": "amazon",
        "title": str(payload.get("title") or "").strip(),
        "current_price": current,
        "old_price": old,
        "url": str(payload.get("url") or "").strip(),
        "image_url": payload.get("image_url") or None,
        "external_id": _asin(payload),
        "category": payload.get("category") or None,
        "reference_price": history.get("reference"),
        "reference_type": "history_median" if history.get("verified") else None,
        "reference_confidence": 0.95 if history.get("verified") else None,
        "live_rechecked": True if price_verified else None,
        "live_recheck_price": current if price_verified else None,
    }

    params = inspect.signature(Deal).parameters
    kwargs = {k: v for k, v in values.items() if k in params}
    return Deal(**kwargs)


def _signal(payload, history):
    visible = _num(payload.get("discount_percent"))
    if visible <= 0:
        visible = _discount(payload.get("current_price"), payload.get("old_price"))

    effective = history.get("discount") if history.get("verified") else visible

    return {
        "effective_discount_percent": effective,
        "history_verified": bool(history.get("verified")),
        "market_verified": False,
        "reference_price": history.get("reference"),
        "historical_typical_price": history.get("reference"),
        "reference_source": "source_db_history" if history.get("verified") else "amazon_source_review",
    }


def _truth(payload, history, price_verified):
    visible = _num(payload.get("discount_percent"))
    hist_disc = _num(history.get("discount"))

    if history.get("verified") and hist_disc >= 80:
        return {
            "class": "SUPER_ULTRA_HISTORY",
            "route": "PRIVATE_URGENT",
            "score": 99,
            "reason": "Amazon bridge + independently verified historical reference",
        }

    if history.get("verified") and hist_disc >= 60:
        return {
            "class": "ULTRA_HISTORY",
            "route": "PRIVATE_URGENT",
            "score": 96,
            "reason": "Amazon bridge + independently verified historical reference",
        }

    if history.get("verified") and hist_disc >= 40:
        return {
            "class": "FAST_40_HISTORY",
            "route": "PRIVATE_PRIORITY",
            "score": 92,
            "reason": "Amazon bridge + independently verified historical reference",
        }

    if visible >= 60:
        return {
            "class": "AMAZON_HIGH_VISIBLE_REVIEW",
            "route": "PRIVATE_REVIEW",
            "score": 84,
            "reason": "large visible Amazon discount; independent reference not proven",
        }

    if visible >= 40:
        return {
            "class": "AMAZON_FAST_REVIEW",
            "route": "PRIVATE_REVIEW",
            "score": 80,
            "reason": "strong visible Amazon discount; independent reference not proven",
        }

    return {
        "class": "AMAZON_SOURCE_REVIEW",
        "route": "PRIVATE_REVIEW",
        "score": 70 if price_verified else 60,
        "reason": "candidate emitted by live legacy Amazon worker",
    }


async def _process_row(row, engine, db):
    try:
        payload = json.loads(row.get("payload") or "{}")
    except Exception:
        return "skip_invalid"

    if not _is_amazon(payload):
        return "skip_non_amazon"

    current = _num(payload.get("current_price"))
    title = str(payload.get("title") or "").strip()
    url = str(payload.get("url") or "").strip()

    if current <= 0 or not title or "amazon.eg" not in url.lower():
        return "skip_invalid_amazon"

    asin = _asin(payload)
    price_verified = _price_verified_from_source_log(asin)
    history = _history_evidence(url, current)
    deal = _make_deal(payload, history, price_verified)

    fp = engine.save_seen(deal)

    if not engine.should_review(fp, deal):
        return "deduped"

    signal = _signal(payload, history)
    truth = _truth(payload, history, price_verified)

    await engine._send_direct_flash_review(deal, fp, signal, truth)

    log.warning(
        "AMAZON BRIDGE SENT | ASIN=%s | visible=%.1f%% | history_verified=%s | route=%s | image=%s",
        asin or "-",
        _num(payload.get("discount_percent")),
        bool(history.get("verified")),
        truth.get("route"),
        bool(payload.get("image_url")),
    )
    return "sent"


async def amazon_bridge_loop(engine, db, settings):
    if not SOURCE_DB.exists():
        log.error("Amazon bridge disabled: source DB missing: %s", SOURCE_DB)
        return

    state = _read_state()

    if not state.get("updated_at"):
        ts, fp = _source_cursor_now()
        _write_state(ts, fp)
        state = {"updated_at": ts, "fingerprint": fp}
        preview = _latest_amazon_preview()
        log.warning(
            "AMAZON READ-ONLY BRIDGE BASELINED | source=%s | preview=%s | AmazonRequests=0",
            SOURCE_DB,
            preview,
        )

    while True:
        try:
            rows = _new_rows(state)
            sent = 0

            for row in rows:
                row_ts = str(row.get("updated_at") or "")
                row_fp = str(row.get("fingerprint") or "")

                try:
                    payload = json.loads(row.get("payload") or "{}")
                except Exception:
                    payload = {}

                if _is_amazon(payload) and sent >= MAX_SEND_PER_POLL:
                    break

                try:
                    result = await _process_row(row, engine, db)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "Amazon bridge row failed; cursor held | source_fp=%s",
                        row_fp[:16],
                    )
                    break

                _write_state(row_ts, row_fp)
                state = {"updated_at": row_ts, "fingerprint": row_fp}

                if result == "sent":
                    sent += 1
                    await asyncio.sleep(0.8)

            if rows:
                log.info(
                    "Amazon bridge poll | rows=%d | sent=%d | source_pid_independent=True",
                    len(rows),
                    sent,
                )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Amazon bridge poll error; V11 scanner/moderation stay alive")

        await asyncio.sleep(POLL_SECONDS)


def offline_self_test():
    sample = {
        "store": "amazon",
        "title": "Test Amazon Product",
        "current_price": 300,
        "old_price": 1000,
        "discount_percent": 70,
        "url": "https://www.amazon.eg/dp/B012345678",
        "image_url": "https://example.invalid/a.jpg",
        "external_id": "B012345678",
        "category": "test",
    }
    assert _is_amazon(sample)
    assert _asin(sample) == "B012345678"
    assert round(_discount(300, 1000), 1) == 70.0

    t = _truth(
        sample,
        {"verified": False, "reference": None, "discount": 0},
        True,
    )
    assert t["route"] == "PRIVATE_REVIEW"

    t = _truth(
        sample,
        {"verified": True, "reference": 1000, "discount": 70},
        True,
    )
    assert t["class"] == "ULTRA_HISTORY"
    assert t["route"] == "PRIVATE_URGENT"

    return True


if __name__ == "__main__":
    offline_self_test()
    print("PASS amazon_bridge_v11 offline self-test")
