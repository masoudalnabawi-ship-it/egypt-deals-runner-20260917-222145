from __future__ import annotations
import asyncio
import logging
import os

DEFAULT_NON_AMAZON = "noon,noon_minutes,jumia,2b,btech,raya,dream2000,carrefour,raneen,kenzz"
raw = os.getenv("V11_ENABLED_STORES") or os.getenv("ENABLED_STORES") or DEFAULT_NON_AMAZON
stores = [x.strip().lower() for x in raw.split(",") if x.strip() and x.strip().lower() != "amazon"]
os.environ["ENABLED_STORES"] = ",".join(dict.fromkeys(stores))

from config import settings
import db
import engine
from db import init_db
from v11_state import init_state, observe_before_save, is_restock, record_priority, record_event, clear_cycle_flag

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("v11-runtime")
logging.getLogger("httpx").setLevel(logging.WARNING)


def install_instrumentation():
    """Piggyback restock + benchmark on existing flow; zero extra store requests."""
    original_save_seen = engine.save_seen
    original_should_review = engine.should_review
    original_mark_stage = engine.mark_stage
    original_ensure_first_seen = engine.ensure_first_seen
    original_direct_review = engine._send_direct_flash_review

    def save_seen_v11(deal):
        fp = db.fingerprint(deal)
        observe_before_save(fp, deal)
        return original_save_seen(deal)

    def should_review_v11(fp, deal):
        if is_restock(fp):
            return True
        return original_should_review(fp, deal)

    def ensure_first_seen_v11(deal):
        result = original_ensure_first_seen(deal)
        try:
            record_event(db.fingerprint(deal), "first_seen", deal)
        except Exception:
            pass
        return result

    def mark_stage_v11(deal, stage):
        result = original_mark_stage(deal, stage)
        try:
            record_event(db.fingerprint(deal), str(stage), deal)
        except Exception:
            pass
        return result

    async def direct_review_v11(deal, fp, signal, truth):
        truth = dict(truth or {})
        if is_restock(fp):
            truth["class"] = "RESTOCK_" + str(truth.get("class") or "PRIORITY")
            truth["reason"] = "high-value product reappeared after an absence; " + str(truth.get("reason") or "")
            record_event(fp, "restock_detected", deal, {"truth": truth})
        result = await original_direct_review(deal, fp, signal, truth)
        record_priority(fp, truth)
        record_event(fp, "private_sent", deal, {"truth": truth})
        clear_cycle_flag(fp)
        return result

    engine.save_seen = save_seen_v11
    engine.should_review = should_review_v11
    engine.ensure_first_seen = ensure_first_seen_v11
    engine.mark_stage = mark_stage_v11
    engine._send_direct_flash_review = direct_review_v11


async def scanner_loop():
    while True:
        try:
            sent = await engine.scan_once()
            log.info("DEV scan complete | reviews=%s | stores=%s", sent, ",".join(settings.enabled_stores))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("DEV scan failed; moderation remains alive")
        await asyncio.sleep(max(60, settings.check_interval_minutes * 60))


async def main():
    init_db()
    init_state()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    if not settings.telegram_channel_id:
        raise RuntimeError("TELEGRAM_CHANNEL_ID missing")
    if not settings.admin_chat_id:
        raise RuntimeError("ADMIN_CHAT_ID missing")
    if "amazon" in settings.enabled_stores:
        raise RuntimeError("Safety guard: Amazon is disabled in V11 safe runtime")
    install_instrumentation()
    log.warning(
        "V11 STABLE DEV | AmazonPrimary=EXTERNAL_RADAR | AmazonDedicatedBot=ON | AmazonScanner=OFF | Noon=DEGRADED | Restock=ON | Benchmark=ON | stores=%s",
        ",".join(settings.enabled_stores),
    )
    await asyncio.gather(
        scanner_loop(),
        engine.moderation_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
