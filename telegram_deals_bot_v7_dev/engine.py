import asyncio
import inspect
import json
import logging
import re
import os
import urllib.request
import time


from config import settings
from models import Deal
from stores import CONNECTORS
from ultra_speed_v7 import (
    is_instant_candidate, semantic_price_sanity, cold_start_truth,
    select_new_or_changed, ensure_first_seen, mark_stage, latency_snapshot,
    clear_runtime_state,
)
from priority_truth_v7 import (
    classify_priority_truth, dedupe_deals,
    is_fast_candidate, is_private_route, cloud_priority,
)
from multistore_campaign_v7 import (
    next_campaign_url, discover_campaign_links,
    remember_campaign_links, capability_label,
)
from comparison import DealVerifier, comparison_html, product_key
from price_intelligence_v7 import (
    build_price_signals, signal_key, qualifies_v7,
    signal_verified, signal_text,
)
from db import (
    save_seen, save_market_observation, should_review, save_pending,
    set_admin_message_id, get_pending_by_short, set_pending_status,
    mark_posted, historical_low,
)
from telegram_client import (
    send_admin_review, send_deal, answer_callback,
    clear_review_buttons, get_updates,
    send_flash_admin_review, send_custom_post,
    build_message, build_featured_message,
)
from flash_review_v9 import build_flash_review_card
from review_media import prepare_review_media

log = logging.getLogger("deals-bot")
AMAZON_READY_HOME = os.getenv("AMAZON_READY_HOME", os.path.expanduser("~/amazon_dynamic_runtime_v8"))

# V11 FAST SAFE: bounded store fetches + in-process circuit breakers.
V11_FAST_STORE_TIMEOUTS = {
    "noon": 18,
    "noon_minutes": 14,
    "jumia": 45,
    "2b": 22,
    "btech": 22,
    "raya": 22,
    "dream2000": 20,
    "carrefour": 20,
    "raneen": 18,
    "kenzz": 20,
}
_V11_STORE_HEALTH = {}

def _v11_store_health(name):
    return _V11_STORE_HEALTH.setdefault(
        str(name).lower(),
        {"failures": 0, "cooldown_until": 0.0},
    )

def _v11_cooldown_seconds(name, exc):
    text = str(exc).lower()
    if any(x in text for x in ("403", "429", "503", "captcha", "robot check")):
        return 15 * 60
    if str(name).lower() in {"noon", "noon_minutes"}:
        return 8 * 60
    return 4 * 60


_V7_PRIORITY_OVERRIDES = {}
_V7_DETECTION_KINDS = {}

# V11 priority policy:
# Amazon > Noon > Jumia > other stores > Kenzz.
# True independently-verified ULTRA still outranks ordinary deals.
V11_STORE_PRIORITY = {
    "amazon": 100,
    "noon": 90,
    "noon_minutes": 88,
    "jumia": 80,
    "2b": 60,
    "btech": 58,
    "raya": 56,
    "dream2000": 54,
    "carrefour": 52,
    "raneen": 50,
    "kenzz": 5,
}

V11_STORE_VERIFY_QUOTA = {
    "amazon": 4,
    "noon": 3,
    "noon_minutes": 1,
    "jumia": 3,
    "2b": 1,
    "btech": 1,
    "raya": 1,
    "dream2000": 1,
    "carrefour": 1,
    "raneen": 1,
    "kenzz": 1,
}


def _v11_store_key(deal):
    store = str(getattr(deal, "store", "") or "").strip().lower()
    url = str(getattr(deal, "url", "") or "").lower()

    if "amazon" in store or "amazon.eg" in url:
        return "amazon"
    if store.startswith("noon") or "noon.com" in url:
        if "minutes" in store or "/minutes/" in url:
            return "noon_minutes"
        return "noon"
    if store in {"2b", "twob"}:
        return "2b"
    return store


def _v11_store_priority(deal):
    return V11_STORE_PRIORITY.get(_v11_store_key(deal), 20)


def _v11_independent_reference(signal):
    signal = signal or {}
    return bool(
        signal.get("history_verified")
        or signal.get("market_verified")
    )


def _v11_kenzz_fast_allowed(deal, signal):
    if _v11_store_key(deal) != "kenzz":
        return True
    return _v11_independent_reference(signal)


