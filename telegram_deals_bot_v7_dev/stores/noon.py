import json
from urllib.parse import quote_plus, urljoin

from models import Deal
from stores.base import StoreConnector, parse_price


class NoonConnector(StoreConnector):
    name = "noon"

    # NOON_PROXY_FIRST_V1
    # Direct Noon access repeatedly times out in this environment.
    prefer_cloud_proxy = True

    DEAL_URLS = [
        "https://www.noon.com/egypt-en/all-products/?sort[by]=discount&sort[dir]=desc",
        "https://www.noon.com/egypt-en/daily-deals-eg/",
        "https://www.noon.com/egypt-en/eg-homepage-megadeals/",
        "https://www.noon.com/egypt-en/home-deals/",
        "https://www.noon.com/egypt-en/",
    ]

    @staticmethod
    def _number(value):
        if value is None:
            return None
        if isinstance(value, dict):
            for key in ("value", "amount", "price", "min"):
                if key in value:
                    return NoonConnector._number(value.get(key))
            return None
        try:
            return float(str(value).replace(",", "").replace("EGP", "").strip())
        except Exception:
            return None

    @staticmethod
    def _image(obj):
        if not isinstance(obj, dict):
            return None
        for key in (
            "image_url", "imageUrl", "image", "thumbnail",
            "thumbnailUrl", "primaryImage", "primary_image"
        ):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value if value.startswith("http") else urljoin("https://www.noon.com", value)
            if isinstance(value, dict):
                for sub in ("url", "src"):
                    v = value.get(sub)
                    if isinstance(v, str) and v:
                        return v if v.startswith("http") else urljoin("https://www.noon.com", v)
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict):
                    for sub in ("url", "src"):
                        v = first.get(sub)
                        if isinstance(v, str):
                            return v
        return None

    def _extract_from_json(self, obj, out):
        if isinstance(obj, dict):
            title = (
                obj.get("name")
                or obj.get("title")
                or obj.get("product_name")
                or obj.get("productName")
            )
            url = (
                obj.get("url")
                or obj.get("productUrl")
                or obj.get("url_key")
                or obj.get("urlKey")
            )
            current = self._number(
                obj.get("sale_price")
                or obj.get("salePrice")
                or obj.get("offer_price")
                or obj.get("offerPrice")
                or obj.get("price")
                or obj.get("priceNow")
            )
            old = self._number(
                obj.get("old_price")
                or obj.get("oldPrice")
                or obj.get("was_price")
                or obj.get("regular_price")
                or obj.get("regularPrice")
                or obj.get("priceWas")
            )

            if title and current and current > 0:
                if old is not None and old <= current:
                    old = None
                full = (
                    str(url)
                    if url and str(url).startswith("http")
                    else urljoin("https://www.noon.com", str(url or "/egypt-en/"))
                )
                out.append(
                    Deal(
                        store=self.name,
                        title=str(title),
                        current_price=current,
                        old_price=old,
                        url=full,
                        image_url=self._image(obj),
                    )
                )

            for value in obj.values():
                self._extract_from_json(value, out)

        elif isinstance(obj, list):
            for value in obj:
                self._extract_from_json(value, out)

    def _parse_html_cards(self, soup):
        deals = []
        selectors = (
            "[data-qa='product-box'], [class*='ProductBox'], "
            "[class*='productContainer'], [class*='product-box'], "
            "[class*='ProductCard'], [data-testid*='product']"
        )

        for card in soup.select(selectors):
            title_el = card.select_one(
                "[data-qa='product-name'], [class*='productTitle'], "
                "[class*='title'], [class*='name'], h2, h3"
            )
            link = card.select_one("a[href]")
            if not (title_el and link):
                continue

            vals = []
            for node in card.select(
                "[class*='priceNow'], [class*='salePrice'], "
                "[class*='priceWas'], [class*='oldPrice'], "
                "[class*='price'], [data-qa='product-price']"
            ):
                val = parse_price(node.get_text(" ", strip=True))
                if val:
                    vals.append(val)

            if not vals:
                continue

            current = min(vals)
            old = max(vals) if len(vals) > 1 and max(vals) > current else None

            img = card.select_one("img")
            image = None
            if img:
                image = img.get("data-src") or img.get("data-lazy-src") or img.get("src")

            deals.append(
                Deal(
                    store=self.name,
                    title=title_el.get_text(" ", strip=True),
                    current_price=current,
                    old_price=old,
                    url=urljoin("https://www.noon.com", link.get("href")),
                    image_url=image,
                )
            )
        return deals

    def _parse_page(self, soup):
        deals = self._parse_html_cards(soup)

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
                self._extract_from_json(data, deals)

        unique = []
        seen = set()
        for deal in deals:
            key = (deal.title.lower().strip(), round(deal.current_price, 2), deal.url)
            if key in seen:
                continue
            seen.add(key)
            unique.append(deal)
        return unique

    async def fetch_deals(self):
        all_deals = []
        seen = set()

        for url in self.DEAL_URLS:
            try:
                soup = await self.get_soup(url)
            except Exception:
                continue

            for deal in self._parse_page(soup):
                key = (deal.title.lower().strip(), round(deal.current_price, 2), deal.url)
                if key not in seen:
                    seen.add(key)
                    all_deals.append(deal)

        return all_deals


    async def fetch_observations(self) -> list[Deal]:
        return await self.fetch_deals()

    async def search_products(self, query):
        url = "https://www.noon.com/egypt-en/search/?q=" + quote_plus(query)
        soup = await self.get_soup(url)
        return self._parse_page(soup)[:30]
