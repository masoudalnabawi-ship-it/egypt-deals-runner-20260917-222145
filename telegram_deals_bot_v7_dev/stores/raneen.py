from urllib.parse import urljoin, quote_plus

from models import Deal
from stores.base import StoreConnector, parse_price


class RaneenConnector(StoreConnector):
    name = "raneen"
    BASE = "https://www.raneen.com/"
    SEEDS = [
        "https://www.raneen.com/",
        "https://www.raneen.com/ar/",
        "https://www.raneen.com/ar/discounts.html",
    ]

    def _value(self, element):
        if not element:
            return None
        amount = element.get("data-price-amount")
        if amount:
            try:
                return float(amount)
            except Exception:
                pass
        return parse_price(element.get_text(" ", strip=True))

    def _parse_cards(self, soup, discounts_only=False):
        deals = []
        seen = set()

        for card in soup.select(
            ".product-item, li.product-item, .product-item-info, "
            "[data-container='product-grid']"
        ):
            title_el = card.select_one(
                "a.product-item-link, .product-item-name, "
                "strong.product-item-name, [class*='product-name']"
            )
            link = card.select_one("a.product-item-link[href], a[href]")
            if not (title_el and link):
                continue

            current_el = card.select_one(
                "[data-price-type='finalPrice'][data-price-amount], "
                "[data-price-type='finalPrice'] [data-price-amount], "
                ".special-price [data-price-amount], .special-price .price, "
                ".price-final_price [data-price-amount]"
            )
            old_el = card.select_one(
                "[data-price-type='oldPrice'][data-price-amount], "
                "[data-price-type='oldPrice'] [data-price-amount], "
                ".old-price [data-price-amount], .old-price .price"
            )

            current = self._value(current_el)
            old = self._value(old_el)

            if not current:
                continue
            if old is not None and old <= current:
                old = None
            if discounts_only and not old:
                continue

            url = urljoin(self.BASE, link.get("href"))

            img = card.select_one("img")
            image = None
            if img:
                image = (
                    img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("data-original")
                    or img.get("src")
                )
                if image:
                    image = urljoin(self.BASE, image)

            key = (url, round(current, 2))
            if key in seen:
                continue
            seen.add(key)

            deals.append(
                Deal(
                    store=self.name,
                    title=title_el.get_text(" ", strip=True),
                    current_price=current,
                    old_price=old,
                    url=url,
                    image_url=image,
                )
            )

        return deals

    async def fetch_deals(self):
        all_deals = []
        seen = set()

        for url in self.SEEDS:
            try:
                soup = await self.get_soup(url)
            except Exception:
                continue

            for deal in self._parse_cards(soup, discounts_only=True):
                key = (deal.url, round(deal.current_price, 2))
                if key not in seen:
                    seen.add(key)
                    all_deals.append(deal)

        return all_deals

    async def fetch_observations(self):
        all_deals = []
        seen = set()

        for url in self.SEEDS:
            try:
                soup = await self.get_soup(url)
            except Exception:
                continue

            for deal in self._parse_cards(soup, discounts_only=False):
                key = (deal.url, round(deal.current_price, 2))
                if key in seen:
                    continue
                seen.add(key)
                all_deals.append(deal)

        return all_deals

    async def search_products(self, query):
        url = self.BASE + "catalogsearch/result/?q=" + quote_plus(query)
        soup = await self.get_soup(url)
        return self._parse_cards(soup, discounts_only=False)[:30]
