from __future__ import annotations

import hashlib
import html
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("AMAZON_REVIEW_CONFIG", str(ROOT / "config.json")))
if not CFG_PATH.exists():
    raise RuntimeError(
        f"Missing review config: {CFG_PATH}. Copy config.example.json to config.json "
        "or run the bundle installer so your existing private config is reused."
    )
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
TOKEN = str(CFG["token"]).strip()
REVIEW = int(CFG["review_group_id"])

# NORMAL_REVIEW_ROUTING_V93
NORMAL_REVIEW = int(CFG.get("normal_review_chat_id") or REVIEW)

def review_chat_for(p, c):
    try:
        rank = int((c or {}).get("rank") or 0)
    except Exception:
        rank = 0

    label = str((c or {}).get("label") or "").upper()

    public_50 = bool(
        p.get("general_50_plus")
        or p.get("public_coupon_50_plus")
    )

    is_ultra = (
        public_50
        or rank >= 3
        or "ULTRA" in label
    )

    return REVIEW if is_ultra else NORMAL_REVIEW


def review_chat_for_deal(did, p, c):
    """
    Preserve the original review chat for existing deals.
    New deals use the new Normal/Ultra routing.
    """
    try:
        with sqlite3.connect(DB) as db:
            row = db.execute(
                "SELECT payload FROM deals WHERE deal_id=?",
                (did,),
            ).fetchone()

        if row and row[0]:
            old = json.loads(row[0])
            saved = old.get("_review_chat_id")

            if saved:
                return int(saved)

            # Legacy deal created before split-routing:
            # its old message belongs to the Ultra review group.
            return REVIEW

    except Exception:
        pass

    return review_chat_for(p, c)

CHANNEL = int(CFG["channel_id"])
INBOX = Path(CFG.get("inbox") or (ROOT / "inbox.jsonl"))
if not INBOX.is_absolute():
    INBOX = ROOT / INBOX
DB = ROOT / "amazon_bot.db"
STATE = ROOT / "poll_state.json"
QUEUE_STATE = ROOT / "queue_state.json"
LOG = ROOT / "amazon_bot.log"

API = f"https://api.telegram.org/bot{TOKEN}"

def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " | " + str(msg)
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

try:
    from visual_card import build_review_card, send_photo_file
except Exception as _visual_error:
    build_review_card = None
    send_photo_file = None

try:
    from amazon_page_capture import capture_amazon_page
except Exception as _capture_error:
    capture_amazon_page = None

