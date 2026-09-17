
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

ASIN_RE = re.compile(
    r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)",
    re.I,
)

CAMPAIGN_WORDS = (
    "deal", "deals", "offer", "offers", "discount", "discounts",
    "promotion", "promotions", "promo", "save", "coupon", "coupons",
    "goldbox", "عروض", "عرض", "خصم", "خصومات", "وفر", "توفير",
    "شاهد جميع", "عرض الكل", "see all", "shop all",
)


def _clean_url(url):
    try:
        parts = urlsplit(url)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, parts.query, "")
        )
    except Exception:
        return str(url or "")


def _amazon_url(base_url, href):
    full = urljoin(base_url, href or "")
    if "amazon.eg" not in full.lower():
        return ""
    return _clean_url(full)


def extract_campaign_targets(
    html,
    base_url="https://www.amazon.eg",
):
    soup = BeautifulSoup(html or "", "html.parser")

    products = {}
    campaign_links = []

    for node in soup.select("[data-asin]"):
        asin = str(node.get("data-asin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue

        title_node = node.select_one(
            "h2 span, h2, .a-size-base-plus, .a-text-normal"
        )
        title = (
            title_node.get_text(" ", strip=True)
            if title_node
            else ""
        )

        products[asin] = {
            "asin": asin,
            "title": title,
            "url": f"https://www.amazon.eg/dp/{asin}",
        }

    seen_links = set()

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        full = _amazon_url(base_url, href)
        if not full:
            continue

        m = ASIN_RE.search(full)
        if m:
            asin = m.group(1).upper()
            title = a.get_text(" ", strip=True)
            current = products.get(asin, {})

            products[asin] = {
                "asin": asin,
                "title": current.get("title") or title,
                "url": f"https://www.amazon.eg/dp/{asin}",
            }
            continue

        text = a.get_text(" ", strip=True).lower()
        low = full.lower()
        combined = text + " " + low

        if not any(
            word in combined
            for word in CAMPAIGN_WORDS
        ):
            continue

        if any(
            x in low
            for x in (
                "/gp/cart",
                "/hz/wishlist",
                "/gp/css",
                "/customer-preferences",
                "/help/",
                "/signin",
            )
        ):
            continue

        if full not in seen_links:
            seen_links.add(full)
            campaign_links.append(full)

    return {
        "products": list(products.values()),
        "campaign_links": campaign_links[:80],
    }