def classify_store_surface(deal):
    store = str(getattr(deal, "store", "") or "").strip().lower()
    title = str(getattr(deal, "title", "") or "").strip().lower()
    url = str(getattr(deal, "url", "") or "").strip().lower()
    category = str(getattr(deal, "category", "") or "").strip().lower()

    hay = " | ".join([store, title, url, category])

    condition = str(
        getattr(deal, "condition", "") or ""
    ).strip().lower()

    if not condition:
        if any(x in hay for x in (
            "used - like new", "used like new",
            "كالجديد", "like-new",
        )):
            condition = "used_like_new"
        elif any(x in hay for x in (
            "used - very good", "used very good",
            "very good", "جيد جدا", "جيد جدًا",
        )):
            condition = "used_very_good"
        elif any(x in hay for x in (
            "open box", "open-box", "warehouse",
            "علبة مفتوحة", "مفتوح الصندوق",
        )):
            condition = "open_box"
        elif any(x in hay for x in (
            "renewed", "refurbished",
            "مجدد", "مجدّد",
        )):
            condition = "renewed"
        elif any(x in hay for x in (
            "used", "pre-owned", "preowned", "مستعمل",
        )):
            condition = "used"
        else:
            condition = "new"

    if "amazon" in store or "amazon.eg" in url:
        if condition != "new" or any(x in hay for x in (
            "amazon resale",
            "amazon warehouse",
            "used",
            "open box",
            "renewed",
            "refurbished",
            "مستعمل",
        )):
            surface = "amazon_used"
            display = "Amazon — مستعمل / Resale"
        elif any(x in hay for x in (
            "amazon now",
            "amazon-now",
            "/now/",
            "now delivery",
            "توصيل الآن",
        )):
            surface = "amazon_now"
            display = "Amazon Now"
        else:
            surface = "amazon_main"
            display = "Amazon — عادي"

    elif store == "noon" or "noon.com" in url or store.startswith("noon"):
        if any(x in hay for x in (
            "noon minutes",
            "noon-minutes",
            "minutes",
            "/minutes/",
            "مينتس",
        )):
            surface = "noon_minutes"
            display = "Noon Minutes"
        else:
            surface = "noon_main"
            display = "Noon — عادي"

    else:
        surface = store or "unknown"
        display = str(getattr(deal, "store", "") or "Unknown")

    try:
        deal.surface = surface
        deal.condition = condition
    except Exception:
        pass

    return {
        "surface": surface,
        "condition": condition,
        "display": display,
    }



def _direct_flash_review_enabled():
    return bool(
        settings.direct_flash_review
        and settings.telegram_bot_token
        and settings.review_chat_id
    )


def _pending_review_meta(row):
    try:
        raw = json.loads(row.get("payload") or "{}")
        meta = raw.get("_review_meta") or {}
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


async def _send_direct_flash_review(
    deal,
    fp,
    signal,
    truth,
):
    signal = signal or {}

    reference_verified = bool(
        signal.get("history_verified")
        or signal.get("market_verified")
    )
    reference_price = (
        signal.get("reference_price")
        or signal.get("market_reference_price")
        or signal.get("historical_typical_price")
    )

    media_meta = await prepare_review_media(deal, fp[:16])

    card = build_flash_review_card(
        store=classify_store_surface(deal)["display"],
        title=deal.title,
        current_price=deal.current_price,
        old_price=deal.old_price,
        discount_percent=(
            signal.get("effective_discount_percent")
            or deal.discount_percent
            or 0
        ),
        url=deal.url,
        image_url=getattr(deal, "image_url", None),
        route=truth.get("route") or "PRIVATE_REVIEW",
        priority_class=truth.get("class") or "FAST_REVIEW",
        price_verified=bool(
            getattr(deal, "live_rechecked", None) is True
        ),
        reference_verified=reference_verified,
        reference_price=(
            reference_price
            if reference_verified
            else None
        ),
        asin=getattr(deal, "external_id", None) or "",
    )
    card.update(media_meta)

    message = await send_flash_admin_review(
        settings.telegram_bot_token,
        settings.review_chat_id,
        deal,
        fp[:16],
        card,
    )

    message_id = (
        message.get("message_id")
        if isinstance(message, dict)
        else None
    )

    # Persist only after Telegram accepted the review.
    # If Telegram fails, the deal stays retryable next cycle.
    save_pending(
        fp,
        deal,
        admin_message_id=(
            int(message_id)
            if message_id is not None
            else None
        ),
        review_meta=card,
    )

    return {
        "results": [
            {
                "ok": True,
                "mode": "direct_telegram",
                "message_id": message_id,
            }
        ]
    }