def api(method, params=None, timeout=30):
    data = None
    if params is not None:
        fixed = {}
        for k, v in params.items():
            if isinstance(v, (dict, list)):
                fixed[k] = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                fixed[k] = "true" if v else "false"
            else:
                fixed[k] = str(v)
        data = urllib.parse.urlencode(fixed).encode()
    req = urllib.request.Request(API + "/" + method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise RuntimeError(f"{method} HTTP {e.code}: {body[:500]}")
    if not payload.get("ok"):
        raise RuntimeError(f"{method}: {payload}")
    return payload["result"]

def db_init():
    with sqlite3.connect(DB) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS deals(
                deal_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'review',
                review_message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.commit()

def num(v, d=0.0):
    try:
        return float(v if v is not None else d)
    except Exception:
        return d

def pct(cur, ref):
    cur, ref = num(cur), num(ref)
    if ref > cur > 0:
        return (ref - cur) / ref * 100.0
    return 0.0

def evidence_text(p):
    e = p.get("evidence")
    if isinstance(e, list):
        return " ".join(str(x) for x in e).lower()
    return str(e or "").lower()

def classify(p):
    base_cur = num(p.get("current_price"))
    promo_final = num(p.get("promo_final_price"))
    promo_verified = bool(p.get("promo_verified"))
    conditional = bool(p.get("conditional"))
    member_only = bool(p.get("member_only"))
    account_specific = bool(p.get("account_specific"))

    # Promo final price is counted only when it is explicitly verified
    # and does not require conditional/member/account eligibility.
    effective_cur = base_cur
    if (
        promo_verified
        and not conditional
        and not member_only
        and not account_specific
        and 0 < promo_final < base_cur
    ):
        effective_cur = promo_final

    market_ref = num(p.get("market_reference"))
    reference = num(p.get("reference_price"))
    stores = p.get("market_stores")
    ev = evidence_text(p)

    market_verified = market_ref > effective_cur and bool(stores)
    strong_history = (
        reference > effective_cur
        and any(k in ev for k in ("history", "historical", "anchor", "observed", "market"))
        and "advertised_old_price_only" not in ev
    )

    independent = bool(market_verified or strong_history)
    if market_verified:
        verified_ref = market_ref
    elif strong_history:
        verified_ref = reference
    else:
        verified_ref = 0.0

    verified_discount = pct(effective_cur, verified_ref) if independent else 0.0
    claimed = num(p.get("discount_percent"))
    score = num(p.get("deal_score"))
    priority = str(p.get("priority") or p.get("intelligence_tier") or "").lower()
    public_50_plus = bool(p.get("general_50_plus") or p.get("public_coupon_50_plus"))

    if public_50_plus:
        label = "🎟️🔥 ULTRA — كوبون/عرض عام 50%+"
        rank = 3
    elif independent and verified_discount >= 80:
        label = "🚨 SUPER ULTRA موثّق"
        rank = 4
    elif independent and verified_discount >= 60:
        label = "🔥🔥 TRUE ULTRA موثّق"
        rank = 3
    elif independent and verified_discount >= 40:
        label = "⚡ FAST LANE — خصم حقيقي موثّق"
        rank = 2
    elif claimed >= 40:
        label = "🔎 خصم كبير ظاهر — يحتاج إثبات مستقل"
        rank = 1
    else:
        label = "👀 مرشح Amazon مهم"
        rank = 0

    important = (
        public_50_plus
        or (independent and verified_discount >= 40)
        or claimed >= 40
        or (promo_verified and score >= 70)
        or score >= 80
        or any(x in priority for x in ("critical", "ultra", "hot"))
    )

    return {
        "important": important,
        "label": label,
        "rank": rank,
        "current": base_cur,
        "effective_current": effective_cur,
        "reference": verified_ref,
        "independent": independent,
        "verified_discount": verified_discount,
        "claimed_discount": claimed,
        "score": score,
        "market_verified": market_verified,
        "strong_history": strong_history,
        "promo_verified": promo_verified,
        "conditional": conditional,
        "member_only": member_only,
    }


# =========================================================
# AMAZON_SMART_DEDUPE_V1
# One Amazon product = one review card.
# Better price/promo/evidence updates the existing card.
# =========================================================
def _product_key(p):
    asin = str(p.get("asin") or "").strip().upper()
    if asin:
        return "asin:" + asin

    url = str(p.get("url") or "").strip()
    url = url.split("#", 1)[0].split("?", 1)[0]
    return "url:" + url


def deal_id(p):
    raw = _product_key(p)
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _effective_price(p):
    current = num(p.get("current_price"))
    promo = num(p.get("promo_final_price"))

    if promo > 0 and (current <= 0 or promo < current):
        return promo

    return current


def _promo_fields(p):
    tokens = (
        "promo",
        "coupon",
        "voucher",
        "bank",
        "task",
        "mission",
        "cart",
        "qty",
        "quantity",
        "wallet",
        "member",
        "membership",
        "account",
        "eligible",
    )

    out = {}

    for k, v in p.items():
        lk = str(k).lower()

        if not any(t in lk for t in tokens):
            continue

        if v in (None, "", False, 0, "0"):
            continue

        out[str(k)] = v

    return out


def _promo_signature(p):
    obj = {
        "current": round(num(p.get("current_price")), 2),
        "effective": round(_effective_price(p), 2),
        "promos": _promo_fields(p),
    }

    raw = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )

    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _promo_weight(p):
    return len(_promo_fields(p))


def _existing_product_row(db, p, preferred_did):
    row = db.execute(
        """SELECT deal_id,payload,status,review_message_id
           FROM deals WHERE deal_id=?""",
        (preferred_did,),
    ).fetchone()

    if row:
        return row

    target = _product_key(p)

    rows = db.execute(
        """SELECT deal_id,payload,status,review_message_id
           FROM deals
           ORDER BY updated_at DESC
           LIMIT 2000"""
    ).fetchall()

    for row in rows:
        try:
            old = json.loads(row[1] or "{}")
        except Exception:
            continue

        if _product_key(old) == target:
            return row

    return None


def _discount_value(c):
    vd = num(c.get("verified_discount"))
    cd = num(c.get("claimed_discount"))
    return vd if vd > 0 else cd


def _should_upgrade(old_p, new_p, old_c, new_c):
    old_eff = _effective_price(old_p)
    new_eff = _effective_price(new_p)

    # Meaningfully better effective price.
    if new_eff > 0 and (
        old_eff <= 0
        or new_eff < old_eff - max(1.0, old_eff * 0.002)
    ):
        return True

    # Unverified -> independently verified.
    if (
        not bool(old_c.get("independent"))
        and bool(new_c.get("independent"))
    ):
        return True

    # Stronger verified/claimed discount evidence.
    if _discount_value(new_c) >= _discount_value(old_c) + 1.0:
        return True

    # More usable stackable promo components without a worse final price.
    if (
        _promo_signature(old_p) != _promo_signature(new_p)
        and _promo_weight(new_p) > _promo_weight(old_p)
        and (
            new_eff <= 0
            or old_eff <= 0
            or new_eff <= old_eff + 0.01
        )
    ):
        return True

    return False


def money(v):
    return f"{num(v):,.2f} ج.م"

def review_text(p, c):
    title = html.escape(str(p.get("title_ar") or p.get("title") or "منتج Amazon"))
    asin = html.escape(str(p.get("asin") or "-"))
    lines = [
        c["label"],
        "",
        f"📦 <b>{title}</b>",
        f"💰 السعر الحالي: <b>{money(c['current'])}</b>",
    ]

    if c["effective_current"] != c["current"]:
        lines.append(f"🎟️ السعر النهائي المؤكد بعد العرض: <b>{money(c['effective_current'])}</b>")

    if c["independent"]:
        lines += [
            f"📊 السعر المرجعي الموثّق: <b>{money(c['reference'])}</b>",
            f"📉 الخصم الحقيقي المحسوب: <b>{c['verified_discount']:.1f}%</b>",
            "✅ التحقق: مرجع مستقل/تاريخ سعر موثوق",
        ]
    else:
        lines += [
            f"📉 الخصم الظاهر: <b>{c['claimed_discount']:.1f}%</b>",
            "⚠️ لم نعتبر السعر المشطوب وحده دليلًا على الخصم الحقيقي.",
        ]

    if c["score"]:
        lines.append(f"🎯 Deal Score: {c['score']:.0f}/100")

    if p.get("promo_verified"):
        promo = html.escape(str(p.get("promo_label") or p.get("promo_details") or "عرض إضافي مؤكد"))
        lines.append(f"🎟️ Promo: {promo}")
    if c["conditional"]:
        lines.append("⚠️ العرض الإضافي مشروط — لم يدخل في نسبة الخصم النهائي.")
    if c["member_only"]:
        lines.append("👤 العرض خاص بعضوية — لم ندخله تلقائيًا في السعر النهائي.")
    if p.get("account_specific"):
        lines.append("👤 العرض خاص بحسابات مؤهلة — مفصول عن السعر العام.")

    product_url = str(
        p.get("url")
        or (
            "https://www.amazon.eg/dp/" + asin
            if asin else
            "https://www.amazon.eg/"
        )
    )

    lines += [
        "",
        "🔗 <b>رابط المنتج:</b>",
        f"<code>{product_url}</code>",
        "",
        "🔒 <b>للمراجعة فقط — لن يُنشر تلقائيًا.</b>"
    ]
    return "\n".join(lines)

def channel_text(p, c, urgent=False):
    title = html.escape(str(p.get("title_ar") or p.get("title") or "عرض Amazon"))
    url = html.escape(str(p.get("url") or ""))
    head = "🚨 <b>عرض عاجل من Amazon</b>" if urgent else "🔥 <b>عرض Amazon</b>"
    lines = [
        head,
        "",
        f"📦 <b>{title}</b>",
        f"💰 السعر: <b>{money(c['effective_current'])}</b>",
    ]
    if c["independent"]:
        lines += [
            f"📊 المرجع: {money(c['reference'])}",
            f"📉 الخصم الحقيقي: <b>{c['verified_discount']:.1f}%</b>",
        ]
    elif c["claimed_discount"]:
        lines.append(f"📉 الخصم الظاهر: {c['claimed_discount']:.1f}%")

    promo_type = str(p.get("promo_type") or "none").lower()
    promo_percent = num(p.get("promo_percent"))
    coupon_value = num(p.get("coupon_value"))
    if bool(p.get("promo_verified")):
        if promo_type == "coupon" and promo_percent > 0:
            lines.append(f"🎟️ كوبون: <b>{promo_percent:.1f}%</b>")
        elif promo_type == "coupon" and coupon_value > 0:
            lines.append(f"🎟️ كوبون: <b>{money(coupon_value)}</b>")
        elif p.get("promo_label"):
            lines.append("🎁 " + html.escape(str(p.get("promo_label"))))

    for offer in (p.get("bulk_offers") or [])[:2]:
        pct_val = num(offer.get("promo_percent")) if isinstance(offer, dict) else 0.0
        qty_val = int(num(offer.get("minimum_quantity"))) if isinstance(offer, dict) else 0
        if pct_val > 0 and qty_val > 0:
            lines.append(f"📦 اشترِ {qty_val}+ ووفر {pct_val:.1f}%")

    lines += ["", f'🛒 <a href="{url}">فتح العرض على Amazon</a>']
    return "\n".join(lines)

def keyboard(p, did):
    return {
        "inline_keyboard": [
            [
                {"text": "🚀 نشر عاجل", "callback_data": "u:" + did},
                {"text": "📢 نشر عادي", "callback_data": "n:" + did},
            ],
            [
                {"text": "🔗 فتح المنتج", "url": str(p.get("url") or "https://www.amazon.eg/")},
                {"text": "❌ رفض", "callback_data": "x:" + did},
            ],
        ]
    }

def is_group_admin(user_id):
    try:
        m = api("getChatMember", {"chat_id": REVIEW, "user_id": user_id}, timeout=15)
        return m.get("status") in ("creator", "administrator")
    except Exception as e:
        log(f"admin check failed: {e}")
        return False


def _send_review_card(p, c, did):
    """
    AMAZON FAST LIVE REVIEW V1

    Strong review:
    Radar -> live Amazon page -> real screenshot ->
    price safety -> ONE Telegram review card.

    No CAPTCHA bypass.
    No auto publish.
    """
    _route_chat = review_chat_for_deal(did, p, c)
    p['_review_chat_id'] = _route_chat
    log('REVIEW ROUTING | ' + ('ULTRA_GROUP' if _route_chat == REVIEW else 'PRIVATE_NORMAL') + ' | asin=' + str(p.get('asin') or ''))
    url = str(p.get('url') or 'https://www.amazon.eg/')
    asin = str(p.get('asin') or did or 'product')
    radar_price = float(c.get('effective_current') or p.get('current_price') or p.get('last_price') or 0)
    if capture_amazon_page is not None:
        try:
            result = capture_amazon_page(url, asin)
        except Exception as e:
            log('LIVE CAPTURE FALLBACK: ' + str(e))
            result = {'ok': False, 'capture_error': True, 'reason': str(e)}
    else:
        result = {'ok': False, 'capture_error': True, 'reason': 'Playwright capture unavailable; using product-card fallback'}
    live_price = 0.0
    available = None
    screenshot = ''
    if result.get('ok'):
        live_price = float(result.get('live_price') or 0)
        available = result.get('availability')
        screenshot = str(result.get('screenshot') or '')
    mismatch = 0.0
    if live_price > 0:
        if radar_price > 0:
            mismatch = abs(live_price - radar_price) / radar_price
        p['current_price'] = live_price
        p['last_price'] = live_price
        p['live_price'] = live_price
        p['live_rechecked'] = True
        p['live_checked_at'] = int(time.time())
        c['effective_current'] = live_price
        reference = float(c.get('reference') or p.get('reference_price') or p.get('amazon_old_price') or p.get('old_price') or 0)
        if reference > live_price > 0:
            new_discount = (reference - live_price) / reference * 100
            if c.get('independent'):
                c['verified_discount'] = new_discount
            else:
                c['claimed_discount'] = new_discount
        elif reference > 0:
            c['verified_discount'] = 0
            c['claimed_discount'] = 0
    title = html.escape(str(p.get('title_ar') or p.get('title') or 'عرض Amazon')[:220])
    live_capture_safe = result.get('ok') and live_price > 0 and (available is not False) and (mismatch <= 0.15)
    radar_recheck_safe = bool(result.get('capture_error')) and bool(p.get('verified')) and bool(p.get('live_rechecked')) and (radar_price > 0)
    safe_publish = bool(live_capture_safe or radar_recheck_safe)
    if live_capture_safe:
        state_line = '✅ <b>تم التحقق حيًا من Amazon</b>'
    elif radar_recheck_safe:
        state_line = '✅ <b>تم التحقق مرتين بواسطة الرادار — صورة مراجعة بديلة</b>'
    elif available is False:
        state_line = '⛔ <b>المنتج غير متاح حاليًا</b>'
    elif live_price <= 0:
        state_line = '⚠️ <b>تعذر تأكيد السعر آليًا</b>'
    elif mismatch > 0.15:
        state_line = '⚠️ <b>السعر تغير عن رصد الرادار</b>'
    else:
        state_line = '⚠️ <b>يحتاج مراجعة يدوية</b>'
    lines = ['🛒 <b>Amazon Egypt</b>', '', f'📦 <b>{title}</b>', state_line]
    if live_price > 0:
        lines.append(f'💰 السعر الآن: <b>{live_price:,.2f} ج.م</b>')
    if radar_price > 0:
        lines.append(f'📡 سعر الرادار: <b>{radar_price:,.2f} ج.م</b>')
    promo_type = str(p.get('promo_type') or 'none').lower()
    promo_percent = num(p.get('promo_percent'))
    coupon_value = num(p.get('coupon_value'))
    if bool(p.get('promo_verified')):
        if promo_type == 'coupon' and promo_percent > 0:
            lines.append(f'🎟️ كوبون مؤكد: <b>{promo_percent:.1f}%</b>')
        elif promo_type == 'coupon' and coupon_value > 0:
            lines.append(f'🎟️ كوبون مؤكد: <b>{money(coupon_value)}</b>')
        elif p.get('promo_label'):
            lines.append('🎁 ' + html.escape(str(p.get('promo_label'))))
    for offer in (p.get('bulk_offers') or [])[:2]:
        pct_val = num(offer.get('promo_percent')) if isinstance(offer, dict) else 0.0
        qty_val = int(num(offer.get('minimum_quantity'))) if isinstance(offer, dict) else 0
        if pct_val > 0 and qty_val > 0:
            lines.append(f'📦 اشترِ {qty_val}+ ووفر {pct_val:.1f}% إضافية')
    if live_price > 0 and radar_price > 0 and (mismatch > 0.02):
        lines.append(f'🔄 فرق السعر: <b>{mismatch * 100:.1f}%</b>')
    lines += ['', '🔗 <b>رابط المنتج:</b>', f'<code>{html.escape(url)}</code>', '', '🔒 للمراجعة فقط — لن يُنشر تلقائيًا.']
    caption = '\n'.join(lines)
    if safe_publish:
        markup = keyboard(p, did)
    else:
        markup = {'inline_keyboard': [[{'text': '🔗 فتح المنتج', 'url': url}, {'text': '❌ رفض', 'callback_data': 'x:' + did}]]}
    if screenshot and os.path.exists(screenshot) and (send_photo_file is not None):
        try:
            p['_review_media_path'] = screenshot
            p['_review_media_kind'] = 'store_page_screenshot'
            return send_photo_file(API, _route_chat, screenshot, caption, markup, timeout=45)
        except Exception as e:
            log('REAL SCREENSHOT SEND FALLBACK: ' + str(e))
    try:
        import tempfile
        if build_review_card is None or send_photo_file is None:
            raise RuntimeError('visual card dependencies unavailable')
        fallback_dir = Path(os.getenv('AMAZON_REVIEW_MEDIA_DIR') or ROOT.parent / 'review_media')
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback = str(fallback_dir / ('amazon_fallback_' + ''.join((x for x in str(did) if x.isalnum() or x in '-_'))[:60] + '.jpg'))
        build_review_card(p, c, fallback)
        p['_review_media_path'] = fallback
        p['_review_media_kind'] = 'visual_fallback'
        return send_photo_file(API, _route_chat, fallback, caption, markup, timeout=45)
    except Exception as e:
        log('VISUAL FALLBACK FAILED: ' + str(e))
        return api('sendMessage', {'chat_id': _route_chat, 'text': caption, 'parse_mode': 'HTML', 'disable_web_page_preview': True, 'reply_markup': markup}, timeout=30)


def _edit_review_card(mid, p, c, did):
    """
    Supports both:
    - new photo review cards
    - old text-only review messages
    """
    _route_chat = review_chat_for_deal(did, p, c)
    p['_review_chat_id'] = _route_chat
    log('REVIEW ROUTING | ' + ('ULTRA_GROUP' if _route_chat == REVIEW else 'PRIVATE_NORMAL') + ' | asin=' + str(p.get('asin') or ''))
    image = str(p.get('image_url') or '').strip()
    text = review_text(p, c)
    if image and len(text) <= 1000:
        try:
            api('editMessageCaption', {'chat_id': _route_chat, 'message_id': int(mid), 'caption': text, 'parse_mode': 'HTML', 'reply_markup': keyboard(p, did)}, timeout=25)
            return int(mid)
        except Exception as e:
            log(f'caption edit fallback: {e}')
    try:
        api('editMessageText', {'chat_id': _route_chat, 'message_id': int(mid), 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True, 'reply_markup': keyboard(p, did)}, timeout=25)
        return int(mid)
    except Exception as e:
        log(f'text edit fallback: {e}')
    sent = _send_review_card(p, c, did)
    return int(sent['message_id'])


def send_review(p):
    c = classify(p)

    if not c["important"]:
        return "skipped"

    preferred_did = deal_id(p)

    with sqlite3.connect(DB) as db:
        existing = _existing_product_row(
            db,
            p,
            preferred_did,
        )

    # -------------------------------------------------
    # EXISTING PRODUCT — UPDATE SAME REVIEW WHEN POSSIBLE
    # -------------------------------------------------
    if existing:
        did, old_payload, old_status, mid = existing

        try:
            old_p = json.loads(old_payload or "{}")
        except Exception:
            old_p = {}

        old_c = classify(old_p)

        if mid:
            if not _should_upgrade(
                old_p,
                p,
                old_c,
                c,
            ):
                log(
                    f"DUPLICATE HELD did={did} "
                    f"asin={p.get('asin')} "
                    f"effective={_effective_price(p):.2f}"
                )
                return "duplicate"

            new_mid = _edit_review_card(
                mid,
                p,
                c,
                did,
            )

            with sqlite3.connect(DB) as db:
                db.execute(
                    """UPDATE deals
                       SET payload=?,
                           status='review',
                           review_message_id=?,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE deal_id=?""",
                    (
                        json.dumps(
                            p,
                            ensure_ascii=False
                        ),
                        new_mid,
                        did,
                    ),
                )
                db.commit()

            log(
                f"REVIEW UPDATED did={did} "
                f"asin={p.get('asin')} "
                f"effective={_effective_price(p):.2f} "
                f"verified={c['independent']}"
            )

            return "updated"

        preferred_did = did

    # -------------------------------------------------
    # NEW PRODUCT
    # -------------------------------------------------
    did = preferred_did

    sent = _send_review_card(
        p,
        c,
        did,
    )

    new_mid = int(sent["message_id"])

    with sqlite3.connect(DB) as db:
        db.execute(
            """INSERT INTO deals(
                   deal_id,
                   payload,
                   status,
                   review_message_id,
                   updated_at
               )
               VALUES(
                   ?,?,
                   'review',
                   ?,
                   CURRENT_TIMESTAMP
               )
               ON CONFLICT(deal_id) DO UPDATE SET
                   payload=excluded.payload,
                   status='review',
                   review_message_id=COALESCE(
                       deals.review_message_id,
                       excluded.review_message_id
                   ),
                   updated_at=CURRENT_TIMESTAMP""",
            (
                did,
                json.dumps(
                    p,
                    ensure_ascii=False
                ),
                new_mid,
            ),
        )
        db.commit()

    log(
        f"REVIEW SENT did={did} "
        f"asin={p.get('asin')} "
        f"verified={c['independent']} "
        f"discount="
        f"{c['verified_discount'] or c['claimed_discount']:.1f}"
    )

    return "sent"


def publish(did, urgent):
    with sqlite3.connect(DB) as db:
        row = db.execute(
            "SELECT payload,status,review_message_id FROM deals WHERE deal_id=?",
            (did,),
        ).fetchone()
    if not row:
        raise RuntimeError("deal not found")
    p = json.loads(row[0])
    status = row[1]
    mid = row[2]
    if status in ("posted", "rejected"):
        return status, mid

    c = classify(p)
    text = channel_text(p, c, urgent=urgent)
    image = str(p.get("image_url") or "").strip()
    media_path = str(p.get("_review_media_path") or "").strip()
    sent_photo = False

    # Publish the exact same screenshot/card that the admin reviewed.
    if media_path and os.path.exists(media_path) and send_photo_file is not None:
        try:
            send_photo_file(
                API, CHANNEL, media_path, text[:1000],
                {"inline_keyboard": []}, timeout=45,
            )
            sent_photo = True
        except Exception as e:
            log(f"channel review-media fallback: {e}")

    if not sent_photo and image and len(text) <= 1000:
        try:
            api(
                "sendPhoto",
                {
                    "chat_id": CHANNEL,
                    "photo": image,
                    "caption": text,
                    "parse_mode": "HTML",
                },
                timeout=30,
            )
            sent_photo = True
        except Exception as e:
            log(f"channel photo fallback: {e}")

    if not sent_photo:
        api(
            "sendMessage",
            {
                "chat_id": CHANNEL,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )

    with sqlite3.connect(DB) as db:
        db.execute(
            "UPDATE deals SET status='posted',updated_at=CURRENT_TIMESTAMP WHERE deal_id=?",
            (did,),
        )
        db.commit()
    return "posted", mid

def reject(did):
    with sqlite3.connect(DB) as db:
        row = db.execute("SELECT review_message_id,status FROM deals WHERE deal_id=?", (did,)).fetchone()
        if not row:
            return "missing", None
        if row[1] == "posted":
            return "posted", row[0]
        db.execute(
            "UPDATE deals SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE deal_id=?",
            (did,),
        )
        db.commit()
        return "rejected", row[0]


def clear_buttons(mid, chat_id=None):
    if not mid:
        return

    target_chat = int(chat_id or REVIEW)

    try:
        api(
            "editMessageReplyMarkup",
            {
                "chat_id": target_chat,
                "message_id": mid,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=15,
        )
    except Exception as e:
        log(f"clear buttons failed: {e}")


def answer_callback(qid, text, alert=False):
    try:
        api(
            "answerCallbackQuery",
            {"callback_query_id": qid, "text": text, "show_alert": alert},
            timeout=15,
        )
    except Exception as e:
        log(f"answer callback failed: {e}")


def handle_callback(q):
    qid = q.get("id")
    user_id = (q.get("from") or {}).get("id")
    msg = q.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    data = str(q.get("data") or "")

    private_ok = (
        chat_id == NORMAL_REVIEW
        and int(user_id or 0) == NORMAL_REVIEW
    )

    ultra_ok = (
        chat_id == REVIEW
        and is_group_admin(user_id)
    )

    if not (private_ok or ultra_ok):
        answer_callback(qid, "غير مصرح", True)
        return

    if ":" not in data:
        answer_callback(qid, "زر غير معروف", True)
        return

    action, did = data.split(":", 1)

    try:
        if action in ("u", "n"):
            status, mid = publish(
                did,
                urgent=(action == "u"),
            )

            clear_buttons(mid, chat_id)

            answer_callback(
                qid,
                "تم النشر ✅"
                if status == "posted"
                else "تم التعامل",
            )

        elif action == "x":
            status, mid = reject(did)

            clear_buttons(mid, chat_id)

            answer_callback(
                qid,
                "تم الرفض ❌"
                if status == "rejected"
                else "تم التعامل",
            )

        else:
            answer_callback(
                qid,
                "زر غير معروف",
                True,
            )

    except Exception as e:
        log(f"callback error: {e}")
        answer_callback(
            qid,
            "حدث خطأ — لم يتم النشر",
            True,
        )


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(path)

def queue_loop():
    """
    QUEUE_FAST_SAFE_V2

    - Never loses a good row.
    - Incomplete last line waits for writer to finish.
    - Poison row retries 3 times then goes to dead-letter.
    - Offset advances only after handled row.
    - Existing dedupe remains active.
    """

    INBOX.parent.mkdir(
        parents=True,
        exist_ok=True
    )
    INBOX.touch(exist_ok=True)

    dead = INBOX.parent / "queue_dead_letter.jsonl"

    state = load_json(
        QUEUE_STATE,
        {"offset": 0}
    )

    offset = int(
        state.get("offset", 0)
        or 0
    )

    size = INBOX.stat().st_size

    if offset > size:
        log(
            f"QUEUE OFFSET RESET old={offset} size={size}"
        )
        offset = 0
        save_json(
            QUEUE_STATE,
            {"offset": offset}
        )

    retries = {}

    log(
        f"QUEUE FAST V2 ON path={INBOX} "
        f"offset={offset} size={size}"
    )

    while True:
        try:
            size = INBOX.stat().st_size

            if size <= offset:
                time.sleep(0.5)
                continue

            with INBOX.open(
                "r",
                encoding="utf-8"
            ) as f:

                f.seek(offset)

                while True:
                    pos = f.tell()
                    line = f.readline()

                    if not line:
                        break

                    # Writer may still be writing the final JSONL row.
                    if not line.endswith("\n"):
                        break

                    nxt = f.tell()

                    try:
                        p = json.loads(line)

                    except Exception as e:
                        n = retries.get(pos, 0) + 1
                        retries[pos] = n

                        log(
                            f"QUEUE JSON ERROR pos={pos} "
                            f"try={n}/3 error={e}"
                        )

                        if n < 3:
                            break

                        with dead.open(
                            "a",
                            encoding="utf-8"
                        ) as df:
                            df.write(
                                json.dumps(
                                    {
                                        "time": int(time.time()),
                                        "position": pos,
                                        "error": str(e),
                                        "raw": line[:4000],
                                    },
                                    ensure_ascii=False
                                )
                                + "\n"
                            )

                        log(
                            f"QUEUE POISON SKIPPED pos={pos}"
                        )

                        offset = nxt
                        save_json(
                            QUEUE_STATE,
                            {"offset": offset}
                        )

                        retries.pop(pos, None)
                        continue

                    asin = str(
                        p.get("asin")
                        or "NO_ASIN"
                    )

                    log(
                        f"QUEUE PROCESS pos={pos} "
                        f"asin={asin}"
                    )

                    try:
                        send_review(p)

                    except Exception as e:
                        n = retries.get(pos, 0) + 1
                        retries[pos] = n

                        log(
                            f"QUEUE REVIEW ERROR pos={pos} "
                            f"asin={asin} try={n}/3 "
                            f"error={e}"
                        )

                        if n < 3:
                            break

                        with dead.open(
                            "a",
                            encoding="utf-8"
                        ) as df:
                            df.write(
                                json.dumps(
                                    {
                                        "time": int(time.time()),
                                        "position": pos,
                                        "asin": asin,
                                        "error": str(e),
                                        "payload": p,
                                    },
                                    ensure_ascii=False
                                )
                                + "\n"
                            )

                        log(
                            f"QUEUE REVIEW DEADLETTER "
                            f"asin={asin} pos={pos}"
                        )

                    else:
                        log(
                            f"QUEUE DONE pos={pos} "
                            f"asin={asin}"
                        )

                    offset = nxt

                    save_json(
                        QUEUE_STATE,
                        {"offset": offset}
                    )

                    retries.pop(pos, None)

                    # Fast drain; no old 1-second delay.
                    time.sleep(0.05)

        except Exception as e:
            log(
                f"QUEUE LOOP ERROR: {e}"
            )
            time.sleep(1)


def initial_offset():
    s = load_json(STATE, {})
    if "offset" in s:
        return int(s["offset"])
    try:
        rows = api("getUpdates", {"offset": -1, "timeout": 0}, timeout=10)
        if rows:
            off = int(rows[-1]["update_id"]) + 1
        else:
            off = 0
    except Exception:
        off = 0
    save_json(STATE, {"offset": off})
    return off

def updates_loop():
    offset = initial_offset()
    log(f"TELEGRAM POLLING ON offset={offset}")
    while True:
        try:
            rows = api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 20,
                    "allowed_updates": ["callback_query"],
                },
                timeout=30,
            )
            for u in rows:
                offset = int(u["update_id"]) + 1
                save_json(STATE, {"offset": offset})
                if u.get("callback_query"):
                    handle_callback(u["callback_query"])
        except Exception as e:
            log(f"polling error: {e}")
            time.sleep(3)

def main():
    db_init()
    t = threading.Thread(target=queue_loop, daemon=True)
    t.start()
    if os.getenv("AMAZON_REVIEW_QUEUE_ONLY", "0") == "1":
        log("TELEGRAM POLLING OFF | QUEUE-ONLY MODE")
        while True:
            time.sleep(3600)
    updates_loop()

if __name__ == "__main__":
    main()
