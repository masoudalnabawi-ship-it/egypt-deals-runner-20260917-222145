from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DB = ROOT / "competitor_intel.db"
QUEUE = ROOT / "competitor_signal_queue.jsonl"

CHANNELS = [
    "Belnos",
    "Yo_Ayman",
    "yahiaashry1",
    "deals_me",
    "Sal7lyEgypt",
]

BASE_INTERVAL = 75
JITTER = 18
REQUEST_TIMEOUT = 16

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

STORE_HOSTS = {
    "amazon": ("amazon.eg", "amzn."),
    "noon": ("noon.com",),
    "jumia": ("jumia.com.eg",),
    "2b": ("2b.com.eg",),
    "btech": ("btech.com",),
    "raya": ("rayashop.com",),
    "dream2000": ("dream2000.com",),
    "carrefour": ("carrefouregypt.com",),
    "raneen": ("raneen.com",),
    "kenzz": ("kenzz.com",),
}

STORE_WORDS = {
    "amazon": ("amazon", "أمازون", "امازون"),
    "noon": ("noon", "نون"),
    "jumia": ("jumia", "جوميا"),
    "2b": ("2b", "تو بي"),
    "btech": ("btech", "بي تك", "بي.تك"),
    "raya": ("raya", "راية"),
    "dream2000": ("dream2000", "دريم 2000", "دريم٢٠٠٠"),
    "carrefour": ("carrefour", "كارفور"),
    "raneen": ("raneen", "رنين"),
    "kenzz": ("kenzz", "كنز"),
}