async def send_cloud_review(deal, fp, report, signal=None):
    api_url = os.getenv("CLOUD_API_URL", "").rstrip("/")
    api_key = os.getenv("CLOUD_API_KEY", "")

    if not api_url or not api_key:
        raise RuntimeError("CLOUD_API_URL or CLOUD_API_KEY is missing")

    signal = signal or {}
    effective_discount = float(
        signal.get("effective_discount_percent")
        or deal.discount_percent
        or 0
    )
    effective_old = (
        float(deal.old_price)
        if deal.old_price
        else signal.get("reference_price")
    )

    if report is None:
        report_text = (
            "⚡ Fast Lane preliminary review\n"
            "Strong deal detected; full comparison/enrichment continues separately."
        )
    else:
        report_text = re.sub(r"<[^>]+>", "", comparison_html(report))

    extra_signal_text = signal_text(signal)
    if extra_signal_text:
        report_text += "\n\n🧠 Price Intelligence V7\n" + extra_signal_text

    priority_truth = _V7_PRIORITY_OVERRIDES.get(fp)
    if not isinstance(priority_truth, dict):
        priority_truth = classify_priority_truth(
            deal,
            signal,
            getattr(deal, "live_rechecked", None),
        )

    payload = {
        "fingerprint": fp,
        "store": getattr(deal, "store", "Unknown"),
        "store_surface": classify_store_surface(deal)["surface"],
        "store_display": classify_store_surface(deal)["display"],
        "condition": classify_store_surface(deal)["condition"],
        "title": deal.title,
        "current_price": float(deal.current_price),
        "old_price": float(effective_old) if effective_old else None,
        "discount_percent": effective_discount,
        "url": deal.url,
        "image_url": getattr(deal, "image_url", None),
        "live_rechecked": getattr(deal, "live_rechecked", None),
        "live_recheck_price": getattr(deal, "live_recheck_price", None),
        "live_recheck_source": getattr(deal, "live_recheck_source", None),
        "title_ar": getattr(deal, "title_ar", None) or deal.title,
        "verification_source": (
            "store_old_price"
            if direct_store_evidence(deal)
            else signal.get("reference_source") or "comparison"
        ),
        "priority": cloud_priority(priority_truth),
        "priority_truth": priority_truth,
        "detection_kind": _V7_DETECTION_KINDS.get(fp),
        "ultra_speed_latency": latency_snapshot(deal),
        "review_mode": "flash" if report is None else "full",
        "preliminary": bool(report is None),
        "verified": bool(
            (
                report is not None
                and bool(getattr(report, "verified", False))
            )
            or direct_store_evidence(deal)
            or getattr(deal, "live_rechecked", None) is True
            or bool(signal.get("history_verified"))
            or bool(signal.get("market_verified"))
        ),
        "price_verified": bool(
            getattr(deal, "live_rechecked", None) is True
        ),
        "reference_verified": bool(
            signal.get("history_verified")
            or signal.get("market_verified")
        ),
        "comparison_report": report_text,
        "price_intelligence": {
            "previous_price": signal.get("previous_price"),
            "previous_change_percent": signal.get("previous_change_percent"),
            "historical_typical_price": signal.get("historical_typical_price"),
            "historical_drop_percent": signal.get("historical_drop_percent"),
            "market_reference_price": signal.get("market_reference_price"),
            "market_advantage_percent": signal.get("market_advantage_percent"),
            "reference_source": signal.get("reference_source"),
        },
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
    "jumia": 5,
    "2b": 5,
    "noon": 5,
    "noon_minutes": 5,
    "btech": 5,
    "raya": 5,
    "dream2000": 5,
    "carrefour": 5,
    "raneen": 5,
    "kenzz": 5,
}
def _safe_num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def direct_store_evidence(deal):
    current = _safe_num(getattr(deal, "current_price", 0))
    old = _safe_num(getattr(deal, "old_price", 0))
    stated = _safe_num(getattr(deal, "discount_percent", 0))

    # Kenzz guard: its own displayed old price is not independent verification.
    if _v11_store_key(deal) == "kenzz":
        return False

    if current <= 0 or old <= current:
        return False

    calculated = ((old - current) / old) * 100.0
    if calculated < 5.0:
        return False

    if stated > 0 and abs(calculated - stated) > 4.0:
        return False

    return True


