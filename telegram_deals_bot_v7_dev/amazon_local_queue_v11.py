from __future__ import annotations
import asyncio
import inspect
import json
import logging
import os
from pathlib import Path

import engine
from models import Deal

log = logging.getLogger("amazon-local-queue-v11")
INBOX = Path(os.getenv("AMAZON_REVIEW_INBOX") or str(Path.home() / "telegram_deals_bot_v7_dev" / "amazon_review_inbox.jsonl"))
STATE = Path(__file__).with_name(".amazon_local_queue_state.json")

def _num(v, default=0.0):
    try:
        return float(v if v is not None else default)
    except (TypeError, ValueError):
        return default

def _discount(cur, ref):
    cur, ref = _num(cur), _num(ref)
    return ((ref-cur)/ref*100.0) if ref > cur > 0 else 0.0

def _load_offset():
    try:
        return max(0, int(json.loads(STATE.read_text(encoding="utf-8")).get("offset", 0)))
    except Exception:
        return 0

def _save_offset(n):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": int(n)}), encoding="utf-8")
    tmp.replace(STATE)

def _make_deal(p):
    vals = {
        "store": "amazon",
        "title": str(p.get("title_ar") or p.get("title") or "").strip(),
        "current_price": _num(p.get("current_price")),
        "old_price": _num(p.get("old_price")) or None,
        "url": str(p.get("url") or "").strip(),
        "image_url": p.get("image_url") or None,
        "external_id": str(p.get("asin") or "").strip() or None,
        "category": p.get("category") or None,
        "live_rechecked": bool(p.get("live_rechecked")),
        "reference_price": _num(p.get("reference_price")) or None,
    }
    params = inspect.signature(Deal).parameters
    return Deal(**{k:v for k,v in vals.items() if k in params})

def _evidence(p, deal):
    cur = _num(deal.current_price)
    ref = _num(p.get("market_reference")) or _num(p.get("reference_price"))
    independent = bool(
        ref > cur and (
            p.get("market_stores")
            or p.get("evidence")
            or p.get("intelligence_tier")
        )
    )
    visible = _num(p.get("discount_percent")) or _discount(cur, _num(p.get("old_price")))
    effective = _discount(cur, ref) if independent else visible
    signal = {
        "effective_discount_percent": effective,
        "history_verified": bool(independent and p.get("evidence")),
        "market_verified": bool(independent and (p.get("market_reference") or p.get("market_stores"))),
        "reference_price": ref if independent else None,
        "market_reference_price": ref if independent else None,
    }
    return signal, effective, independent

def _truth(p, effective, independent):
    if independent and effective >= 80:
        return {"class":"SUPER_ULTRA_AMAZON","route":"PRIVATE_URGENT","score":99,"reason":"Amazon live-reviewed with independent reference"}
    if independent and effective >= 60:
        return {"class":"ULTRA_AMAZON","route":"PRIVATE_URGENT","score":96,"reason":"Amazon live-reviewed with independent reference"}
    if independent and effective >= 40:
        return {"class":"FAST_AMAZON_40","route":"PRIVATE_PRIORITY","score":92,"reason":"Amazon live-reviewed with independent reference"}
    return {"class":"AMAZON_LIVE_REVIEW","route":"PRIVATE_REVIEW","score":85 if _num(p.get("deal_score")) >= 40 else 75,"reason":"Amazon radar approved review; independent reference not proven"}

async def _handle(p):
    if "amazon" not in str(p.get("store") or "amazon").lower() and "amazon.eg" not in str(p.get("url") or "").lower():
        return "skip"
    deal = _make_deal(p)
    if not deal.title or _num(deal.current_price) <= 0 or "amazon.eg" not in str(deal.url).lower():
        return "skip"
    fp = engine.save_seen(deal)
    if not engine.should_review(fp, deal):
        return "deduped"
    signal, effective, independent = _evidence(p, deal)
    truth = _truth(p, effective, independent)
    await engine._send_direct_flash_review(deal, fp, signal, truth)
    log.warning(
        "AMAZON REVIEW SENT | asin=%s | route=%s | discount=%.1f | image=%s",
        getattr(deal, "external_id", None), truth["route"], effective,
        bool(getattr(deal, "image_url", None)),
    )
    return "sent"

async def amazon_local_queue_loop():
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    INBOX.touch(exist_ok=True)
    offset = _load_offset()
    if offset > INBOX.stat().st_size:
        offset = 0
        _save_offset(0)
    log.warning("AMAZON LOCAL INBOX ON | path=%s | AmazonRequests=0", INBOX)
    while True:
        try:
            if INBOX.stat().st_size > offset:
                with INBOX.open("r", encoding="utf-8") as f:
                    f.seek(offset)
                    while True:
                        pos = f.tell()
                        line = f.readline()
                        if not line:
                            break
                        nxt = f.tell()
                        try:
                            payload = json.loads(line)
                            await _handle(payload)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception("Amazon inbox row failed; cursor held")
                            f.seek(pos)
                            break
                        offset = nxt
                        _save_offset(offset)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Amazon local inbox loop error")
            await asyncio.sleep(5)

if __name__ == "__main__":
    p = {"store":"Amazon Egypt","title":"x","current_price":300,"reference_price":1000,"market_stores":["x"],"discount_percent":70,"url":"https://www.amazon.eg/dp/B012345678","asin":"B012345678","live_rechecked":True}
    d = _make_deal(p)
    _, eff, independent = _evidence(p, d)
    t = _truth(p, eff, independent)
    assert independent and 69 < eff < 71 and t["route"] == "PRIVATE_URGENT"
    print("PASS amazon_local_queue_v11 self-test")