ASIN_RE = re.compile(r"(?i)(?:/dp/|/gp/product/|asin[\s:=\-]*)([A-Z0-9]{10})(?:[/?&#\s]|$)")
MODEL_RE = re.compile(r"\b(?=[A-Z0-9._/-]*[A-Z])(?=[A-Z0-9._/-]*\d)[A-Z0-9][A-Z0-9._/-]{2,}\b", re.I)
COUPON_RE = re.compile(
    r"(?i)(?:كود|كوبون|coupon|promo\s*code|code)\s*[:：=\-]?\s*([A-Z0-9][A-Z0-9_-]{2,19})"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | competitor-intel | %(message)s",
)
log = logging.getLogger("competitor-intel")


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS competitor_posts (
                channel TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                posted_at TEXT,
                observed_at TEXT NOT NULL,
                permalink TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                stores_json TEXT NOT NULL,
                asins_json TEXT NOT NULL,
                urls_json TEXT NOT NULL,
                coupons_json TEXT NOT NULL,
                models_json TEXT NOT NULL,
                PRIMARY KEY(channel, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_comp_posts_time
                ON competitor_posts(posted_at);

            CREATE TABLE IF NOT EXISTS competitor_signals (
                signal_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                first_channel TEXT NOT NULL,
                first_post_id INTEGER NOT NULL,
                first_posted_at TEXT,
                first_observed_at TEXT NOT NULL,
                permalink TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_state (
                channel TEXT PRIMARY KEY,
                baseline_done INTEGER NOT NULL DEFAULT 0,
                last_ok_at TEXT,
                last_error TEXT
            );
            """
        )
        con.commit()


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if not host:
            return ""
        path = re.sub(r"/+$", "", p.path or "/")
        return urlunsplit(("https", host, path, "", ""))
    except Exception:
        return ""


def detect_store(text: str, urls: list[str]) -> list[str]:
    low = (text or "").lower()
    found = set()
    for store, words in STORE_WORDS.items():
        if any(w.lower() in low for w in words):
            found.add(store)
    for u in urls:
        host = (urlsplit(u).hostname or "").lower()
        for store, hosts in STORE_HOSTS.items():
            if any(h in host for h in hosts):
                found.add(store)
    return sorted(found)


def extract_signals(text: str, urls: list[str]):
    blob = (text or "") + "\n" + "\n".join(urls)
    asins = sorted(set(x.upper() for x in ASIN_RE.findall(blob)))

    product_urls = []
    for u in urls:
        cu = canonical_url(u)
        if not cu:
            continue
        host = (urlsplit(cu).hostname or "").lower()
        if any(any(h in host for h in hosts) for hosts in STORE_HOSTS.values()):
            product_urls.append(cu)
    product_urls = sorted(set(product_urls))

    coupons = sorted(set(x.upper() for x in COUPON_RE.findall(text or "")))
    models = []
    for x in MODEL_RE.findall((text or "").upper()):
        if len(x) <= 24 and x not in {"AMAZON", "NOON"}:
            models.append(x)
    models = sorted(set(models))[:12]

    stores = detect_store(text or "", product_urls)
    return stores, asins, product_urls, coupons, models


def signal_rows(asins, urls, coupons, models):
    rows = []
    rows += [("asin", x) for x in asins]
    rows += [("url", x) for x in urls]
    rows += [("coupon", x) for x in coupons]
    rows += [("model", x) for x in models]
    return rows


def sig_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def parse_page(channel: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        data_post = (msg.get("data-post") or "").strip()
        if "/" not in data_post:
            continue
        ch, raw_id = data_post.rsplit("/", 1)
        if ch.lower() != channel.lower():
            continue
        try:
            post_id = int(raw_id)
        except Exception:
            continue

        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""

        urls = []
        for a in msg.select("a[href]"):
            href = (a.get("href") or "").strip()
            if href.startswith("http://") or href.startswith("https://"):
                urls.append(href)

        t = msg.select_one("time[datetime]")
        posted_at = (t.get("datetime") or "").strip() if t else ""
        permalink = f"https://t.me/{channel}/{post_id}"

        stores, asins, product_urls, coupons, models = extract_signals(text, urls)
        out.append(
            {
                "channel": channel,
                "post_id": post_id,
                "posted_at": posted_at,
                "permalink": permalink,
                "text_hash": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(),
                "stores": stores,
                "asins": asins,
                "urls": product_urls,
                "coupons": coupons,
                "models": models,
            }
        )
    out.sort(key=lambda x: x["post_id"])
    return out


def get_state(channel):
    with connect() as con:
        row = con.execute(
            "SELECT baseline_done FROM channel_state WHERE channel=?",
            (channel,),
        ).fetchone()
        return bool(row and row["baseline_done"])


def mark_channel(channel, baseline_done=None, error=None):
    now = utcnow()
    with connect() as con:
        existing = con.execute(
            "SELECT baseline_done FROM channel_state WHERE channel=?",
            (channel,),
        ).fetchone()
        baseline_value = (
            int(bool(baseline_done))
            if baseline_done is not None
            else int(existing["baseline_done"]) if existing else 0
        )
        con.execute(
            """INSERT INTO channel_state(channel,baseline_done,last_ok_at,last_error)
               VALUES(?,?,?,?)
               ON CONFLICT(channel) DO UPDATE SET
                 baseline_done=excluded.baseline_done,
                 last_ok_at=excluded.last_ok_at,
                 last_error=excluded.last_error""",
            (
                channel,
                baseline_value,
                now if not error else None,
                error,
            ),
        )
        con.commit()


def insert_post(post, enqueue: bool):
    observed_at = utcnow()
    new_signals = []

    with connect() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO competitor_posts
               (channel,post_id,posted_at,observed_at,permalink,text_hash,
                stores_json,asins_json,urls_json,coupons_json,models_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                post["channel"],
                post["post_id"],
                post["posted_at"],
                observed_at,
                post["permalink"],
                post["text_hash"],
                json.dumps(post["stores"], ensure_ascii=False),
                json.dumps(post["asins"], ensure_ascii=False),
                json.dumps(post["urls"], ensure_ascii=False),
                json.dumps(post["coupons"], ensure_ascii=False),
                json.dumps(post["models"], ensure_ascii=False),
            ),
        )
        is_new = cur.rowcount > 0

        if is_new and enqueue:
            for kind, value in signal_rows(
                post["asins"], post["urls"], post["coupons"], post["models"]
            ):
                key = sig_key(kind, value)
                c2 = con.execute(
                    """INSERT OR IGNORE INTO competitor_signals
                       (signal_key,kind,value,first_channel,first_post_id,
                        first_posted_at,first_observed_at,permalink)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        kind,
                        value,
                        post["channel"],
                        post["post_id"],
                        post["posted_at"],
                        observed_at,
                        post["permalink"],
                    ),
                )
                if c2.rowcount > 0:
                    new_signals.append(
                        {
                            "signal_key": key,
                            "kind": kind,
                            "value": value,
                            "channel": post["channel"],
                            "post_id": post["post_id"],
                            "posted_at": post["posted_at"],
                            "observed_at": observed_at,
                            "permalink": post["permalink"],
                            "stores": post["stores"],
                        }
                    )
        con.commit()

    if new_signals:
        with QUEUE.open("a", encoding="utf-8") as f:
            for row in new_signals:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

    return is_new, new_signals


async def fetch_channel(client, channel):
    url = f"https://t.me/s/{channel}"
    r = await client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP_{r.status_code}")
    if len(r.text) < 500:
        raise RuntimeError("SHORT_PAGE")
    return parse_page(channel, r.text)


async def scan_channel(client, channel):
    try:
        posts = await fetch_channel(client, channel)
        baseline = get_state(channel)

        if not baseline:
            for post in posts:
                insert_post(post, enqueue=False)
            mark_channel(channel, baseline_done=True)
            log.info(
                "BASELINE READY | channel=%s | posts=%d | alerts=0",
                channel, len(posts),
            )
            return

        new_posts = 0
        new_signals = 0
        for post in posts:
            is_new, signals = insert_post(post, enqueue=True)
            if is_new:
                new_posts += 1
                new_signals += len(signals)

        mark_channel(channel)
        log.info(
            "COMPETITOR READY | channel=%s | visible=%d | new_posts=%d | new_signals=%d",
            channel, len(posts), new_posts, new_signals,
        )
    except Exception as exc:
        mark_channel(channel, error=f"{type(exc).__name__}:{str(exc)[:120]}")
        log.warning(
            "COMPETITOR DEGRADED | channel=%s | %s: %s",
            channel, type(exc).__name__, str(exc)[:120],
        )


async def run_once():
    init_db()
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=10.0)
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=timeout, follow_redirects=True
    ) as client:
        for channel in CHANNELS:
            await scan_channel(client, channel)
            await asyncio.sleep(random.uniform(0.8, 2.0))