def qualifies(deal):
    # V7 policy: every genuine 5%+ primary discount is eligible.
    return _safe_num(getattr(deal, "discount_percent", 0)) >= 5.0


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

    candidate_market = select_new_or_changed(market)
    if not candidate_market:
        return 0

    for deal in candidate_market:
        ensure_first_seen(deal)

    full_market = dedupe_deals(market, product_key)
    signals = build_price_signals(full_market)
    candidate_market = dedupe_deals(candidate_market, product_key)

    urgent = []

    for deal in candidate_market:
        signal = signals.get(signal_key(deal), {})
        truth = classify_priority_truth(
            deal,
            signal,
            getattr(deal, "live_rechecked", None),
        )
        instant = is_instant_candidate(deal, signal, truth)

        if not _v11_kenzz_fast_allowed(deal, signal):
            log.info(
                "V11 KENZZ FAST-LANE HELD FOR INDEPENDENT EVIDENCE | %s",
                deal.title,
            )
            continue

        if not instant.get("candidate"):
            continue

        mark_stage(deal, "candidate")

        if (
            instant.get("kind") != "cold_start_semantic"
            and not qualifies_v7(deal, signal)
        ):
            continue

        urgent.append((deal, signal, truth, instant))

    urgent.sort(
        key=lambda item: (
            int(item[2].get("score", 0)),
            int(item[3].get("score", 0)),
            _v11_store_priority(item[0]),
            _v7_fast_key(item[0], item[1]),
        ),
        reverse=True,
    )

    prepared = []

    for deal, signal, truth, instant in urgent:
        fp = save_seen(deal)

        if fp in dispatched:
            continue

        if not should_review(fp, deal):
            dispatched.add(fp)
            continue

        prepared.append((deal, signal, truth, instant, fp))

    if not prepared:
        return 0

    concurrency = max(
        1,
        min(
            int(os.getenv("V7_ULTRA_RECHECK_CONCURRENCY", "10")),
            20,
        ),
    )
    recheck_timeout = max(
        1.5,
        min(
            float(os.getenv("V7_ULTRA_RECHECK_TIMEOUT", "3")),
            10.0,
        ),
    )
    sem = asyncio.Semaphore(concurrency)

    async def process_one(item):
        deal, signal, truth, instant, fp = item

        async with sem:
            mark_stage(deal, "recheck_start")

            try:
                live_status = await asyncio.wait_for(
                    _v7_flash_live_recheck(deal),
                    timeout=recheck_timeout,
                )
            except asyncio.TimeoutError:
                live_status = None
                log.warning(
                    "V7 ULTRA SPEED RECHECK TIMEOUT | %.1fs | %s | %s",
                    recheck_timeout,
                    deal.store,
                    deal.title,
                )
            except Exception as exc:
                live_status = None
                log.debug(
                    "V7 ULTRA SPEED RECHECK ERROR | %s | %s | %s",
                    deal.store,
                    deal.title,
                    exc,
                )

            mark_stage(deal, "recheck_done")

            if live_status is False:
                log.warning(
                    "V7 ULTRA SPEED REJECTED | FAST | %s | %s",
                    deal.store,
                    deal.title,
                )
                return 0

            truth_now = classify_priority_truth(
                deal,
                signal,
                live_status,
            )

            semantic = (
                instant.get("semantic")
                or semantic_price_sanity(deal)
            )
            semantic_truth = cold_start_truth(
                deal,
                semantic,
                live_status,
            )

            if (
                semantic_truth
                and int(semantic_truth.get("score", 0))
                > int(truth_now.get("score", 0))
            ):
                truth_now = semantic_truth

            direct_discount = _safe_num(
                getattr(deal, "discount_percent", 0)
            )
            direct_evidence = direct_store_evidence(deal)
            intelligence_verified = bool(
                signal.get("history_verified")
                or signal.get("market_verified")
            )

            if (
                not is_private_route(truth_now)
                and live_status is None
                and direct_discount >= 40.0
                and direct_evidence
            ):
                truth_now = {
                    "class": "FAST_40_REVIEW",
                    "route": "PRIVATE_REVIEW",
                    "score": max(72, min(89, int(direct_discount))),
                    "reason": (
                        "fresh 40%+ store evidence; secondary live recheck "
                        "unavailable/timed out"
                    ),
                }

            if (
                not is_private_route(truth_now)
                and live_status is None
                and semantic.get("candidate")
                and semantic.get("identity_guard")
                and int(semantic.get("score", 0)) >= 92
            ):
                truth_now = {
                    "class": "COLD_START_SUSPECT",
                    "route": "PRIVATE_REVIEW",
                    "score": int(semantic.get("score", 92)),
                    "reason": (
                        "severe semantic anomaly from fresh source; "
                        "secondary live confirmation pending"
                    ),
                }

            if not is_private_route(truth_now):
                return 0

            fast_review_allowed = bool(
                direct_evidence
                or intelligence_verified
                or live_status is True
                or truth_now.get("class")
                in {"FAST_40_REVIEW", "COLD_START_SUSPECT"}
            )

            if settings.strict_verification and not fast_review_allowed:
                return 0

            mark_stage(deal, "private_ready")
            _V7_PRIORITY_OVERRIDES[fp] = truth_now
            _V7_DETECTION_KINDS[fp] = instant.get("kind")

            try:
                if _direct_flash_review_enabled():
                    result = await _send_direct_flash_review(
                        deal,
                        fp,
                        signal,
                        truth_now,
                    )
                else:
                    result = await send_cloud_review(
                        deal,
                        fp,
                        None,
                        signal,
                    )

                cloud_result = (result.get("results") or [{}])[0]

                if not cloud_result.get("ok"):
                    return 0

                dispatched.add(fp)
                mark_stage(deal, "sent")
                latency = latency_snapshot(deal)

                log.warning(
                    "V7 FLASH REVIEW LATENCY | candidate_ms=%s | "
                    "recheck_done_ms=%s | ready_ms=%s | sent_ms=%s | "
                    "store=%s | %s",
                    latency.get("candidate_ms"),
                    latency.get("recheck_done_ms"),
                    latency.get("private_ready_ms"),
                    latency.get("sent_ms"),
                    deal.store,
                    deal.title,
                )

                log.warning(
                    "V7 PRIVATE FAST LANE SENT | class=%s | route=%s | "
                    "discount=%.1f%% | %s",
                    truth_now.get("class"),
                    truth_now.get("route"),
                    _v7_fast_discount(deal, signal),
                    deal.title,
                )
                return 1

            except Exception as exc:
                log.exception(
                    "V7 ultra-speed flash review failed: %s",
                    exc,
                )
                return 0

    results = await asyncio.gather(
        *(process_one(item) for item in prepared),
        return_exceptions=True,
    )

    sent = 0
    for result in results:
        if isinstance(result, Exception):
            log.error("V7 ultra-speed worker failed: %s", result)
            continue
        sent += int(result or 0)

    return sent

async def _v7_parse_campaign_page(name, connector, url):
    name = str(name).lower()

    try:
        if name == "kenzz" and hasattr(connector, "_get"):
            html = await asyncio.wait_for(
                asyncio.to_thread(connector._get, url),
                timeout=18,
            )
            links = discover_campaign_links(name, html, url)
            remember_campaign_links(name, links)
            return list(connector._parse(html) or [])

        if not hasattr(connector, "get_soup"):
            return []

        soup = await asyncio.wait_for(
            connector.get_soup(url),
            timeout=18,
        )

        links = discover_campaign_links(name, soup, url)
        remember_campaign_links(name, links)

        if name in ("jumia", "2b") and hasattr(connector, "_parse_cards"):
            return list(connector._parse_cards(soup) or [])

        if name in ("noon", "noon_minutes") and hasattr(connector, "_parse_page"):
            return list(connector._parse_page(soup) or [])

        if name == "btech" and hasattr(connector, "_extract_items"):
            out = []
            for item in connector._extract_items(soup):
                try:
                    deal = connector._make_deal(item, discounts_only=False)
                except TypeError:
                    deal = connector._make_deal(item)
                if deal:
                    out.append(deal)
            return out

        if name == "raya" and hasattr(connector, "_parse_cards"):
            return list(
                connector._parse_cards(soup, only_discounts=False)
                or []
            )

        if name == "carrefour" and hasattr(connector, "_parse_page"):
            return list(connector._parse_page(soup) or [])

        if name == "raneen" and hasattr(connector, "_parse_cards"):
            try:
                return list(
                    connector._parse_cards(
                        soup,
                        discounts_only=False,
                    )
                    or []
                )
            except TypeError:
                return list(connector._parse_cards(soup) or [])

    except Exception as exc:
        log.debug(
            "V7 campaign page failed | %s | %s",
            name,
            exc,
        )

    return []


