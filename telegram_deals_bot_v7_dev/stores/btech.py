import json

from models import Deal
from stores.base import StoreConnector


class BtechConnector(StoreConnector):
    name = "btech"

    @staticmethod
    def _find_image(obj):
        from urllib.parse import urljoin

        def normalize(value):
            if not isinstance(value, str):
                return None

            value = value.strip()
            low = value.lower()

            if not value:
                return None

            looks_image = (
                any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif"))
                or "image" in low
                or "/media/" in low
                or "/cdn/" in low
            )

            if not looks_image:
                return None

            if value.startswith("//"):
                return "https:" + value

            return urljoin("https://btech.com/", value)

        preferred = (
            "image_url", "imageUrl", "image", "thumbnail",
            "thumbnail_url", "thumbnailUrl", "main_image",
            "mainImage", "src", "url"
        )

        if isinstance(obj, dict):
            for key in preferred:
                if key not in obj:
                    continue

                value = obj.get(key)
                direct = normalize(value)
                if direct:
                    return direct

                if isinstance(value, (dict, list)):
                    nested = BtechConnector._find_image(value)
                    if nested:
                        return nested

            for value in obj.values():
                nested = BtechConnector._find_image(value)
                if nested:
                    return nested

        elif isinstance(obj, list):
            for value in obj[:30]:
                nested = BtechConnector._find_image(value)
                if nested:
                    return nested

        elif isinstance(obj, str):
            return normalize(obj)

        return None

    URL = (
        "https://btech.com/ar/collection/"
        "91342e36-259c-4ba7-b204-fbfbb217e5d1"
    )

    def _extract_items(self, soup):
        items = []
        decoder = json.JSONDecoder()

        for script in soup.find_all("script"):
            raw = script.string or script.get_text("", strip=False)

            if not raw or "self.__next_f.push" not in raw:
                continue

            prefix = "self.__next_f.push("
            start = raw.find(prefix)

            if start == -1:
                continue

            payload = raw[start + len(prefix):]

            if payload.endswith(")"):
                payload = payload[:-1]

            if payload.endswith(";"):
                payload = payload[:-1]

            try:
                outer = json.loads(payload)
            except Exception:
                continue

            for part in outer:
                if not isinstance(part, str):
                    continue

                pos = 0

                while True:
                    marker = '"items":'
                    idx = part.find(marker, pos)

                    if idx == -1:
                        break

                    arr_start = idx + len(marker)

                    try:
                        value, consumed = decoder.raw_decode(
                            part[arr_start:]
                        )
                    except Exception:
                        pos = arr_start
                        continue

                    if isinstance(value, list):
                        for obj in value:
                            if isinstance(obj, dict):
                                items.append(obj)

                    pos = arr_start + consumed

        return items

    def _make_deal(self, item, discounts_only=True):
        title = item.get("name")
        slug = item.get("slug")

        price = item.get("price") or {}

        try:
            current = float(price.get("final_price") or 0)
        except Exception:
            return None

        if not title or not slug or current <= 0:
            return None

        old_candidates = []

        for key in (
            "base_price",
            "final_without_coupon"
        ):
            try:
                value = float(price.get(key) or 0)

                if value > current:
                    old_candidates.append(value)
            except Exception:
                pass

        try:
            drop = float(item.get("price_drop_amount") or 0)

            if drop > 0:
                old_candidates.append(current + drop)
        except Exception:
            pass

        old_price = max(old_candidates) if old_candidates else None

        # Normal catalogue items are useful for price history.
        if old_price is not None and old_price <= current:
            old_price = None

        if discounts_only and not old_price:
            return None

        return Deal(
            store=self.name,
            title=str(title),
            current_price=current,
            old_price=old_price,
            url="https://btech.com/ar/p/" + str(slug),
            image_url=self._find_image(item),
        )


    async def fetch_observations(self):
        soup = await self.get_soup(self.URL)
        raw_items = self._extract_items(soup)

        deals = []
        seen = set()

        for item in raw_items:
            deal = self._make_deal(item, discounts_only=False)
            if not deal:
                continue

            key = (
                deal.title.lower().strip(),
                deal.current_price,
                deal.url,
            )
            if key in seen:
                continue

            seen.add(key)
            deals.append(deal)

        return deals

    async def fetch_deals(self):
        soup = await self.get_soup(self.URL)

        raw_items = self._extract_items(soup)

        deals = []
        seen = set()

        for item in raw_items:
            deal = self._make_deal(item)

            if not deal:
                continue

            key = (
                deal.title.lower().strip(),
                deal.current_price,
                deal.url
            )

            if key in seen:
                continue

            seen.add(key)
            deals.append(deal)

        return deals
