import asyncio
import inspect
import json
import logging
import re
import os
import urllib.request


from config import settings
from models import Deal
from stores import CONNECTORS
from comparison import DealVerifier, comparison_html, product_key
from price_intelligence_v7 import build_price_signals, signal_key, qualifies_v7
from db import (
    save_seen, save_market_observation, should_review, save_pending,
    set_admin_message_id, get_pending_by_short, set_pending_status,
    mark_posted, historical_low,
)
from telegram_client import (
    send_admin_review, send_deal, answer_callback,
    clear_review_buttons, get_updates,
)

log = logging.getLogger("deals-bot")


def classify_store_surface(deal):
    store = str(getattr(deal, "store", "") or "").strip().lower()
    title = str(getattr(deal, "title", "") or "").strip().lower()
    url = str(getattr(deal, "url", "") or "").strip().lower()
    category = str(getattr(deal, "category", "") or "").strip().lower()
    hay = " | ".join([store, title, url, category])

    condition = str(getattr(deal, "condition", "") or "").strip().lower()

    if not condition:
        if any(x in hay for x in ("used - like new", "used like new", "like-new", "كالجديد")):
            condition = "used_like_new"
        elif any(x in hay for x in ("used - very good", "used very good", "very good", "جيد جدا", "جيد جدًا")):
            condition = "used_very_good"
        elif any(x in hay for x in ("open box", "open-box", "amazon warehouse", "علبة مفتوحة", "مفتوح الصندوق")):
            condition = "open_box"
        elif any(x in hay for x in ("renewed", "refurbished", "مجدد", "مجدّد")):
            condition = "renewed"
        elif any(x in hay for x in ("used", "pre-owned", "preowned", "مستعمل")):
            condition = "used"
        else:
            condition = "new"

    if "amazon" in store or "amazon.eg" in url:
        if condition != "new" or any(x in hay for x in ("amazon resale", "amazon warehouse", "used", "open box", "renewed", "refurbished", "مستعمل")):
            surface = "amazon_used"
            display = "Amazon — مستعمل / Resale"
        elif any(x in hay for x in ("amazon now", "amazon-now", "/now/", "now delivery", "توصيل الآن")):
            surface = "amazon_now"
            display = "Amazon Now"
        else:
            surface = "amazon_main"
            display = "Amazon — عادي"
    elif store == "noon" or "noon.com" in url or store.startswith("noon"):
        if any(x in hay for x in ("noon minutes", "noon-minutes", "/minutes/", "minutes", "مينتس")):
            surface = "noon_minutes"
            display = "Noon Minutes"
        else:
            surface = "noon_main"
            display = "Noon — عادي"
    else:
        surface = store or "unknown"
        display = str(getattr(deal, "store", "") or "Unknown")

    deal.surface = surface
    deal.condition = condition
    return {"surface": surface, "condition": condition, "display": display}


async def send_cloud_review(deal, fp, report):
    api_url = os.getenv("CLOUD_API_URL", "").rstrip("/")
    api_key = os.getenv("CLOUD_API_KEY", "")

    if not api_url or not api_key:
        raise RuntimeError("CLOUD_API_URL or CLOUD_API_KEY is missing")

    payload = {
        "fingerprint": fp,
        "store": getattr(deal, "store", "Unknown"),
        "store_surface": classify_store_surface(deal)["surface"],
        "store_display": classify_store_surface(deal)["display"],
        "condition": classify_store_surface(deal)["condition"],
        "title": deal.title,
        "current_price": float(deal.current_price),
        "old_price": float(deal.old_price) if deal.old_price else None,
        "discount_percent": float(deal.discount_percent or 0),
        "url": deal.url,
        "verified": True,
        "comparison_report": re.sub(r"<[^>]+>", "", comparison_html(report)),
    }

    def _post():
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            api_url + "/api/deals",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "EgyptDealsBot/1.0",
                "x-api-key": api_key,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(_post)


STORE_MIN_DISCOUNT = {
    "amazon": 5,
    "jumia": 20,
    "2b": 15,
    "btech": 15,
    "raya": 15,
    "dream2000": 15,
}

def qualifies(deal):
    min_discount = STORE_MIN_DISCOUNT.get(
        deal.store.lower(),
        settings.min_discount_percent
    )

    return (
        deal.discount_percent >= min_discount
        and deal.saving >= settings.min_saving_egp
    )


async def sync_cloud_observations(deals):
    api_url = os.getenv("CLOUD_API_URL", "").rstrip("/")
    api_key = os.getenv("CLOUD_API_KEY", "")

    if not api_url or not api_key:
        return

    observations = []

    for deal in deals:
        observations.append({
            "product_key": product_key(deal),
            "store": deal.store,
            "title": deal.title,
            "current_price": float(deal.current_price),
            "old_price": (
                float(deal.old_price)
                if deal.old_price is not None
                else None
            ),
            "url": deal.url,
        })

    def _post(chunk):
        payload = json.dumps(
            {"observations": chunk},
            ensure_ascii=False
        ).encode("utf-8")

        req = urllib.request.Request(
            api_url + "/api/observations",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "EgyptDealsBot/1.0",
                "x-api-key": api_key,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=40) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    inserted = 0

    for i in range(0, len(observations), 50):
        result = await asyncio.to_thread(
            _post,
            observations[i:i + 50]
        )

        inserted += int(result.get("inserted", 0))

    log.info(
        "Cloud history sync: %d observations / %d inserted",
        len(observations),
        inserted
    )