async def run_store_campaign(name):
    cls = CONNECTORS.get(name)
    if not cls:
        return []

    if str(name).lower() == "dream2000":
        return []

    try:
        connector = cls(
            settings.timeout,
            settings.user_agent,
        )
        url = next_campaign_url(name, connector)
        if not url:
            return []

        deals = await _v7_parse_campaign_page(
            name,
            connector,
            url,
        )

        for deal in deals:
            try:
                classify_store_surface(deal)
            except Exception:
                pass

        log.info(
            "V7 CAMPAIGN | %s | %s | products=%d",
            name,
            url,
            len(deals),
        )
        return deals

    except Exception as exc:
        log.debug(
            "V7 campaign runner failed | %s | %s",
            name,
            exc,
        )
        return []


async def _v7_flash_live_recheck(deal):
    """One-shot preliminary price confirmation for Fast Lane."""
    existing = getattr(deal, "live_rechecked", None)
    if existing is True or existing is False:
        return existing

    name = str(getattr(deal, "store", "") or "").lower()
    cls = CONNECTORS.get(name)
    if not cls:
        return None

    connector = cls(settings.timeout, settings.user_agent)
    exact = getattr(connector, "recheck_exact", None)
    price = None

    if exact:
        try:
            price = await asyncio.wait_for(exact(deal), timeout=2.35)
        except Exception:
            price = None
    else:
        searcher = (
            getattr(connector, "search_products", None)
            or getattr(connector, "search", None)
        )
        if not searcher:
            return None
        try:
            results = await asyncio.wait_for(searcher(deal.title), timeout=2.35)
        except Exception:
            return None

        target_url = str(getattr(deal, "url", "") or "").split("?")[0]
        for item in list(results or []):
            item_url = str(getattr(item, "url", "") or "").split("?")[0]
            if target_url and item_url == target_url:
                try:
                    price = float(item.current_price)
                    break
                except (TypeError, ValueError):
                    pass

        if price is None:
            try:
                target_key = product_key(deal)
            except Exception:
                target_key = None
            if target_key:
                for item in list(results or []):
                    try:
                        if product_key(item) == target_key:
                            price = float(item.current_price)
                            break
                    except Exception:
                        continue

    if price is None:
        return None

    try:
        deal.live_recheck_price = float(price)
        deal.live_recheck_source = name
    except Exception:
        pass

    original = float(getattr(deal, "current_price", 0) or 0)
    tolerance = max(1.0, original * 0.08)

    if float(price) <= original + tolerance:
        try:
            deal.live_rechecked = True
        except Exception:
            pass
        return True

    try:
        deal.live_rechecked = False
    except Exception:
        pass
    return False


async def _v7_multistore_live_recheck(deal):
    existing = getattr(deal, "live_rechecked", None)
    if existing is True or existing is False:
        return existing

    name = str(getattr(deal, "store", "") or "").lower()
    cls = CONNECTORS.get(name)

    if not cls:
        return None

    async def probe():
        connector = cls(
            settings.timeout,
            settings.user_agent,
        )

        searcher = (
            getattr(connector, "search_products", None)
            or getattr(connector, "search", None)
        )

        if not searcher:
            return None

        try:
            results = await asyncio.wait_for(
                searcher(deal.title),
                timeout=22,
            )
        except Exception:
            return None

        target_url = str(
            getattr(deal, "url", "") or ""
        ).split("?")[0]

        for item in list(results or []):
            item_url = str(
                getattr(item, "url", "") or ""
            ).split("?")[0]

            if target_url and item_url == target_url:
                try:
                    return float(item.current_price)
                except (TypeError, ValueError):
                    continue

        try:
            target_key = product_key(deal)
        except Exception:
            target_key = None

        if target_key:
            matches = []
            for item in list(results or []):
                try:
                    if product_key(item) == target_key:
                        matches.append(float(item.current_price))
                except Exception:
                    continue

            if matches:
                return min(matches)

        return None

    first = await probe()

    if first is None:
        deal.live_rechecked = None
        deal.live_recheck_source = name
        return None

    await asyncio.sleep(0.15)
    second = await probe()

    if second is None:
        deal.live_rechecked = None
        deal.live_recheck_price = first
        deal.live_recheck_source = name
        return None

    deal.live_recheck_price = second
    deal.live_recheck_source = name

    original = float(
        getattr(deal, "current_price", 0) or 0
    )
    tolerance = max(1.0, original * 0.08)

    if (
        first <= original + tolerance
        and second <= original + tolerance
    ):
        deal.live_rechecked = True
        return True

    if (
        first > original + tolerance
        and second > original + tolerance
    ):
        deal.live_rechecked = False
        return False

    deal.live_rechecked = None
    return None


