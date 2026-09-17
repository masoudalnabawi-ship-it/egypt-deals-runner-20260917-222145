import time
import json
from pathlib import Path
from urllib.parse import urljoin, quote_plus
from models import Deal
from stores.base import StoreConnector, parse_price

class JumiaConnector(StoreConnector):
    name = "jumia"
    BASE = "https://www.jumia.com.eg/ar/flash-sales/"
    SEARCH = "https://www.jumia.com.eg/ar/catalog/?q={query}"
    CATALOG = "https://www.jumia.com.eg/ar/catalog/"
    CURSOR_FILE = Path(".jumia_cursor.json")

    def _parse_cards(self, soup) -> list[Deal]:
        deals, seen = [], set()
        for card in soup.select("article.prd, article"):
            title_el = card.select_one(".name, h3, .prd-name")
            current_el = card.select_one(".prc")
            old_el = card.select_one(".old")
            if not (title_el and current_el):
                continue

            product_url = None
            for a in card.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href or href == "#" or href.startswith("javascript:"):
                    continue
                if "customer/account/login" in href or "wishlist" in href:
                    continue
                if ".html" in href:
                    product_url = urljoin(self.BASE, href)
                    break
            if not product_url:
                continue

            current = parse_price(current_el.get_text(" ", strip=True))
            old = parse_price(old_el.get_text(" ", strip=True)) if old_el else None
            if not current:
                continue
            if old is not None and old <= current:
                old = None

            title = title_el.get_text(" ", strip=True)
            key = product_url.split("?")[0]
            if key in seen:
                continue
            seen.add(key)

            img = card.select_one("img")
            image = None
            if img:
                image = img.get("data-src") or img.get("data-lazy") or img.get("src")

            deals.append(Deal(
                store=self.name,
                title=title,
                current_price=current,
                old_price=old,
                url=product_url,
                image_url=image,
            ))
        return deals

    async def _fetch_pages(self, base_url, max_pages):
        all_deals = []
        seen_urls = set()

        for page in range(1, max_pages + 1):
            url = base_url if page == 1 else (
                base_url + ("&" if "?" in base_url else "?") + f"page={page}"
            )

            try:
                soup = await self.get_soup(url)
            except Exception:
                continue

            page_deals = self._parse_cards(soup)

            for deal in page_deals:
                key = deal.url.split("?")[0]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                all_deals.append(deal)

        return all_deals

    async def fetch_deals(self) -> list[Deal]:
        # V11_JUMIA_ROTATING_HOT_SCAN
        # Page 1 is always hot; deeper pages rotate to keep broad coverage.
        all_deals = []
        seen_urls = set()
        bucket = int(time.time() // 120)

        sources = list(getattr(self, "SOURCES", []) or [self.BASE])
        chosen_sources = [sources[0]]
        if len(sources) > 1:
            chosen_sources.append(sources[1 + (bucket % (len(sources) - 1))])

        for source_index, base in enumerate(chosen_sources):
            if source_index == 0:
                rotating = [
                    2 + ((bucket * 3 + i) % 29)
                    for i in range(3)
                ]
                pages = [1] + rotating
            else:
                pages = [1]

            for page in pages:
                url = base if page == 1 else f"{base}?page={page}"
                try:
                    soup = await self.get_soup(url)
                except Exception:
                    continue

                for d in self._parse_cards(soup):
                    key = d.url.split("?")[0]
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    all_deals.append(d)

        return all_deals

    async def search_products(self, query: str) -> list[Deal]:
        url = self.SEARCH.format(query=quote_plus(query))
        soup = await self.get_soup(url)
        return self._parse_cards(soup)[:30]
