from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

# Conservative sanity floors: investigation triggers, NOT claimed market prices.
FAMILIES = (
    ("electric_treadmill",
     ("treadmill", "walking pad", "electric treadmill", "مشاية كهربائية", "مشاية كهربية", "جهاز مشي"),
     2200.0,
     ("mat", "cover", "belt", "lubricant", "oil", "دواسة", "سجادة", "غطاء", "زيت")),
    ("refrigerator",
     ("refrigerator", "fridge", "ثلاجة", "ديب فريزر", "deep freezer"),
     3500.0,
     ("shelf", "drawer", "filter", "handle", "رف", "درج", "فلتر", "مقبض")),
    ("washing_machine",
     ("washing machine", "washer", "غسالة ملابس", "غساله ملابس", "غسالة اتوماتيك", "غسالة أوتوماتيك"),
     3500.0,
     ("cover", "hose", "filter", "stand", "غطاء", "خرطوم", "فلتر", "قاعدة")),
    ("air_conditioner",
     ("air conditioner", "split ac", "تكييف", "مكيف"),
     5000.0,
     ("remote", "ريموت", "cover", "غطاء", "filter", "فلتر", "bracket", "حامل")),
    ("laptop",
     ("laptop", "notebook", "macbook", "لابتوب", "لاب توب"),
     4500.0,
     ("bag", "case", "charger", "adapter", "stand", "keyboard cover", "شنطة", "جراب", "شاحن", "ادابتر", "حامل")),
    ("television",
     ("smart tv", "qled", "oled tv", "led tv", "television", "تلفزيون", "شاشة سمارت"),
     2500.0,
     ("remote", "ريموت", "mount", "bracket", "حامل", "cover", "غطاء")),
    ("smartphone",
     ("iphone", "galaxy s", "galaxy a", "redmi note", "poco ", "smartphone", "هاتف", "موبايل", "تليفون"),
     1800.0,
     ("case", "cover", "screen protector", "charger", "cable", "جراب", "اسكرينة", "شاحن", "كابل", "وصلة", "ستاند", "حامل")),
    ("gaming_console",
     ("playstation 5", "ps5", "xbox series", "nintendo switch", "بلايستيشن 5", "بلاي ستيشن 5"),
     4500.0,
     ("controller", "يد تحكم", "game", "لعبة", "cover", "جراب", "stand", "حامل")),
    ("exercise_bike",
     ("exercise bike", "spinning bike", "دراجة رياضية", "عجلة رياضية"),
     1800.0,
     ("mat", "cover", "seat", "pedal", "سجادة", "غطاء", "مقعد", "بدال")),
    ("microwave",
     ("microwave", "ميكروويف"),
     1600.0,
     ("cover", "plate", "dish", "rack", "غطاء", "طبق", "رف")),
    ("dishwasher",
     ("dishwasher", "غسالة اطباق", "غسالة أطباق"),
     4500.0,
     ("tablet", "detergent", "salt", "rinse", "منظف", "ملح", "قرص")),
    ("water_heater",
     ("water heater", "electric heater", "سخان كهربائي", "سخان غاز"),
     1300.0,
     ("valve", "hose", "خرطوم", "محبس", "شمعه", "شمعة")),
    ("vacuum_cleaner",
     ("vacuum cleaner", "robot vacuum", "مكنسة كهربائية", "مكنسه كهربائيه"),
     1100.0,
     ("bag", "filter", "brush", "hose", "كيس", "فلتر", "فرشة", "خرطوم")),
)

GLOBAL_ACCESSORY_WORDS = (
    "replacement", "spare part", "accessory", "accessories",
    "قطعة غيار", "قطع غيار", "اكسسوار", "إكسسوار",
    "sticker", "ستيكر", "skin", "cover only", "case only",
)

LOW_COST_HINTS = (
    "bath mat", "bathroom mat", "door mat", "floor mat",
    "مشاية حمام", "دواسة حمام", "دعاسة", "مفرش",
    "usb cable", "hdmi cable", "كابل", "وصلة",
    "phone case", "جراب موبايل", "screen protector", "اسكرينة",
)

# Semantic Identity V2: distinguish the actual product from accessories/usages.
# Example: "mouse for laptop" is a mouse, not a laptop.
FAMILY_IDENTITY_REJECTORS = {
    "laptop": (
        "mouse", "ماوس", "فأرة", "فاره", "keyboard", "كيبورد",
        "charger", "شاحن", "adapter", "ادابتر", "stand", "حامل",
        "bag", "شنطة", "case", "جراب", "sleeve", "hub", "dock",
        "cooling pad", "قاعدة تبريد", "webcam", "كاميرا ويب",
        "stylus", "digital pen", "قلم ستايلس", "قلم",
        "for laptop", "للابتوب", "للاب توب",
    ),
    "washing_machine": (
        "pressure washer", "high pressure", "car washer",
        "غسالة سيارة", "غسيل سيارة", "مسدس رش", "ضغط", "بار",
        "portable washer", "غسالة محمولة",
    ),
    "television": (
        "remote", "ريموت", "mount", "bracket", "حامل شاشة",
        "tv stand", "ستاند شاشة",
    ),
    "smartphone": (
        "case", "جراب", "cover", "screen protector", "اسكرينة",
        "charger", "شاحن", "cable", "كابل", "holder", "حامل",
        "lens", "عدسة", "stylus", "digital pen", "touch pen",
        "قلم ستايلس", "قلم لمس", "قلم", "pencil",
        "earbuds", "headphones", "headset", "سماعة", "سماعات",
        "mouse", "ماوس", "فأرة", "keyboard", "كيبورد",
        "power bank", "باور بانك", "tripod", "ترايبود",
        "for smartphone", "for phone", "for mobile",
        "لهاتف", "للهاتف", "للموبايل", "للتليفون",
        "لحاسوب لوحي", "لتابلت", "للتابلت",
    ),
}

