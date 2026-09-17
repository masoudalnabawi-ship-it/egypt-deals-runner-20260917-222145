from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

PRIVATE_ROUTES = {"PRIVATE_URGENT", "PRIVATE_PRIORITY", "PRIVATE_REVIEW"}
FAST_CANDIDATE_ROUTES = PRIVATE_ROUTES | {"RECHECK_REQUIRED"}

def _num(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default

def clean_url(url):
    u = str(url or "").strip()
    if not u:
        return ""
    try:
        p = urlsplit(u)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))
    except Exception:
        return u.split("?")[0].split("#")[0].rstrip("/")

def deal_identity(deal, product_key_fn=None):
    url = clean_url(getattr(deal, "url", ""))
    if url:
        return ("url", url)
    if product_key_fn is not None:
        try:
            return ("product_key", product_key_fn(deal))
        except Exception:
            pass
    return (
        "fallback",
        str(getattr(deal, "store", "") or "").lower(),
        str(getattr(deal, "title", "") or "").strip().lower(),
        round(_num(getattr(deal, "current_price", 0)), 2),
    )

def dedupe_deals(deals, product_key_fn=None):
    chosen = {}
    for deal in list(deals or []):
        key = deal_identity(deal, product_key_fn)
        old = chosen.get(key)
        if old is None:
            chosen[key] = deal
            continue
        old_rank = (_num(getattr(old, "discount_percent", 0)), 1 if getattr(old, "image_url", None) else 0)
        new_rank = (_num(getattr(deal, "discount_percent", 0)), 1 if getattr(deal, "image_url", None) else 0)
        if new_rank > old_rank:
            chosen[key] = deal
    return list(chosen.values())

def effective_discount(deal, signal):
    signal = signal or {}
    return max(
        _num(getattr(deal, "discount_percent", 0)),
        _num(signal.get("effective_discount_percent"), 0),
        _num(signal.get("historical_drop_percent"), 0),
        _num(signal.get("market_advantage_percent"), 0),
    )

def classify_priority_truth(deal, signal, live_status=None):
    signal = signal or {}
    current = _num(getattr(deal, "current_price", 0), 0)
    direct = _num(getattr(deal, "discount_percent", 0), 0)
    effective = effective_discount(deal, signal)
    history_verified = bool(signal.get("history_verified"))
    market_verified = bool(signal.get("market_verified"))
    history_gap = _num(signal.get("historical_drop_percent"), 0)
    market_gap = _num(signal.get("market_advantage_percent"), 0)
    prev_change = _num(signal.get("previous_change_percent"), 0)
    previous_drop = -prev_change if prev_change < 0 else 0
    extreme_gap = max(history_gap, market_gap, previous_drop)

    low_value = 0 < current < 200
    market_evidence_ok = market_verified and (not low_value or history_verified or market_gap >= 65.0)
    independent_strong = history_verified or market_evidence_ok

    if current > 0 and independent_strong and extreme_gap >= 65.0:
        return {"class":"PRICE_ERROR","route":"PRIVATE_URGENT","score":100,"reason":f"independent anomaly {extreme_gap:.1f}%"}
    if independent_strong and effective >= 60.0:
        return {"class":"SUPER_ULTRA","route":"PRIVATE_URGENT","score":95,"reason":f"independent evidence + {effective:.1f}% effective drop"}
    if independent_strong and effective >= 40.0:
        return {"class":"ULTRA","route":"PRIVATE_PRIORITY","score":85,"reason":f"independent evidence + {effective:.1f}% effective drop"}
    if direct >= 40.0 and live_status is None:
        return {"class":"RECHECK_REQUIRED","route":"RECHECK_REQUIRED","score":80 if direct >= 60.0 else 74,"reason":"extreme store claim needs current-price recheck"}
    if live_status is True and direct >= 60.0:
        return {"class":"EXTREME_REVIEW","route":"PRIVATE_REVIEW","score":78,"reason":"current price live-confirmed; reference price unverified"}
    if live_status is True and direct >= 40.0:
        return {"class":"ULTRA_REVIEW","route":"PRIVATE_REVIEW","score":72,"reason":"current price live-confirmed; reference price unverified"}
    if effective >= 25.0:
        return {"class":"STRONG","route":"GENERAL_PRIORITY","score":55,"reason":f"{effective:.1f}% drop without private-grade proof"}
    if effective >= 5.0:
        return {"class":"NORMAL","route":"GENERAL","score":30,"reason":f"{effective:.1f}% eligible discount"}
    return {"class":"IGNORE","route":"NONE","score":0,"reason":"below 5%"}

def is_fast_candidate(truth):
    return str((truth or {}).get("route")) in FAST_CANDIDATE_ROUTES

def is_private_route(truth):
    return str((truth or {}).get("route")) in PRIVATE_ROUTES

def cloud_priority(truth):
    route = str((truth or {}).get("route") or "")
    if route == "PRIVATE_URGENT":
        return "super_ultra"
    if route in ("PRIVATE_PRIORITY", "PRIVATE_REVIEW"):
        return "ultra"
    return "normal"
