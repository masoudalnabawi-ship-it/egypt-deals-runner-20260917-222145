from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

CAPTURE_PYTHON = os.getenv(
    "REVIEW_CAPTURE_PYTHON",
    os.path.expanduser("~/amazon_dynamic_runtime_v8/.venv/bin/python"),
)
CAPTURE_HELPER = str(Path(__file__).with_name("store_page_capture.py"))


async def prepare_review_media(deal, key: str) -> dict:
    """Create one durable store-page image for both review and publication."""
    if os.getenv("STORE_SCREENSHOT_REVIEWS", "1") != "1":
        return {"media_path": "", "media_source": "disabled"}

    py = CAPTURE_PYTHON if Path(CAPTURE_PYTHON).exists() else os.sys.executable
    args = [
        py, CAPTURE_HELPER,
        "--store", str(getattr(deal, "store", "") or ""),
        "--url", str(getattr(deal, "url", "") or ""),
        "--key", str(key or getattr(deal, "external_id", "deal") or "deal"),
        "--image-url", str(getattr(deal, "image_url", "") or ""),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=42)
        if proc.returncode != 0:
            return {"media_path": "", "media_source": "capture_error", "media_error": err.decode("utf-8", "ignore")[-300:]}
        lines = out.decode("utf-8", "ignore").strip().splitlines()
        data = json.loads(lines[-1]) if lines else {}
        path = str(data.get("screenshot") or "")
        if data.get("ok") and path and Path(path).exists():
            return {
                "media_path": path,
                "media_source": str(data.get("source") or "store_page"),
            }
        return {"media_path": "", "media_source": "capture_failed", "media_error": str(data.get("reason") or "")[:300]}
    except Exception as exc:
        return {"media_path": "", "media_source": "capture_exception", "media_error": str(exc)[:300]}