async def run_forever():
    init_db()
    log.warning(
        "COMPETITOR INTELLIGENCE ON | channels=%s | interval~%ss | no_store_requests=1 | no_publish=1",
        ",".join(CHANNELS), BASE_INTERVAL,
    )
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=10.0)
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=timeout, follow_redirects=True
    ) as client:
        while True:
            started = time.monotonic()
            for channel in CHANNELS:
                await scan_channel(client, channel)
                await asyncio.sleep(random.uniform(1.0, 2.4))
            elapsed = time.monotonic() - started
            target = BASE_INTERVAL + random.uniform(-JITTER, JITTER)
            await asyncio.sleep(max(20.0, target - elapsed))


def self_test():
    sample = """
    <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="deals_me/123">
      <div class="tgme_widget_message_text">Amazon كود SAVE20 موديل ABC-123
      <a href="https://www.amazon.eg/dp/B01N0Z1YKE?tag=x">deal</a></div>
      <time datetime="2026-09-16T15:00:00+00:00"></time>
    </div>
    """
    rows = parse_page("deals_me", sample)
    assert len(rows) == 1
    r = rows[0]
    assert "amazon" in r["stores"]
    assert "B01N0Z1YKE" in r["asins"]
    assert "SAVE20" in r["coupons"]
    print("PASS competitor_intel self-test")


def report():
    init_db()
    with connect() as con:
        print("=== COMPETITOR INTELLIGENCE STATUS ===")
        rows = con.execute(
            """SELECT channel,baseline_done,last_ok_at,last_error
               FROM channel_state ORDER BY channel"""
        ).fetchall()
        for r in rows:
            print(
                f"{r['channel']:16} baseline={r['baseline_done']} "
                f"last_ok={r['last_ok_at'] or '-'} error={r['last_error'] or '-'}"
            )
        p = con.execute("SELECT COUNT(*) n FROM competitor_posts").fetchone()["n"]
        s = con.execute("SELECT COUNT(*) n FROM competitor_signals").fetchone()["n"]
        print(f"posts={p} signals={s}")
        print(f"queue_bytes={QUEUE.stat().st_size if QUEUE.exists() else 0}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    init_db()
    if args.self_test:
        self_test()
    elif args.report:
        report()
    elif args.once:
        asyncio.run(run_once())
    else:
        asyncio.run(run_forever())
