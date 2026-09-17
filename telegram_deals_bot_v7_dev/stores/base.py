from abc import ABC, abstractmethod
import os
import logging
import re
import httpx
from bs4 import BeautifulSoup
from models import Deal

logger = logging.getLogger(__name__)

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")

def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    text = text.translate(ARABIC_DIGITS)
    text = text.replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None

class StoreConnector(ABC):
    name: str

    def __init__(self, timeout: float, user_agent: str):
        self.timeout = timeout
        self.headers = {
            "User-Agent": user_agent,
            "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
        }

    async def _cloud_proxy_soup(self, client, url: str) -> BeautifulSoup:
        cloud_url = os.getenv("CLOUD_API_URL", "").strip()
        cloud_key = os.getenv("CLOUD_API_KEY", "").strip()

        if not cloud_url or not cloud_key:
            raise RuntimeError(f"{self.name}: cloud proxy unavailable")

        cloud_url = cloud_url.rstrip("/")
        if cloud_url.endswith("/api/deals"):
            cloud_url = cloud_url[:-len("/api/deals")]

        proxy_url = cloud_url + "/api/store-proxy"

        proxy = await client.get(
            proxy_url,
            params={"url": url},
            headers={
                "x-api-key": cloud_key,
                "Accept": "text/html",
            },
        )
        proxy.raise_for_status()

        logger.info(
            "%s Cloudflare proxy returned %s bytes",
            self.name,
            len(proxy.content),
        )
        return BeautifulSoup(proxy.text, "html.parser")

    async def get_soup(self, url: str) -> BeautifulSoup:
        async with httpx.AsyncClient(
            headers=self.headers,
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                return BeautifulSoup(response.text, "html.parser")

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (403, 429):
                    raise
                logger.warning(
                    "%s direct access returned %s; trying cloud proxy",
                    self.name,
                    exc.response.status_code,
                )
                return await self._cloud_proxy_soup(client, url)

            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError):
                logger.warning(
                    "%s direct access timed out/failed; trying cloud proxy",
                    self.name,
                )
                return await self._cloud_proxy_soup(client, url)

    @abstractmethod
    async def fetch_deals(self) -> list[Deal]:
        raise NotImplementedError
