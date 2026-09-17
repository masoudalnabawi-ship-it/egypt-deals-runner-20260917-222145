import json
from urllib.parse import urljoin

from models import Deal
from stores.base import StoreConnector, parse_price


class CarrefourConnector(StoreConnector):
    name = "carrefour"
    BASE = "https://www.carrefouregypt.com"

    SEEDS = [
        "https://www.carrefouregypt.com/mafegy/ar/c/FEGY1700000?isRR=true",
        "https://www.carrefouregypt.com/mafegy/ar/c/FEGY1760000",
        "https://www.carrefouregypt.com/mafegy/en/c/13269",
    ]

    @staticmethod
    def _number(value):
        if value is None:
            return None
        if isinstance(value, dict):
            for key in ("value", "amount", "price"):
                if key in value:
                    return CarrefourConnector._number(value.get(key))
            return None
        return parse_price(str(value))

    @staticmethod
    def _image_from_obj(obj):
        if not isinstance(obj, dict):
            return None

        for key in (
            "image", "imageUrl", "image_url", "thumbnail",
            "thumbnailUrl", "primaryImage", "media"
        ):
            value = obj.get(key)

            if isinstance(value, str) and value:
                return value if value.startswith("http") else urljoin(CarrefourConnector.BASE, value)

            if isinstance(value, dict):
                for sub in ("url", "src", "imageUrl"):
                    nested = value.get(sub)
                    if isinstance(nested, str) and nested:
                        return nested if nested.startswith("http") else urljoin(CarrefourConnector.BASE, nested)

            if isinstance(value, list):
                for item in value[:3]:
                    if isinstance(item, str) and item:
                        return item if item.startswith("http") else urljoin(CarrefourConnector.BASE, item)
                    if isinstance(item, dict):
                        for sub in ("url", "src"):
                            nested = item.get(sub)
                            if isinstance(nested, str) and nested:
                                return nested if nested.startswith("http") else urljoin(CarrefourConnector.BASE, nested)

        return None

    def _deal_from_obj(self, obj):
        if not isinstance(obj, dict):
            return None

        title = (
            obj.get("name")
            or obj.get("title")
            or obj.get("productName")
            or obj.get("product_name")
        )

        url = (
            obj.get("url")
            or obj.get("productUrl")
            or obj.get("product_url")
            or obj.get("slug")
        )

        current = self._number(
            obj.get("price")
            or obj.get("sellingPrice")
            or obj.get("selling_price")
            or obj.get("specialPrice")
            or obj.get("special_price")
            or obj.get("finalPrice")
        )

        old = self._number(
            obj.get("oldPrice")
            or obj.get("old_price")
            or obj.get("regularPrice")
            or obj.get("originalPrice")
            or obj.get("wasPrice")
            or obj.get("listPrice")
        )

        if not title or not current or current <= 0:
            return None

        if old is not None and old <= current:
            old = None

        full_url = (
            str(url)
            if url and str(url).startswith("http")
            else urljoin(self.BASE, str(url or "/"))
        )

        return Deal(
            store=self.name,
            title=str(title),
            current_price=current,
            old_price=old,
            url=full_url,
            image_url=self._image_from_obj(obj),
        )

    def _extract_json(self, obj, out):
        if isinstance(obj, dict):
            deal = self._deal_from_obj(obj)
            if deal:
                out.append(deal)

            for value in obj.values():
                self._extract_json(value, out)

        elif isinstance(obj, list):
            for value in obj:
                self._extract_json(value, out)

    def _parse_dom(self, soup):
        deals = []

        selectors = (
            "[data-testid*='product'], [class*='product-card'], "
            "[class*='ProductCard'], [class*='product_item'], "
            "[class*='productItem'], article"
        )

        for card in soup.select(selectors):
            link = card.select_one("a[href]")
            title_el = card.select_one(
                "h2, h3, [class*='name'], [class*='title'], "
                "[data-testid*='name']"
            )

            if not (link and title_el):
                continue

            price_nodes = card.select(
                "[data-testid*='price'], [class*='price'], [class*='Price']"
            )

            values = []
            for node in price_nodes:
                value = parse_price(node.get_text(" ", strip=True))
                if value and value > 0:
                    values.append(value)

            if not values:
                continue

            current = min(values)
            old = max(values) if len(values) > 1 and max(values) > current else None

            img = card.select_one("img")
            image = None
            if img:
                image = (
                    img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("src")
                )
                if image:
                    image = urljoin(self.BASE, image)

            deals.append(
                Deal(
                    store=self.name,
                    title=title_el.get_text(" ", strip=True),
                    current_price=current,
                    old_price=old,
                    url=urljoin(self.BASE, link.get("href")),
                    image_url=image,
                )
            )

        return deals

    def _parse_page(self, soup):
        deals = self._parse_dom(soup)

        for script in soup.select("script"):
            raw = script.string or script.get_text("", strip=True)
            if not raw or len(raw) < 20:
                continue

            script_type = script.get("type") or ""
            script_id = script.get("id") or ""

            if script_type in ("application/json", "application/ld+json") or "__NEXT_DATA__" in script_id:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                self._extract_json(data, deals)

        unique = []
        seen = set()

        for deal in deals:
            key = (
                deal.title.lower().strip(),
                round(deal.current_price, 2),
                deal.url,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(deal)

        return unique

    async def fetch_observations(self):
        all_deals = []
        seen = set()

        for url in self.SEEDS:
            try:
                soup = await self.get_soup(url)
            except Exception:
                continue

            for deal in self._parse_page(soup):
                key = (deal.url, round(deal.current_price, 2))
                if key not in seen:
                    seen.add(key)
                    all_deals.append(deal)

        return all_deals

    async def fetch_deals(self):
        observations = await self.fetch_observations()
        return [d for d in observations if d.discount_percent >= 5.0]
