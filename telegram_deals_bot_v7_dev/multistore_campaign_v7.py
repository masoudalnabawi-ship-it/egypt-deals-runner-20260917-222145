
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

STATE_FILE = Path(".multistore_campaign_v7.json")

CAMPAIGN_WORDS = (
    "deal", "deals", "offer", "offers", "discount", "discounts",
    "promo", "promotion", "promotions", "sale", "flash",
    "campaign", "collection", "clearance", "coupon",
    "عرض", "عروض", "خصم", "خصومات", "تخفيض", "تخفيضات",
    "توفير", "وفر", "حملة",
)

NOON_EXTRA = [
    "https://www.noon.com/egypt-en/daily-deals-eg/",
    "https://www.noon.com/egypt-en/eg-homepage-megadeals/",
    "https://www.noon.com/egypt-en/home-deals/",
]


def _load():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _urls_from(value):
    out = []
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        out.append(value)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            out.extend(_urls_from(item))
    return out


def connector_seed_urls(name, connector):
    urls = []
    for attr in (
        "DEAL_URLS", "SEEDS", "DEFAULT_CATEGORIES",
        "URL", "HOME", "CATALOG", "BASE",
    ):
        urls.extend(_urls_from(getattr(connector, attr, None)))

    if str(name).lower() == "noon":
        urls.extend(NOON_EXTRA)

    if str(name).lower() == "dream2000":
        urls = []

    clean, seen = [], set()
    for url in urls:
        url = str(url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        clean.append(url)
    return clean[:60]


def next_campaign_url(name, connector):
    name = str(name).lower()
    state = _load()
    row = state.setdefault(name, {"cursor": 0, "discovered": []})

    seeds = connector_seed_urls(name, connector)
    discovered = [
        x for x in row.get("discovered", [])
        if isinstance(x, str) and x.startswith(("http://", "https://"))
    ]

    urls, seen = [], set()
    for url in discovered + seeds:
        if url not in seen:
            seen.add(url)
            urls.append(url)

    if not urls:
        return None

    cursor = int(row.get("cursor", 0) or 0) % len(urls)
    chosen = urls[cursor]
    row["cursor"] = (cursor + 1) % len(urls)
    row["discovered"] = discovered[-120:]
    state[name] = row
    _save(state)
    return chosen


def discover_campaign_links(name, html_or_soup, page_url):
    soup = (
        BeautifulSoup(html_or_soup, "html.parser")
        if isinstance(html_or_soup, str)
        else html_or_soup
    )
    if soup is None:
        return []

    try:
        base_host = urlsplit(page_url).netloc.lower()
    except Exception:
        base_host = ""

    out, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        full = urljoin(page_url, href)

        try:
            parts = urlsplit(full)
        except Exception:
            continue

        if parts.scheme not in ("http", "https"):
            continue
        if base_host and parts.netloc.lower() != base_host:
            continue

        text = " ".join(a.stripped_strings).lower()
        low = full.lower()
        hay = text + " " + low

        if not any(word in hay for word in CAMPAIGN_WORDS):
            continue

        if any(x in low for x in (
            "/cart", "/checkout", "/login", "/account",
            "/wishlist", "/help", "/customer",
        )):
            continue

        if full not in seen:
            seen.add(full)
            out.append(full)

    return out[:40]


def remember_campaign_links(name, links):
    name = str(name).lower()
    state = _load()
    row = state.setdefault(name, {"cursor": 0, "discovered": []})

    current = [x for x in row.get("discovered", []) if isinstance(x, str)]
    seen = set(current)

    for link in links:
        if link not in seen:
            seen.add(link)
            current.append(link)

    row["discovered"] = current[-120:]
    state[name] = row
    _save(state)


def capability_label(name, connector):
    name = str(name).lower()
    seeds = connector_seed_urls(name, connector)
    observations = hasattr(connector, "fetch_observations")
    search = hasattr(connector, "search_products") or hasattr(connector, "search")

    if name == "dream2000":
        campaign = "FULL_CATALOG"
    elif seeds:
        campaign = "CAMPAIGN_ROTATION"
    else:
        campaign = "BASE_ONLY"

    return {
        "observations": observations,
        "search": search,
        "campaign": campaign,
        "seed_count": len(seeds),
    }