async def run_store(name):
    name = str(name).lower()
    cls = CONNECTORS.get(name)
    if not cls:
        log.warning("Unknown store: %s", name)
        return []

    health = _v11_store_health(name)
    now = time.monotonic()
    if health["cooldown_until"] > now:
        remain = int(health["cooldown_until"] - now)
        log.warning("V11 STORE COOLDOWN | %s | remaining=%ss", name, remain)
        return []

    timeout = float(V11_FAST_STORE_TIMEOUTS.get(name, max(18, settings.timeout)))
    connector = cls(settings.timeout, settings.user_agent)

    try:
        deals = await asyncio.wait_for(
            connector.fetch_deals(),
            timeout=timeout,
        )
        deals = list(deals or [])
        health["failures"] = 0
        health["cooldown_until"] = 0.0
        log.info("V11 STORE READY | %s | fetched=%d | timeout=%ss", name, len(deals), int(timeout))
        return deals

    except asyncio.TimeoutError:
        health["failures"] += 1
        if health["failures"] >= (2 if name in {"noon", "noon_minutes"} else 3):
            health["cooldown_until"] = time.monotonic() + _v11_cooldown_seconds(name, "timeout")
        log.warning(
            "V11 STORE TIMEOUT ISOLATED | %s | failures=%d | timeout=%ss",
            name, health["failures"], int(timeout),
        )
        return []

    except Exception as exc:
        health["failures"] += 1
        text = str(exc).lower()
        protected = any(x in text for x in ("403", "429", "503", "captcha", "robot check"))
        threshold = 1 if protected else (2 if name in {"noon", "noon_minutes"} else 3)
        if health["failures"] >= threshold:
            health["cooldown_until"] = time.monotonic() + _v11_cooldown_seconds(name, exc)
        log.warning(
            "V11 STORE ERROR ISOLATED | %s | %s | failures=%d",
            name, type(exc).__name__, health["failures"],
        )
        return []