def _v7_fast_discount(deal, signal):
    try:
        direct = float(getattr(deal, "discount_percent", 0) or 0)
    except (TypeError, ValueError):
        direct = 0.0
    try:
        effective = float(signal.get("effective_discount_percent", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        effective = 0.0
    return max(direct, effective)


def _v7_fast_tier(deal, signal):
    discount = _v7_fast_discount(deal, signal)
    if bool(signal.get("is_super_ultra", False)) or discount > 60.0:
        return 3
    if bool(signal.get("is_ultra", False)) or discount >= 40.0:
        return 2
    if discount >= 25.0:
        return 1
    return 0


def _v7_fast_key(deal, signal):
    try:
        saving = float(getattr(deal, "saving", 0) or 0)
    except (TypeError, ValueError):
        saving = 0.0
    return (
        _v7_fast_tier(deal, signal),
        _v7_fast_discount(deal, signal),
        saving,
    )


async def _v7_fast_lane_dispatch(market, dispatched):
    if not market:
        return 0

    signals = build_price_signals(market)
    urgent = []

    for deal in market:
        signal = signals.get(signal_key(deal), {})
        if _v7_fast_tier(deal, signal) < 2:
            continue
        if not qualifies_v7(deal, signal):
            continue
        urgent.append((deal, signal))

    urgent.sort(
        key=lambda item: _v7_fast_key(item[0], item[1]),
        reverse=True,
    )

    sent = 0

    for deal, signal in urgent:
        fp = save_seen(deal)

        if fp in dispatched:
            continue
        if not should_review(fp, deal):
            dispatched.add(fp)
            continue

        report = await DealVerifier(market).verify(deal)
        direct_verified = direct_store_evidence(deal)
        intelligence_verified = bool(
            signal.get("history_verified")
            or signal.get("market_verified")
        )

        verified_now = bool(
            getattr(report, "verified", False)
            or direct_verified
            or intelligence_verified
        )

        if settings.strict_verification and not verified_now:
            continue

        try:
            param_count = len(
                inspect.signature(send_cloud_review).parameters
            )
            if param_count >= 4:
                result = await send_cloud_review(
                    deal, fp, report, signal
                )
            else:
                result = await send_cloud_review(
                    deal, fp, report
                )

            cloud_result = (result.get("results") or [{}])[0]

            if cloud_result.get("ok"):
                dispatched.add(fp)
                sent += 1
                log.warning(
                    "V7 FAST LANE SENT | tier=%s | discount=%.1f%% | %s",
                    _v7_fast_tier(deal, signal),
                    _v7_fast_discount(deal, signal),
                    deal.title,
                )
                if _v7_fast_tier(deal, signal) == 2:
                    await asyncio.sleep(0.05)

        except Exception as exc:
            log.exception(
                "V7 FAST LANE cloud send failed for %s: %s",
                deal.title,
                exc,
            )

    return sent


async def run_store(name):
    cls = CONNECTORS.get(name)
    if not cls:
        return []
    try:
        c = cls(settings.timeout, settings.user_agent)
        deals = await c.fetch_deals()
        for deal in deals:
            classify_store_surface(deal)
        log.info("%s: fetched %d candidates", name, len(deals))
        return deals
    except Exception as exc:
        log.exception("%s failed: %s", name, exc)
        return []

async def scan_once():
    market = []
    fast_lane_dispatched = set()
    fast_lane_sent = 0

    tasks = [
        asyncio.create_task(run_store(store))
        for store in settings.enabled_stores
    ]

    for completed in asyncio.as_completed(tasks):
        batch = await completed
        if not batch:
            continue

        market.extend(batch)

        try:
            fast_lane_sent += await _v7_fast_lane_dispatch(
                market,
                fast_lane_dispatched,
            )
        except Exception as exc:
            log.exception("V7 FAST LANE batch failed: %s", exc)

    log.info(
        "V7 FAST LANE | early reviews sent=%d",
        fast_lane_sent,
    )

    for deal in market:
        save_seen(deal)
        save_market_observation(product_key(deal), deal)

    # Persist useful price history without uploading thousands
    # of weak/no-discount catalogue variants every cycle.
    history_market = [
        d for d in market
        if d.old_price is not None
        and d.discount_percent >= 5
    ]

    try:
        await sync_cloud_observations(history_market)
    except Exception as exc:
        log.exception("Cloud history sync failed: %s", exc)

    # Give every store a fair chance instead of allowing one store
    # (usually Jumia) to occupy all verification slots.
    by_store = {}

    for d in market:
        if qualifies(d):
            by_store.setdefault(d.store.lower(), []).append(d)

    # Products that appear under the same normalized product key
    # in multiple stores are much easier and safer to verify.
    key_stores = {}

    for d in market:
        key_stores.setdefault(
            product_key(d),
            set()
        ).add(d.store.lower())

    def verification_priority(d):
        exact_cross_store = (
            len(key_stores.get(product_key(d), set())) >= 2
        )

        # Prefer products with identifiable model numbers.
        # These are much safer to compare across stores than generic items.
        title = str(d.title).upper()

        has_model = bool(
            re.search(
                r"\b(?=[A-Z0-9._/-]*[A-Z])"
                r"(?=[A-Z0-9._/-]*\d)"
                r"[A-Z0-9][A-Z0-9._/-]{2,}\b",
                title
            )
        )

        return (
            1 if exact_cross_store else 0,
            1 if has_model else 0,
            d.discount_percent,
            d.saving,
        )

    for store_deals in by_store.values():
        store_deals.sort(
            key=verification_priority,
            reverse=True
        )

    candidates = []
    store_order = list(settings.enabled_stores)

    # Round-robin between stores
    index = 0
    while len(candidates) < settings.max_verify_candidates:
        added = False

        for store in store_order:
            deals = by_store.get(store, [])

            if index < len(deals):
                candidates.append(deals[index])
                added = True

                if len(candidates) >= settings.max_verify_candidates:
                    break

        if not added:
            break

        index += 1

    verifier = DealVerifier(market)
    sent = 0

    for deal in candidates:
        if sent >= settings.max_posts_per_cycle:
            break
        fp = save_seen(deal)
        if not should_review(fp, deal):
            continue

        report = await verifier.verify(deal)
        if settings.strict_verification and not report.verified:
            log.info("UNVERIFIED rejected: %s | %s", deal.title, report.reason)
            continue

        try:
            result = await send_cloud_review(deal, fp, report)
            cloud_result = (result.get("results") or [{}])[0]

            if cloud_result.get("ok"):
                log.info("CLOUD review sent: %s", deal.title)
                sent += 1
            else:
                log.warning(
                    "CLOUD rejected: %s | %s",
                    deal.title,
                    cloud_result
                )

        except Exception as exc:
            log.exception("Cloud API failed for %s: %s", deal.title, exc)

        try:
            _signal_for_delay = build_price_signals(market).get(
                signal_key(deal),
                {},
            )
            _tier_for_delay = _v7_fast_tier(
                deal,
                _signal_for_delay,
            )
        except Exception:
            _tier_for_delay = 0

        if _tier_for_delay >= 3:
            await asyncio.sleep(0)
        elif _tier_for_delay == 2:
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(1)

    log.info("Cycle complete: %d VERIFIED offers sent for review", sent)
    return sent

def _deal_from_payload(payload):
    data = json.loads(payload)
    data.pop("discount_percent", None)
    return Deal(**data)

async def handle_callback(cb):
    cb_id = cb.get("id")
    from_user = cb.get("from", {})
    message = cb.get("message", {})
    data = cb.get("data", "")

    if int(from_user.get("id", 0)) != settings.admin_chat_id:
        await answer_callback(settings.telegram_bot_token, cb_id, "غير مصرح", True)
        return
    if ":" not in data:
        return

    action, short_fp = data.split(":", 1)
    row = get_pending_by_short(short_fp)
    if not row:
        await answer_callback(settings.telegram_bot_token, cb_id, "العرض غير موجود", True)
        return
    if row["status"] != "pending":
        await answer_callback(settings.telegram_bot_token, cb_id, "تم التعامل مع العرض بالفعل")
        return

    deal = _deal_from_payload(row["payload"])
    fp = row["fingerprint"]

    if action == "r":
        set_pending_status(fp, "rejected")
        await answer_callback(settings.telegram_bot_token, cb_id, "تم تجاهل العرض ❌")
    elif action in ("p", "f"):
        # Re-check immediately before publishing.
        report = await DealVerifier([]).verify(deal)
        if settings.strict_verification and not report.verified:
            set_pending_status(fp, "rejected")
            await answer_callback(
                settings.telegram_bot_token, cb_id,
                "العرض لم يعد يحقق شروط التحقق، لذلك لم يتم نشره.", True
            )
            return

        low = historical_low(fp)
        await send_deal(
            settings.telegram_bot_token, settings.telegram_channel_id, deal,
            is_historical_low=(low is None or deal.current_price <= low),
            featured=(action == "f"),
        )
        mark_posted(fp, deal)
        set_pending_status(fp, "posted")
        await answer_callback(
            settings.telegram_bot_token, cb_id,
            "تم النشر المميز ⭐" if action == "f" else "تم النشر ✅"
        )
    else:
        return

    try:
        await clear_review_buttons(
            settings.telegram_bot_token,
            int(message["chat"]["id"]), int(message["message_id"])
        )
    except Exception:
        pass

async def moderation_loop():
    offset = None
    while True:
        try:
            updates = await get_updates(settings.telegram_bot_token, offset=offset, timeout=25)
            for update in updates:
                offset = int(update["update_id"]) + 1
                if update.get("callback_query"):
                    await handle_callback(update["callback_query"])
        except Exception as exc:
            log.exception("Moderation polling error: %s", exc)
            await asyncio.sleep(5)
