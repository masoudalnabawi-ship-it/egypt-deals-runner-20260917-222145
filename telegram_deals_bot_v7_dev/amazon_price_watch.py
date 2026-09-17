import asyncio
import hashlib
import os
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from engine import run_store
from comparison import product_key
from stores.base import parse_price

load_dotenv(".env")

RAW_BASE = os.environ["CLOUD_API_URL"].rstrip("/")
if RAW_BASE.endswith("/api/deals"):
    BASE = RAW_BASE[:-len("/api/deals")]
else:
    BASE = RAW_BASE

API_KEY = os.environ["CLOUD_API_KEY"]

API_HEADERS = {
    "x-api-key": API_KEY,
    "Accept": "application/json",
    "User-Agent": "EgyptDealsAmazonWatch/2.0",
}

AMAZON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) "
        "AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


def num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


async def get_history(client, key):
    try:
        r = await client.get(
            BASE + "/api/history",
            params={"product_key": key},
            headers=API_HEADERS,
            timeout=20,
        )

        if r.status_code != 200:
            return {}

        data = r.json()

        if isinstance(data.get("history"), dict):
            data = data["history"]

        if isinstance(data.get("stats"), dict):
            data = data["stats"]

        return data

    except Exception:
        return {}


async def recheck_amazon_price(client, deal):
    """
    Open the exact Amazon product URL immediately before alerting.
    Returns latest visible price, or None if it cannot be safely verified.
    """

    try:
        r = await client.get(
            deal.url,
            headers=AMAZON_HEADERS,
            timeout=25,
            follow_redirects=True,
        )

        if r.status_code != 200:
            print(
                "LIVE RECHECK FAILED:",
                r.status_code,
                deal.title[:70],
                flush=True,
            )
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        availability = soup.select_one("#availability")

        if availability:
            availability_text = availability.get_text(
                " ",
                strip=True
            ).lower()

            unavailable_words = (
                "غير متوفر",
                "غير متاح",
                "currently unavailable",
                "temporarily out of stock",
            )

            if any(x in availability_text for x in unavailable_words):
                print(
                    "LIVE RECHECK: OUT OF STOCK:",
                    deal.title[:70],
                    flush=True,
                )
                return None

        selectors = [
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .priceToPay .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            "#apex_desktop .priceToPay .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            ".priceToPay .a-offscreen",
            "#price_inside_buybox",
            "#newBuyBoxPrice",
            "#priceblock_dealprice",
            "#priceblock_ourprice",
        ]

        for selector in selectors:
            el = soup.select_one(selector)

            if not el:
                continue

            p = parse_price(
                el.get_text(" ", strip=True)
            )

            if p and p > 0:
                return float(p)

        print(
            "LIVE RECHECK: PRICE NOT FOUND:",
            deal.title[:70],
            flush=True,
        )

        return None

    except Exception as e:
        print(
            "LIVE RECHECK ERROR:",
            repr(e),
            flush=True,
        )
        return None


async def sync_all_prices(client, deals):
    observations = []

    for d in deals:
        current = num(d.current_price)

        if current <= 0:
            continue

        observations.append({
            "product_key": product_key(d),
            "store": "amazon",
            "title": d.title,
            "current_price": current,
            "old_price": num(d.old_price) if d.old_price else None,
            "url": d.url,
        })

    inserted = 0

    for i in range(0, len(observations), 50):
        chunk = observations[i:i + 50]

        try:
            r = await client.post(
                BASE + "/api/observations",
                json={"observations": chunk},
                headers=API_HEADERS,
                timeout=30,
            )

            if r.status_code == 200:
                data = r.json()

                inserted += int(
                    data.get("inserted")
                    or data.get("received")
                    or len(chunk)
                )

        except Exception as e:
            print(
                "HISTORY SYNC ERROR:",
                repr(e),
                flush=True,
            )

    return inserted


def fingerprint(deal, current):
    raw = (
        "amazon-live|"
        + deal.url.split("?")[0]
        + "|"
        + str(round(current, 2))
    )

    return (
        "amazon-live-"
        + hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:32]
    )