async def scan_once():
    market = []
    fast_lane_dispatched = set()
    fast_lane_sent = 0

    tasks = [
        asyncio.create_task(run_store(store))
        for store in settings.enabled_stores
    ]

    # Campaign pages run beside normal scans so Fast Lane is not blocked.
    tasks.extend(
        asyncio.create_task(run_store_campaign(store))
        for store in settings.enabled_stores
    )

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

    raw_market_count = len(market)
    market = dedupe_deals(market, product_key)

    log.info(
        "V7 DEDUPE | raw=%d | unique=%d | removed=%d",
        raw_market_count,
        len(market),
        raw_market_count - len(market),
    )

    # Build price signals before persisting this scan.
    price_signals = build_price_signals(market)

    fetched_by_store = {}
    for d in market:
        key = str(getattr(d, "store", "unknown")).lower()
        fetched_by_store[key] = fetched_by_store.get(key, 0) + 1
    log.info("V7 STORE HEALTH | FETCHED=%s", fetched_by_store)

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
        signal = price_signals.get(signal_key(d), {})
        if qualifies_v7(d, signal):
            by_store.setdefault(d.store.lower(), []).append(d)

    qualified_by_store = {
        store: len(items) for store, items in by_store.items()
    }
    log.info("V7 STORE HEALTH | QUALIFIED=%s", qualified_by_store)

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

        title = str(d.title).upper()

        has_model = bool(
            re.search(
                r"\b(?=[A-Z0-9._/-]*[A-Z])"
                r"(?=[A-Z0-9._/-]*\d)"
                r"[A-Z0-9][A-Z0-9._/-]{2,}\b",
                title
            )
        )

        signal = price_signals.get(signal_key(d), {})
        effective_discount = float(
            signal.get("effective_discount_percent")
            or d.discount_percent
            or 0
        )
        effective_saving = float(
            signal.get("saving")
            or d.saving
            or 0
        )

        return (
            1 if exact_cross_store else 0,
            1 if signal.get("is_ultra") else 0,
            1 if has_model else 0,
            effective_discount,
            effective_saving,
        )

    for store_deals in by_store.values():
        store_deals.sort(
            key=lambda d: (
                _v7_fast_key(
                    d,
                    price_signals.get(
                        signal_key(d),
                        {},
                    ),
                ),
                verification_priority(d),
            ),
            reverse=True
        )

    candidates = []
    candidate_ids = set()
    picked_by_store = {}

    preferred_order = [
        "amazon",
        "noon",
        "noon_minutes",
        "jumia",
        "2b",
        "btech",
        "raya",
        "dream2000",
        "carrefour",
        "raneen",
        "kenzz",
    ]
    preferred_order += sorted(set(by_store) - set(preferred_order))

    for store in preferred_order:
        deals = by_store.get(store, [])
        quota = V11_STORE_VERIFY_QUOTA.get(store, 1)

        for deal in deals[:quota]:
            ident = id(deal)
            if ident in candidate_ids:
                continue
            candidates.append(deal)
            candidate_ids.add(ident)
            picked_by_store[store] = picked_by_store.get(store, 0) + 1

            if len(candidates) >= settings.max_verify_candidates:
                break

        if len(candidates) >= settings.max_verify_candidates:
            break

    if len(candidates) < settings.max_verify_candidates:
        remainder = []
        for store, deals in by_store.items():
            for deal in deals:
                if id(deal) in candidate_ids:
                    continue
                signal = price_signals.get(signal_key(deal), {})
                if (
                    _v11_store_key(deal) == "kenzz"
                    and not _v11_independent_reference(signal)
                    and picked_by_store.get("kenzz", 0) >= 1
                ):
                    continue
                remainder.append(deal)

        remainder.sort(
            key=lambda d: (
                _v11_store_priority(d),
                _v7_fast_key(
                    d,
                    price_signals.get(signal_key(d), {}),
                ),
                verification_priority(d),
            ),
            reverse=True,
        )

        for deal in remainder:
            if len(candidates) >= settings.max_verify_candidates:
                break

            store = _v11_store_key(deal)

            # V11_NOON_VERIFY_ISOLATION_V1
            # Slow Noon verification must never starve healthy stores.
            if (
                store in {"noon", "noon_minutes"}
                and picked_by_store.get(store, 0)
                    >= V11_STORE_VERIFY_QUOTA.get(store, 1)
            ):
                continue

            candidates.append(deal)
            candidate_ids.add(id(deal))
            picked_by_store[store] = picked_by_store.get(store, 0) + 1

    log.info(
        "V11 PRIORITY VERIFY QUEUE | priority=amazon>noon>jumia>others>kenzz | picked=%s",
        picked_by_store,
    )

    verifier = DealVerifier(market)
    sent = 0
    sent_by_store = {}

    for deal in candidates:
        if sent >= settings.max_posts_per_cycle:
            break
        fp = save_seen(deal)
        if not should_review(fp, deal):
            continue

        signal = price_signals.get(signal_key(deal), {})
        if _v7_fast_tier(deal, signal) >= 2:
            live_status = await _v7_multistore_live_recheck(deal)
            if live_status is False:
                log.warning(
                    "V7 EXTREME RECHECK REJECTED | MAIN | %s | %s",
                    deal.store,
                    deal.title,
                )
                continue

        store_key = _v11_store_key(deal)
        verify_timeout = (
            6
            if store_key in {"noon", "noon_minutes"}
            else 15
        )

        try:
            report = await asyncio.wait_for(
                verifier.verify(deal),
                timeout=verify_timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "V11 VERIFY TIMEOUT ISOLATED | store=%s | timeout=%ss | %s",
                store_key,
                verify_timeout,
                deal.title,
            )

            class _TimeoutReport:
                # External comparison timed out.
                # This object must still satisfy ComparisonReport consumers.
                verified = False
                verdict = "DIRECT STORE CHECK"
                matches = []
                best_competitor_price = None
                market_advantage_percent = None
                history_count = 0
                historical_low = None
                historical_average = None
                reason = "External comparison timed out; direct-store evidence may still verify this deal."
                timeout = True

            report = _TimeoutReport()

        direct_verified = direct_store_evidence(deal)
        intel_verified = signal_verified(signal)

        # If external comparison timed out but the store page itself
        # provides valid deal evidence, keep the deal eligible for review.
        if getattr(report, "timeout", False) and direct_verified:
            report.verdict = "DIRECT STORE VERIFIED"
            report.reason = (
                "Verified from the store's own current price/deal evidence; "
                "external comparison timed out."
            )
        verified_now = (
            bool(getattr(report, "verified", False))
            or direct_verified
            or intel_verified
        )

        if settings.strict_verification and not verified_now:
            log.info(
                "UNVERIFIED rejected: %s | %s",
                deal.title,
                getattr(report, "reason", "no verification evidence"),
            )
            continue

        if direct_verified and not bool(getattr(report, "verified", False)):
            log.info(
                "DIRECT STORE VERIFIED: %s | %.1f%%",
                deal.title,
                _safe_num(getattr(deal, "discount_percent", 0)),
            )

        if intel_verified and not direct_verified:
            log.info(
                "PRICE INTELLIGENCE VERIFIED: %s | effective=%.1f%% | source=%s",
                deal.title,
                float(signal.get("effective_discount_percent") or 0),
                signal.get("reference_source"),
            )

        try:
            if _direct_flash_review_enabled():
                normal_truth = {
                    "class": "VERIFIED_REVIEW",
                    "route": "PRIVATE_REVIEW",
                    "score": 70,
                    "reason": "verified multistore review",
                }
                result = await _send_direct_flash_review(
                    deal, fp, signal, normal_truth
                )
                review_mode = "DIRECT IMAGE"
            else:
                result = await send_cloud_review(deal, fp, report, signal)
                review_mode = "CLOUD"

            cloud_result = (result.get("results") or [{}])[0]

            if cloud_result.get("ok"):
                log.info("%s review sent: %s", review_mode, deal.title)
                sent += 1
                store_key = str(getattr(deal, "store", "unknown")).lower()
                sent_by_store[store_key] = sent_by_store.get(store_key, 0) + 1
            else:
                log.warning(
                    "%s rejected: %s | %s",
                    review_mode,
                    deal.title,
                    cloud_result
                )

        except Exception as exc:
            log.exception("Review dispatch failed for %s: %s", deal.title, exc)

        try:
            _signal_for_delay = price_signals.get(
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

    log.info("V7 STORE HEALTH | SENT=%s", sent_by_store)
    log.info("Cycle complete: %d VERIFIED offers sent for review", sent)
    return sent

def _deal_from_payload(payload):
    data = json.loads(payload)
    data.pop("discount_percent", None)
    data.pop("_review_meta", None)
    return Deal(**data)

async def handle_callback(cb):
    cb_id = cb.get("id")
    from_user = cb.get("from", {})
    message = cb.get("message", {})
    data = cb.get("data", "")

    if int(from_user.get("id", 0)) != settings.admin_chat_id:
        await answer_callback(
            settings.telegram_bot_token,
            cb_id,
            "غير مصرح",
            True,
        )
        return

    if ":" not in data:
        return

    action, short_fp = data.split(":", 1)

    # Amazon V8 review callbacks.
    # Probe the Amazon review DB first so V11's own "u:" action
    # continues to work normally when the ID is not an Amazon deal.
    if action in ("u", "n", "x"):
        proc = await asyncio.create_subprocess_exec(
            os.path.join(AMAZON_READY_HOME, ".venv", "bin", "python"),
            os.path.join(AMAZON_READY_HOME, "amazon_deals_bot_ready", "amazon_callback_action.py"),
            action,
            short_fp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        out, err = await proc.communicate()

        if proc.returncode == 0:
            msg = {
                "u": "تم نشر عرض Amazon عاجل 🚀",
                "n": "تم نشر عرض Amazon 📢",
                "x": "تم رفض عرض Amazon ❌",
            }[action]

            await answer_callback(
                settings.telegram_bot_token,
                cb_id,
                msg,
            )
            return

        # Exit 3 = not an Amazon V8 deal.
        # Continue with the normal V11 moderation handler.
        if proc.returncode != 3:
            await answer_callback(
                settings.telegram_bot_token,
                cb_id,
                "حدث خطأ أثناء معالجة عرض Amazon",
                True,
            )
            return

    row = get_pending_by_short(short_fp)

    if not row:
        await answer_callback(
            settings.telegram_bot_token,
            cb_id,
            "العرض غير موجود",
            True,
        )
        return

    if row["status"] != "pending":
        await answer_callback(
            settings.telegram_bot_token,
            cb_id,
            "تم التعامل مع العرض بالفعل",
        )
        return

    deal = _deal_from_payload(row["payload"])
    review_meta = _pending_review_meta(row)
    fp = row["fingerprint"]

    if action == "r":
        set_pending_status(fp, "rejected")
        await answer_callback(
            settings.telegram_bot_token,
            cb_id,
            "تم رفض العرض ❌",
        )

    elif action == "u":
        price_verified = bool(
            (review_meta.get("evidence") or {}).get(
                "price_verified"
            )
        )

        # Urgent means zero extra store requests at click time.
        # If the incoming Flash Review did not already confirm the live
        # product price, do not risk a new request or publish blindly.
        if not price_verified:
            await answer_callback(
                settings.telegram_bot_token,
                cb_id,
                (
                    "السعر لم يتم تأكيده من صفحة المنتج بعد. "
                    "لن أرسل طلبًا إضافيًا للمتجر الآن — "
                    "افتح المنتج أو استخدم النشر العادي."
                ),
                True,
            )
            return

        text = (
            review_meta.get("urgent_post_text")
            or build_message(deal)
        )
        await send_custom_post(
            settings.telegram_bot_token,
            settings.telegram_channel_id,
            deal,
            text,
            review_meta.get("image_url"),
            review_meta.get("media_path"),
        )
        mark_posted(fp, deal)
        set_pending_status(fp, "posted_urgent")
        await answer_callback(
            settings.telegram_bot_token,
            cb_id,
            "تم النشر العاجل 🚀",
        )

    elif action in ("p", "f"):
        report = await DealVerifier([]).verify(deal)

        if settings.strict_verification and not report.verified:
            await answer_callback(
                settings.telegram_bot_token,
                cb_id,
                (
                    "العرض لم يعد يحقق شروط التحقق، "
                    "لذلك لم يتم نشره."
                ),
                True,
            )
            return

        low = historical_low(fp)

        if review_meta:
            text = (
                build_featured_message(deal, is_historical_low=(low is None or deal.current_price <= low))
                if action == "f"
                else (review_meta.get("normal_post_text") or build_message(deal))
            )
            await send_custom_post(
                settings.telegram_bot_token,
                settings.telegram_channel_id,
                deal,
                text,
                review_meta.get("image_url"),
                review_meta.get("media_path"),
            )
        else:
            await send_deal(
                settings.telegram_bot_token,
                settings.telegram_channel_id,
                deal,
                is_historical_low=(
                    low is None
                    or deal.current_price <= low
                ),
                featured=(action == "f"),
            )

        mark_posted(fp, deal)
        set_pending_status(fp, "posted")
        await answer_callback(
            settings.telegram_bot_token,
            cb_id,
            (
                "تم النشر المميز ⭐"
                if action == "f"
                else "تم النشر العادي 📢"
            ),
        )
    else:
        return

    try:
        await clear_review_buttons(
            settings.telegram_bot_token,
            int(message["chat"]["id"]),
            int(message["message_id"]),
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