FAMILY_STRONG_EVIDENCE = {
    "laptop": (
        "intel", "ryzen", "core i", "celeron", "processor", "معالج",
        " ram", "رام", "ssd", "hdd", "rtx", "gtx", "windows",
    ),
    "washing_machine": (
        "washing machine", "غسالة ملابس", "غساله ملابس",
        "اتوماتيك", "أوتوماتيك", "تحميل أمامي", "تحميل علوي",
        "front load", "top load", " kg", " كجم",
    ),
    "television": (
        "smart tv", "qled", "oled", "4k", "uhd", " inch", " بوصة",
        "تلفزيون", "شاشة سمارت",
    ),
    "smartphone": (
        "smartphone", "5g", "4g", "128gb", "256gb", "512gb",
        "64gb", "رام", "camera", "كاميرا",
    ),
}

def _family_identity_ok(family, text):
    rejectors = FAMILY_IDENTITY_REJECTORS.get(family, ())
    if not any(x in text for x in rejectors):
        return True

    strong = FAMILY_STRONG_EVIDENCE.get(family, ())
    if strong and any(x in text for x in strong):
        return True

    return False


# In-process cache: prevents rescanning thousands of unchanged rows when the
# existing engine calls Fast Lane repeatedly with an accumulated market list.
_SEEN_SIGNATURES = {}
CACHE_TTL_SECONDS = 1800.0

def _num(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default

def _text(deal):
    fields = (
        getattr(deal, "title", ""),
        getattr(deal, "category", ""),
        getattr(deal, "description", ""),
        getattr(deal, "brand", ""),
        getattr(deal, "model", ""),
        getattr(deal, "variant", ""),
    )
    return " | ".join(str(x or "").lower() for x in fields)

def _clean_url(url):
    u = str(url or "").strip()
    if not u:
        return ""
    try:
        p = urlsplit(u)
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))
    except Exception:
        return u.split("?")[0].split("#")[0].rstrip("/")

def _deal_key(deal):
    url = _clean_url(getattr(deal, "url", ""))
    if url:
        return ("url", url)
    return (
        "fallback",
        str(getattr(deal, "store", "") or "").lower(),
        str(getattr(deal, "title", "") or "").strip().lower(),
    )

def _signature(deal):
    return (
        round(_num(getattr(deal, "current_price", 0)), 2),
        round(_num(getattr(deal, "old_price", 0)), 2),
        round(_num(getattr(deal, "discount_percent", 0)), 2),
        bool(getattr(deal, "in_stock", True)),
    )

def select_new_or_changed(deals):
    """
    Return only products that are new, changed price/discount/stock signature,
    or whose cache entry expired. This makes the function compatible with the
    current engine call shape without patching scan_once().
    """
    now = time.monotonic()
    out = []

    for deal in list(deals or []):
        key = _deal_key(deal)
        sig = _signature(deal)
        prev = _SEEN_SIGNATURES.get(key)

        changed = (
            prev is None
            or prev["signature"] != sig
            or (now - prev["seen_at"]) >= CACHE_TTL_SECONDS
        )

        _SEEN_SIGNATURES[key] = {
            "signature": sig,
            "seen_at": now,
        }

        if changed:
            out.append(deal)

    # Bound memory.
    if len(_SEEN_SIGNATURES) > 100000:
        cutoff = now - CACHE_TTL_SECONDS
        stale = [k for k, v in _SEEN_SIGNATURES.items() if v["seen_at"] < cutoff]
        for k in stale[:50000]:
            _SEEN_SIGNATURES.pop(k, None)

    return out

