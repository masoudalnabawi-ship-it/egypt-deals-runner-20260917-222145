from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from models import Deal
from stores.base import StoreConnector


class NoonMinutesConnector(StoreConnector):
    name = "noon_minutes"
    BASE = "https://minutes.noon.com"
    HOME = "https://minutes.noon.com/egypt-en/"
    CURSOR_FILE = Path(".noon_minutes_cursor.json")

    DEFAULT_CATEGORIES = [
        "https://minutes.noon.com/egypt-en/category/dairy_eggs/?f%5Bcategory%5D=auto_deal",
        "https://minutes.noon.com/egypt-en/category/eg_beverages/?f%5Bcategory%5D=auto_deal",
    ]

    @staticmethod
    def _num(value):
        try:
            return float(str(value).replace(",", "").strip())
        except Exception:
            return None

    @staticmethod
    def _discount(text):
        m = re.search(r"(\d{1,3})\s*%\s*OFF", text, re.I)
        if not m:
            return None
        try:
            value = float(m.group(1))
        except Exception:
            return None
        return value if 0 < value < 100 else None

    @classmethod
    def _price_pair(cls, text):
        text = re.sub(r"\s+", " ", text)
        m = re.search(r"\bEGP\s*([\d,]+(?:\.\d+)?)", text, re.I)
        if not m:
            return None, None

        current = cls._num(m.group(1))
        if not current:
            return None, None

        tail = text[m.end():m.end() + 50]
        old = None
        m2 = re.search(r"^\s*(?:EGP\s*)?([\d,]+(?:\.\d+)?)\b", tail, re.I)
        if m2:
            candidate = cls._num(m2.group(1))
            if candidate and candidate > current:
                old = candidate

        return current, old

    @staticmethod
    def _image(card):
        img = card.select_one("img")
        if not img:
            return None
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        return urljoin(NoonMinutesConnector.BASE, src) if src else None

    @staticmethod
    def _clean_title(text):
        text = re.sub(r"\b\d{1,3}%\s*OFF\b", " ", text, flags=re.I)
        text = re.sub(r"\bEGP\s*[\d,.]+", " ", text, flags=re.I)
        text = re.sub(r"\b(Add|Chilled|Frozen)\b", " ", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip(" -|")[:220]

    def _parse_page(self, soup):
        deals = []
        seen = set()

        for a in soup.select('a[href*="/now-product/"]'):
            href = (a.get("href") or "").strip()
            if not href:
                continue

            url = urljoin(self.BASE, href)
            if url in seen:
                continue

            text = re.sub(r"\s+", " ", " ".join(a.stripped_strings)).strip()
            current, old = self._price_pair(text)
            badge = self._discount(text)

            if not current:
                continue

            # Discovery fallback only when Minutes visibly states a discount.
            if old is None and badge and badge >= 5:
                old = round(current / (1.0 - badge / 100.0), 2)

            title = None
            img = a.select_one("img")
            if img:
                title = img.get("alt") or img.get("title")

            if not title:
                title_el = a.select_one("h2, h3, h4, [class*='title'], [class*='name']")
                if title_el:
                    title = title_el.get_text(" ", strip=True)

            if not title:
                title = self._clean_title(text)

            if not title or len(title) < 3:
                continue

            deals.append(
                Deal(
                    store=self.name,
                    title=title,
                    current_price=current,
                    old_price=old if old and old > current else None,
                    url=url,
                    image_url=self._image(a),
                )
            )
            seen.add(url)

        # Next.js JSON can expose additional products/images.
        for script in soup.select("script"):
            if (script.get("id") or "") != "__NEXT_DATA__":
                continue
            raw = script.string or script.get_text("", strip=True)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            self._walk_json(data, deals, seen)

        return deals

    def _walk_json(self, obj, deals, seen):
        if isinstance(obj, dict):
            title = obj.get("name") or obj.get("title") or obj.get("productName")
            url = obj.get("relativeUrl") or obj.get("url") or obj.get("productUrl")
            price = obj.get("price") or obj.get("salePrice") or obj.get("offerPrice")
            old = obj.get("oldPrice") or obj.get("regularPrice") or obj.get("originalPrice")

            current = self._num(price)
            old_price = self._num(old)

            if title and url and current and current > 0:
                full = urljoin(self.BASE, str(url))
                if "/now-product/" in full and full not in seen:
                    image = None
                    for key in ("imageUrl", "image_url", "image", "thumbnail"):
                        value = obj.get(key)
                        if isinstance(value, str) and value:
                            image = urljoin(self.BASE, value)
                            break
                    deals.append(
                        Deal(
                            store=self.name,
                            title=str(title),
                            current_price=current,
                            old_price=old_price if old_price and old_price > current else None,
                            url=full,
                            image_url=image,
                        )
                    )
                    seen.add(full)

            for value in obj.values():
                self._walk_json(value, deals, seen)

        elif isinstance(obj, list):
            for value in obj:
                self._walk_json(value, deals, seen)

    def _discover_categories(self, soup):
        urls = []
        seen = set()

        for a in soup.select('a[href*="/egypt-en/category/"]'):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = urljoin(self.BASE, href)
            if url in seen:
                continue
            seen.add(url)

            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query.setdefault("f[category]", "auto_deal")
            urls.append(
                urlunsplit((
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(query),
                    parts.fragment,
                ))
            )

        return urls[:30]

    def _load_cursor(self):
        try:
            return int(json.loads(self.CURSOR_FILE.read_text()).get("i", 0))
        except Exception:
            return 0

    def _save_cursor(self, value):
        try:
            self.CURSOR_FILE.write_text(json.dumps({"i": int(value)}))
        except Exception:
            pass

    async def fetch_observations(self):
        home = await self.get_soup(self.HOME)
        deals = self._parse_page(home)

        categories = self._discover_categories(home) or list(self.DEFAULT_CATEGORIES)
        cursor = self._load_cursor() % max(1, len(categories))

        selected = [categories[(cursor + i) % len(categories)] for i in range(min(4, len(categories)))]
        self._save_cursor((cursor + len(selected)) % max(1, len(categories)))

        for url in selected:
            try:
                soup = await asyncio.wait_for(self.get_soup(url), timeout=16)
            except Exception:
                continue
            deals.extend(self._parse_page(soup))

        unique = []
        seen = set()
        for deal in deals:
            key = (deal.url, round(deal.current_price, 2))
            if key in seen:
                continue
            seen.add(key)
            unique.append(deal)
        return unique

    async def fetch_deals(self):
        return [d for d in await self.fetch_observations() if d.discount_percent >= 5.0]

    async def search_products(self, query):
        deals = await self.fetch_observations()
        terms = [x for x in re.split(r"\s+", str(query).lower()) if len(x) >= 2]
        scored = []
        for deal in deals:
            title = deal.title.lower()
            score = sum(term in title for term in terms)
            if score:
                scored.append((score, deal))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:30]]
