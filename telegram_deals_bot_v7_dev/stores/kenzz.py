import asyncio
import inspect
import re
import urllib.request
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from models import Deal


class KenzzConnector:
    name = "kenzz"
    BASE = "https://kenzz.com/"
    SEEDS = [
        "https://kenzz.com/",
        "https://kenzz.com/?page=2",
        "https://kenzz.com/?page=3",
        "https://kenzz.com/?page=4",
        "https://kenzz.com/?page=5",
        "https://kenzz.com/brand/kenzz",
        "https://kenzz.com/brand/brand-stores",
    ]

    def __init__(self, timeout=30, user_agent=None):
        self.timeout = min(float(timeout or 30), 20.0)
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Safari/537.36"
        )

    def _get(self, url):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", errors="ignore")

    @staticmethod
    def _price(value):
        try:
            return float(value.replace(",", "").strip())
        except Exception:
            return None

    @staticmethod
    def _make_deal(**values):
        params = inspect.signature(Deal).parameters
        kwargs = {k: v for k, v in values.items() if k in params}
        for k, v in {
            "currency": "EGP",
            "category": "",
            "seller": "Kenzz",
            "source": "kenzz",
            "promo_text": "",
        }.items():
            if k in params and k not in kwargs:
                kwargs[k] = v
        return Deal(**kwargs)

    def _parse(self, html):
        soup = BeautifulSoup(html, "html.parser")
        out, seen = [], set()

        for a in soup.select('a[href*="/product/"]'):
            href = a.get("href")
            if not href:
                continue

            url = urljoin(self.BASE, href)
            if url in seen:
                continue

            text = re.sub(r"\s+", " ", " ".join(a.stripped_strings)).strip()
            dm = re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*-?", text)
            prices = re.findall(
                r"(\d[\d,]*(?:\.\d+)?)\s*(?:ج\.?\s*م|EGP)", text, flags=re.I
            )
            if not dm or len(prices) < 2:
                continue

            old_price = self._price(prices[0])
            current_price = self._price(prices[1])
            if not old_price or not current_price or old_price <= current_price:
                continue

            real_discount = ((old_price - current_price) / old_price) * 100.0
            if real_discount < 5.0:
                continue

            title_text = re.sub(r"^\s*\d+(?:[.,]\d+)?\s*%\s*-?\s*", "", text)
            pos = title_text.find(prices[0])
            title = title_text[:pos].strip(" -") if pos >= 0 else title_text[:180].strip()
            if len(title) < 3:
                continue

            image_url = None
            img = a.find("img")
            if img:
                image_url = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if image_url:
                    image_url = urljoin(self.BASE, image_url)

            try:
                deal = self._make_deal(
                    store="kenzz",
                    title=title,
                    title_ar=title,
                    current_price=current_price,
                    old_price=old_price,
                    url=url,
                    image_url=image_url,
                )
            except Exception:
                continue

            seen.add(url)
            out.append(deal)

        return out

    async def fetch_deals(self):
        def work():
            all_deals, seen = [], set()
            for url in self.SEEDS:
                try:
                    html = self._get(url)
                except Exception:
                    continue
                for deal in self._parse(html):
                    key = getattr(deal, "url", None) or repr(deal)
                    if key in seen:
                        continue
                    seen.add(key)
                    all_deals.append(deal)
            return all_deals
        return await asyncio.to_thread(work)


    async def fetch_observations(self):
        return await self.fetch_deals()

    async def search(self, query):
        deals = await self.fetch_deals()
        terms = [x for x in re.split(r"\s+", str(query).lower()) if len(x) >= 2]
        if not terms:
            return deals[:20]
        ranked = []
        for deal in deals:
            title = str(getattr(deal, "title", "")).lower()
            score = sum(1 for t in terms if t in title)
            if score:
                ranked.append((score, deal))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in ranked[:20]]


    async def search_products(self, query):
        return await self.search(query)