def semantic_price_sanity(deal):
    text = _text(deal)
    price = _num(getattr(deal, "current_price", 0), 0)
    out = {
        "candidate": False,
        "family": None,
        "score": 0,
        "floor_egp": None,
        "price_floor_ratio": None,
        "reason": None,
        "identity_guard": False,
    }

    if price <= 0:
        return out

    if any(x in text for x in LOW_COST_HINTS):
        out["reason"] = "explicit low-cost/accessory category"
        return out

    if any(x in text for x in GLOBAL_ACCESSORY_WORDS):
        out["reason"] = "accessory/spare-part wording"
        return out

    for family, words, floor, blockers in FAMILIES:
        if not any(x in text for x in words):
            continue

        out["family"] = family
        out["floor_egp"] = floor

        if not _family_identity_ok(family, text):
            out["reason"] = f"{family} keyword appears in accessory/usage context"
            return out

        if any(x in text for x in blockers):
            out["reason"] = f"{family} keyword found but accessory blocker matched"
            return out

        ratio = price / floor if floor > 0 else 1.0
        out["price_floor_ratio"] = ratio
        out["identity_guard"] = True

        if ratio <= 0.10:
            out["candidate"] = True
            out["score"] = 99
            out["reason"] = f"{family} at {ratio*100:.1f}% of conservative sanity floor"
        elif ratio <= 0.20:
            out["candidate"] = True
            out["score"] = 92
            out["reason"] = f"{family} at {ratio*100:.1f}% of conservative sanity floor"
        elif ratio <= 0.35:
            out["candidate"] = True
            out["score"] = 82
            out["reason"] = f"{family} unusually below conservative sanity floor"
        else:
            out["reason"] = f"{family} price not extreme enough for cold-start lane"

        return out

    return out

def _effective_discount(deal, signal):
    signal = signal or {}
    return max(
        _num(getattr(deal, "discount_percent", 0), 0),
        _num(signal.get("effective_discount_percent"), 0),
        _num(signal.get("historical_drop_percent"), 0),
        _num(signal.get("market_advantage_percent"), 0),
    )

def is_instant_candidate(deal, signal=None, truth=None):
    signal = signal or {}
    truth = truth or {}
    effective = _effective_discount(deal, signal)
    semantic = semantic_price_sanity(deal)

    if effective >= 40.0:
        return {
            "candidate": True,
            "kind": "discount_40_plus",
            "score": max(80, min(100, int(effective))),
            "effective_discount": effective,
            "semantic": semantic,
        }

    if semantic["candidate"] and semantic["score"] >= 82:
        return {
            "candidate": True,
            "kind": "cold_start_semantic",
            "score": semantic["score"],
            "effective_discount": effective,
            "semantic": semantic,
        }

    route = str(truth.get("route") or "")
    if route in {"PRIVATE_URGENT", "PRIVATE_PRIORITY", "PRIVATE_REVIEW", "RECHECK_REQUIRED"}:
        return {
            "candidate": True,
            "kind": "priority_truth",
            "score": int(truth.get("score") or 75),
            "effective_discount": effective,
            "semantic": semantic,
        }

    return {
        "candidate": False,
        "kind": None,
        "score": 0,
        "effective_discount": effective,
        "semantic": semantic,
    }

def cold_start_truth(deal, semantic, live_status):
    semantic = semantic or {}

    if not semantic.get("candidate") or not semantic.get("identity_guard"):
        return None

    if live_status is not True:
        return {
            "class": "COLD_START_RECHECK",
            "route": "RECHECK_REQUIRED",
            "score": int(semantic.get("score") or 80),
            "reason": semantic.get("reason") or "semantic anomaly needs live recheck",
        }

    score = int(semantic.get("score") or 0)

    if score >= 92:
        return {
            "class": "COLD_START_ANOMALY",
            "route": "PRIVATE_URGENT",
            "score": score,
            "reason": "severe semantic anomaly + live-confirmed current price; reference enrichment pending",
        }

    if score >= 82:
        return {
            "class": "COLD_START_REVIEW",
            "route": "PRIVATE_REVIEW",
            "score": score,
            "reason": "semantic anomaly + live-confirmed current price; reference enrichment pending",
        }

    return None

_LATENCY_STATE = {}

def _state_key(deal):
    return _deal_key(deal)

def now_ms():
    return int(time.time() * 1000)

def ensure_first_seen(deal):
    key = _state_key(deal)
    state = _LATENCY_STATE.setdefault(key, {})
    value = state.get("first_seen_ms")
    if not value:
        value = now_ms()
        state["first_seen_ms"] = value
    return int(value)

def mark_stage(deal, stage):
    key = _state_key(deal)
    state = _LATENCY_STATE.setdefault(key, {})
    ensure_first_seen(deal)
    value = now_ms()
    state[f"{stage}_ms"] = value
    return value

def latency_snapshot(deal):
    key = _state_key(deal)
    state = _LATENCY_STATE.setdefault(key, {})
    first = ensure_first_seen(deal)

    def delta(stage):
        value = state.get(f"{stage}_ms")
        if not value:
            return None
        return max(0, int(value) - first)

    return {
        "first_seen_ms": first,
        "candidate_ms": delta("candidate"),
        "recheck_start_ms": delta("recheck_start"),
        "recheck_done_ms": delta("recheck_done"),
        "private_ready_ms": delta("private_ready"),
        "sent_ms": delta("sent"),
    }

def clear_runtime_state():
    _SEEN_SIGNATURES.clear()
    _LATENCY_STATE.clear()
