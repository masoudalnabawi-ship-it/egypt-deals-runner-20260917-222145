#!/usr/bin/env python3
from __future__ import annotations

import compileall
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RADAR = ROOT / "telegram_deals_bot_v1_ready"
REVIEW = ROOT / "amazon_deals_bot_ready"
sys.path.insert(0, str(RADAR))

from bs4 import BeautifulSoup
from amazon_promo_v5 import extract_amazon_promo
from deal_engine_v5 import evaluate_deal


def check(name, cond, details=""):
    if not cond:
        raise AssertionError(f"{name}: {details}")
    print(f"✅ {name}")


def promo(html):
    return extract_amazon_promo(BeautifulSoup(html, "html.parser"))


def main():
    # Syntax across the ready codebase.
    check("compile radar", compileall.compile_dir(str(RADAR), quiet=1))
    check("compile review", compileall.compile_dir(str(REVIEW), quiet=1))

    # General 50% = public + verified + ULTRA.
    p = promo('<div id="promotions_feature_div"><span>خصم 50% عند الشراء</span></div>')
    check("general 50 detected", p.get("promo_percent") == 50.0, p)
    check("general 50 public", p.get("promo_scope") == "universal", p)
    check("general 50 verified", bool(p.get("promo_verified")), p)
    check("general 50 priority", bool(p.get("general_50_plus")), p)

    d = evaluate_deal(
        current_price=1000,
        promo_type=p.get("promo_type"),
        promo_percent=p.get("promo_percent"),
        promo_verified=p.get("promo_verified"),
        conditional=p.get("conditional"),
        member_only=p.get("member_only"),
        promo_scope=p.get("promo_scope"),
        account_specific=p.get("account_specific"),
    )
    check("general 50 sends", d.get("send") is True, d)
    check("general 50 ULTRA", d.get("tier") == "ULTRA", d)

    # Bank offer is isolated and cannot trigger a public deal by itself.
    p = promo('<div id="promotions_feature_div">خصم 50% عند الدفع ببطاقة CIB</div>')
    check("bank scope isolated", p.get("promo_scope") == "bank", p)
    check("bank conditional", bool(p.get("conditional")), p)
    d = evaluate_deal(
        current_price=1000,
        promo_type=p.get("promo_type"),
        promo_percent=p.get("promo_percent"),
        promo_verified=p.get("promo_verified"),
        conditional=p.get("conditional"),
        member_only=p.get("member_only"),
        promo_scope=p.get("promo_scope"),
        account_specific=p.get("account_specific"),
    )
    check("bank alone does not send", d.get("send") is False, d)

    # Public and bank offers on same dynamic container remain separate.
    p = promo('''
        <div id="quickPromoBucketContent">
          <span>خصم 30% عند الشراء</span>
          <span>خصم 50% عند الدفع ببطاقة Visa</span>
        </div>
    ''')
    check("mixed keeps public primary", p.get("promo_scope") == "universal", p)
    check("mixed public percent", p.get("promo_percent") == 30.0, p)
    check("mixed keeps bank metadata", len(p.get("bank_offers", [])) >= 1, p)

    # Account-targeted offer cannot become public.
    p = promo('<div class="promo-box">خصم 60% لحسابات مؤهلة فقط</div>')
    check("account offer isolated", p.get("promo_scope") == "account", p)
    check("account flag", bool(p.get("account_specific")), p)

    # "Up to" is never treated as a guaranteed public 50%.
    p = promo('<div id="promotions_feature_div">خصم حتى 50% على منتجات مختارة</div>')
    check("up-to not guaranteed", not bool(p.get("general_50_plus")), p)
    check("up-to not verified final", not bool(p.get("promo_verified")), p)

    # An ordinary price savings badge is not a second checkout promo.
    p = promo('<span class="a-size-large savingsPercentage">خصم 50%</span>')
    check("price badge not verified promo", not bool(p.get("promo_verified")), p)

    # Cache/backoff/circuit-breaker implementation remains present.
    radar_src = (RADAR / "amazon_radar.py").read_text(encoding="utf-8")
    for token in (
        "AMAZON_FETCH_CACHE", "AMAZON_SEARCH_BACKOFF_UNTIL",
        "AMAZON_CIRCUIT_UNTIL", "def note_amazon_503", "async def fetch_html",
    ):
        check(f"cache/backoff {token}", token in radar_src)
    check("no hardcoded /root in radar", "/root/" not in radar_src)

    # Review card can be generated without network access when Pillow exists.
    try:
        sys.path.insert(0, str(REVIEW))
        from visual_card import build_review_card
        out = Path(tempfile.gettempdir()) / "amazon_ready_self_test.jpg"
        build_review_card(
            {"title": "Self Test Product", "current_price": 500, "url": "https://www.amazon.eg/dp/B000000000"},
            {"effective_current": 500, "reference": 1000, "independent": True, "verified_discount": 50, "claimed_discount": 0, "score": 92},
            str(out),
        )
        check("review image card", out.exists() and out.stat().st_size > 1000)
        out.unlink(missing_ok=True)
    except Exception as exc:
        # Image card is optional at runtime; bot has text fallback. Record the
        # capability instead of failing the whole installer on minimal Termux.
        print(f"⚠️ review image card optional fallback: {exc}")

    print("\n✅ SELF-TEST PASSED")


if __name__ == "__main__":
    main()