async def send_alert(
    client,
    deal,
    current,
    reference,
    drop,
    saving,
    level,
):
    checked_at = datetime.now().strftime("%H:%M:%S")

    if level == "CRITICAL":
        heading = "🚨 هبوط سعر استثنائي على Amazon"
    else:
        heading = "⚡ هبوط قوي في سعر Amazon"

    reason = (
        f"{heading}\n"
        f"السعر الحالي: {current:,.2f} جنيه\n"
        f"السعر المرجعي من تاريخنا: {reference:,.2f} جنيه\n"
        f"التوفير التقريبي: {saving:,.2f} جنيه\n"
        f"الهبوط المحسوب: {drop:.1f}%\n"
        f"✅ تمت إعادة فتح صفحة Amazon والتحقق من السعر مباشرة "
        f"قبل الإرسال الساعة {checked_at}."
    )

    payload = {
        "fingerprint": fingerprint(deal, current),
        "store": "Amazon Egypt",
        "title": deal.title,
        "url": deal.url,
        "current_price": current,

        # Amazon may not display an old price.
        "old_price": None,

        "discount_percent": round(drop, 1),
        "saving": round(saving, 2),

        "verified": True,
        "verification_reason": reason,
        "comparison_report": reason,
        "reason": reason,

        "priority": level.lower(),
        "price_reference": "stored_market_history",
        "live_rechecked": True,
        "live_checked_at": checked_at,
    }

    r = await client.post(
        BASE + "/api/deals",
        json=payload,
        headers=API_HEADERS,
        timeout=30,
    )

    print(
        f"{level} ALERT SENT:",
        deal.title[:80],
        "| LIVE PRICE:",
        current,
        "| REF:",
        round(reference, 2),
        "| DROP:",
        round(drop, 1),
        "%",
        "| API:",
        r.status_code,
        flush=True,
    )

    return r.status_code == 200


async def main():
    print(
        "\nAMAZON PRICE WATCH:",
        datetime.now().isoformat(timespec="seconds"),
        flush=True,
    )

    deals = await run_store("amazon")

    print(
        "AMAZON PRODUCTS FOUND =",
        len(deals),
        flush=True,
    )

    alerts = 0
    rechecks = 0

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
    ) as client:

        # Check historical price BEFORE saving this cycle.
        for d in deals:
            detected_price = num(d.current_price)

            if detected_price <= 0:
                continue

            stats = await get_history(
                client,
                product_key(d)
            )

            count = int(stats.get("count") or 0)
            reference = num(stats.get("avg_price"))
            previous_min = num(stats.get("min_price"))

            if count < 1 or reference <= 0:
                continue

            saving = reference - detected_price

            if saving <= 0:
                continue

            drop = (saving / reference) * 100

            critical = (
                detected_price <= reference * 0.50
                and saving >= 300
            )

            strong = (
                count >= 2
                and detected_price <= reference * 0.75
                and saving >= 300
                and (
                    previous_min <= 0
                    or detected_price < previous_min * 0.90
                )
            )

            if not critical and not strong:
                continue

            # Important: verify exact page price immediately.
            rechecks += 1

            live_price = await recheck_amazon_price(
                client,
                d
            )

            if live_price is None:
                continue

            # Price must still be close to or lower than detected price.
            # If Amazon changed it upwards significantly, don't alert.
            if live_price > detected_price * 1.05:
                print(
                    "ALERT CANCELLED - PRICE CHANGED:",
                    d.title[:75],
                    "| detected:",
                    detected_price,
                    "| live:",
                    live_price,
                    flush=True,
                )
                continue

            # Recalculate everything using live price.
            live_saving = reference - live_price

            if live_saving <= 0:
                continue

            live_drop = (
                live_saving / reference
            ) * 100

            live_critical = (
                live_price <= reference * 0.50
                and live_saving >= 300
            )

            live_strong = (
                count >= 2
                and live_price <= reference * 0.75
                and live_saving >= 300
            )

            if not live_critical and not live_strong:
                print(
                    "ALERT CANCELLED - DROP NO LONGER VALID:",
                    d.title[:75],
                    flush=True,
                )
                continue

            level = (
                "CRITICAL"
                if live_critical
                else "STRONG"
            )

            if await send_alert(
                client,
                d,
                live_price,
                reference,
                live_drop,
                live_saving,
                level,
            ):
                alerts += 1

        # Save all currently observed Amazon prices AFTER comparisons.
        inserted = await sync_all_prices(
            client,
            deals
        )

    print(
        "LIVE PRODUCT RECHECKS =",
        rechecks,
        flush=True,
    )

    print(
        "ALL AMAZON PRICES SYNCED =",
        inserted,
        flush=True,
    )

    print(
        "AMAZON HISTORY ALERTS =",
        alerts,
        flush=True,
    )


asyncio.run(main())
