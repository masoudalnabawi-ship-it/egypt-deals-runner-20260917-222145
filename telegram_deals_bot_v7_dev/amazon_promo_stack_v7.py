from __future__ import annotations

import re


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def _norm(text):
    text = str(text or "").translate(ARABIC_DIGITS).lower()
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _num(value):
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return 0.0


def _near_bank(text, start, end, radius=70):
    chunk = text[max(0, start-radius): min(len(text), end+radius)]
    return bool(re.search(
        r"(?:visa|mastercard|card|bank|بطاق|كارت|بنك|ائتمان)",
        chunk,
        re.I,
    ))


def _dedupe(items):
    out = []
    seen = set()
    for item in items:
        key = (
            item.get("type"),
            round(float(item.get("percent") or 0), 2),
            round(float(item.get("value") or 0), 2),
            int(item.get("minimum_quantity") or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def parse_promo_stack_text(text):
    t = _norm(text)
    found = []

    if re.search(
        r"(buy\s*1.*?get\s*1|اشتر[ي]?\s*(?:1|واحد).*?(?:واحصل|خذ).*?(?:1|واحد).*?(?:مجانا|مجانًا)|قطعتين\s*بسعر\s*قطعة)",
        t,
        re.I,
    ):
        found.append({
            "type": "bogo",
            "percent": 50.0,
            "verified": True,
            "conditional": True,
            "label": "Buy 1 Get 1",
        })

    quantity_patterns = [
        r"(?:اشتر[ي]?|buy)\s*(\d+)\s*(?:او\s*اكثر|\+|or\s*more)?[^%]{0,90}?(?:وفر|save)\s*(\d+(?:\.\d+)?)\s*%",
        r"(?:وفر|save)\s*(\d+(?:\.\d+)?)\s*%[^0-9]{0,70}?(?:عند|when|on)?[^0-9]{0,30}?(?:شراء|buy(?:ing)?)\s*(\d+)",
    ]
    for idx, pattern in enumerate(quantity_patterns):
        for m in re.finditer(pattern, t, re.I):
            if idx == 0:
                qty = int(float(m.group(1)))
                pct = _num(m.group(2))
            else:
                pct = _num(m.group(1))
                qty = int(float(m.group(2)))
            if qty >= 1 and 0 < pct < 100:
                found.append({
                    "type": "quantity_percent",
                    "percent": pct,
                    "minimum_quantity": qty,
                    "verified": True,
                    "conditional": True,
                    "label": f"Buy {qty}+ / Save {pct:g}%",
                })

    for m in re.finditer(
        r"(?:subscribe\s*(?:&|and)?\s*save|اشترك\s*(?:و|و\s*)?وفر)[^%]{0,60}?(\d+(?:\.\d+)?)\s*%",
        t,
        re.I,
    ):
        pct = _num(m.group(1))
        if 0 < pct < 100:
            found.append({
                "type": "subscribe_save",
                "percent": pct,
                "verified": True,
                "conditional": True,
                "label": f"Subscribe & Save {pct:g}%",
            })

    task_patterns = [
        # Preferred form: "خصم المهام 150 جنيه" / "task 150 EGP".
        r"(?:خصم\s*)?(?:المهام|مهمات|task|mission)"
        r"[^0-9]{0,24}?"
        r"(\d+(?:\.\d+)?)\s*(?:جنيه|egp|le)",

        # Also allow "150 جنيه خصم المهام", but keep the distance tight
        # so the normal product price cannot be captured by mistake.
        r"(\d+(?:\.\d+)?)\s*(?:جنيه|egp|le)"
        r"[^a-z\u0600-\u06FF0-9]{0,6}"
        r"(?:خصم\s*)?(?:المهام|مهمات|task|mission)",
    ]
    for pattern in task_patterns:
        for m in re.finditer(pattern, t, re.I):
            value = _num(m.group(1))
            if value > 0:
                found.append({
                    "type": "task_fixed",
                    "value": value,
                    "verified": True,
                    "conditional": True,
                    "account_specific": True,
                    "label": f"Task discount {value:g} EGP",
                })

    coupon_patterns = [
        r"(?:coupon|كوبون)[^.]{0,70}?(\d+(?:\.\d+)?)\s*(?:جنيه|egp|le)",
        r"(\d+(?:\.\d+)?)\s*(?:جنيه|egp|le)[^.]{0,50}?(?:coupon|كوبون)",
    ]
    for pattern in coupon_patterns:
        for m in re.finditer(pattern, t, re.I):
            value = _num(m.group(1))
            if value > 0:
                found.append({
                    "type": "coupon_fixed",
                    "value": value,
                    "verified": True,
                    "conditional": True,
                    "label": f"Coupon {value:g} EGP",
                })

    for m in re.finditer(
        r"(\d+(?:\.\d+)?)\s*%[^.]{0,90}?(?:visa|mastercard|card|bank|بطاق|كارت|بنك|ائتمان)",
        t,
        re.I,
    ):
        pct = _num(m.group(1))
        if 0 < pct < 100:
            found.append({
                "type": "bank_percent",
                "percent": pct,
                "verified": True,
                "conditional": True,
                "primary": False,
                "label": f"Bank/Card {pct:g}%",
            })

    percent_patterns = [
        r"(?:save|وفر|خصم|discount)\s*(?:up\s*to\s*)?(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%\s*(?:off|خصم|توفير|saving)",
    ]
    for pattern in percent_patterns:
        for m in re.finditer(pattern, t, re.I):
            if _near_bank(t, m.start(), m.end()):
                continue
            pct = _num(m.group(1))
            if 0 < pct < 100:
                found.append({
                    "type": "product_percent",
                    "percent": pct,
                    "verified": True,
                    "conditional": True,
                    "label": f"Product promo {pct:g}%",
                })

    return _dedupe(found)


def _candidate_texts_from_soup(soup):
    pieces = []
    selectors = [
        "[id*='promo']", "[class*='promo']",
        "[id*='coupon']", "[class*='coupon']",
        "[id*='offer']", "[class*='offer']",
        "[id*='saving']", "[class*='saving']",
        "[id*='deal']", "[class*='deal']",
        "[id*='sns']", "[class*='sns']",
    ]

    for selector in selectors:
        try:
            for node in soup.select(selector)[:80]:
                text = node.get_text(" ", strip=True)
                if text:
                    pieces.append(text)
        except Exception:
            pass

    try:
        rx = re.compile(
            r"(وفر|خصم|save|off|coupon|كوبون|المهام|مهمات|task|mission|subscribe|اشترك|buy|اشتر)",
            re.I,
        )
        for node in soup.find_all(string=rx)[:120]:
            parent = getattr(node, "parent", None)
            text = parent.get_text(" ", strip=True) if parent is not None else str(node)
            if text:
                pieces.append(text)
    except Exception:
        pass

    try:
        page_text = soup.get_text(" ", strip=True)
        if page_text:
            pieces.append(page_text)
    except Exception:
        pass

    out = []
    seen = set()
    for piece in pieces:
        clean = _norm(piece)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def extract_stack_from_soup(soup):
    all_items = []
    for piece in _candidate_texts_from_soup(soup):
        all_items.extend(parse_promo_stack_text(piece))
    return _dedupe(all_items)


def page_current_price(soup):
    selectors = [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".priceToPay .a-offscreen",
        ".apexPriceToPay .a-offscreen",
        "#newBuyBoxPrice",
    ]
    for selector in selectors:
        try:
            node = soup.select_one(selector)
            if not node:
                continue
            text = node.get_text(" ", strip=True).translate(ARABIC_DIGITS)
            m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
            if m:
                value = _num(m.group(1))
                if value > 0:
                    return value
        except Exception:
            pass
    return 0.0


def calculate_effective_stack(price, stack):
    price = _num(price)
    if price <= 0:
        return {
            "final_price": 0.0,
            "saving": 0.0,
            "effective_discount_percent": 0.0,
            "ultra": False,
            "super_ultra": False,
            "primary_percent": 0.0,
            "task_discount": 0.0,
            "coupon_discount": 0.0,
            "bank_only": False,
            "lines": [],
        }

    stack = list(stack or [])

    public_percent_components = [
        x for x in stack
        if x.get("verified")
        and x.get("type") in {
            "product_percent", "quantity_percent", "subscribe_save", "bogo",
        }
    ]

    strongest = max(
        public_percent_components,
        key=lambda x: float(x.get("percent") or 0),
        default=None,
    )

    primary_percent = float(strongest.get("percent") or 0) if strongest else 0.0

    task_discount = max(
        [float(x.get("value") or 0) for x in stack
         if x.get("verified") and x.get("type") == "task_fixed"] or [0.0]
    )

    coupon_discount = max(
        [float(x.get("value") or 0) for x in stack
         if x.get("verified") and x.get("type") == "coupon_fixed"] or [0.0]
    )

    bank_components = [x for x in stack if x.get("type") == "bank_percent"]

    fixed_discount = task_discount if task_discount > 0 else coupon_discount

    final_price = price
    lines = []

    if primary_percent > 0:
        percent_saving = final_price * primary_percent / 100.0
        final_price -= percent_saving
        lines.append(
            f"🏷️ خصم العرض {primary_percent:g}%: -{percent_saving:,.2f} جنيه"
        )

    if fixed_discount > 0:
        applied = min(final_price, fixed_discount)
        final_price -= applied
        if task_discount > 0:
            lines.append(f"🎯 خصم المهام: -{applied:,.2f} جنيه")
        else:
            lines.append(f"🎟️ كوبون: -{applied:,.2f} جنيه")

    final_price = max(0.0, round(final_price, 2))
    saving = max(0.0, round(price - final_price, 2))
    effective = round((saving / price) * 100.0, 1) if price else 0.0

    bank_only = bool(bank_components) and not (
        primary_percent > 0 or fixed_discount > 0
    )

    ultra = (
        effective >= 40.0
        or (effective >= 25.0 and saving >= 5000.0)
    )

    # User rule: strictly ABOVE 60% = SUPER ULTRA.
    # Exactly 60% remains ULTRA.
    super_ultra = effective > 60.0

    if saving > 0:
        lines.append(f"💰 السعر النهائي المتوقع: {final_price:,.2f} جنيه")
        lines.append(f"🔥 التوفير الفعلي: {saving:,.2f} جنيه ({effective:g}%)")

    if task_discount > 0:
        lines.append("⚠️ خصم المهام قد يكون مرتبطًا بالحساب/المهمة ويحتاج تحقق السلة.")

    return {
        "final_price": final_price,
        "saving": saving,
        "effective_discount_percent": effective,
        "ultra": ultra,
        "super_ultra": super_ultra,
        "primary_percent": primary_percent,
        "task_discount": task_discount,
        "coupon_discount": coupon_discount,
        "bank_only": bank_only,
        "lines": lines,
    }


def legacy_promo_override(base, soup):
    result = dict(base or {})
    stack = extract_stack_from_soup(soup)
    price = page_current_price(soup)
    calc = calculate_effective_stack(price, stack)

    result["promo_stack"] = stack
    result["task_discount_value"] = calc["task_discount"]
    result["stack_effective_price"] = calc["final_price"] if calc["saving"] > 0 else 0.0
    result["stack_effective_discount_percent"] = calc["effective_discount_percent"]
    result["stack_ultra"] = bool(calc["ultra"])
    result["stack_super_ultra"] = bool(calc.get("super_ultra", False))
    result["stack_bank_only"] = bool(calc["bank_only"])
    result["stack_lines"] = calc["lines"]

    if calc["bank_only"]:
        return result

    if calc["effective_discount_percent"] >= 5.0:
        result["promo_type"] = "percent"
        result["promo_verified"] = True
        result["promo_percent"] = calc["effective_discount_percent"]
        result["coupon_value"] = 0.0
        result["conditional"] = any(
            bool(x.get("conditional"))
            for x in stack
            if x.get("type") != "bank_percent"
        )
        details = " | ".join(calc["lines"])
        result["details"] = details
        result["promo_text"] = details
        result["promo_source"] = "V7_STACK"

    return result


def condition_from_soup(soup):
    title = ""
    try:
        node = soup.select_one("#productTitle")
        if node:
            title = _norm(node.get_text(" ", strip=True))
    except Exception:
        pass

    if re.search(r"\b(renewed|refurbished)\b|مجد(?:د|دة)|معاد\s*تجديد", title, re.I):
        return "renewed"

    buybox_texts = []
    selectors = [
        "#buybox",
        "#merchant-info",
        "[data-csa-c-condition-type]",
        "[data-condition]",
        "#newAccordionRow",
        "#usedAccordionRow",
    ]
    for selector in selectors:
        try:
            node = soup.select_one(selector)
            if node:
                buybox_texts.append(node.get_text(" ", strip=True))
                for attr in ("data-csa-c-condition-type", "data-condition"):
                    value = node.get(attr)
                    if value:
                        buybox_texts.append(str(value))
        except Exception:
            pass

    text = _norm(" ".join(buybox_texts))

    patterns = [
        ("used_like_new", r"(used\s*[-–]?\s*like\s*new|مستعمل[^.]{0,20}(?:كالجديد|مثل\s*الجديد))"),
        ("used_very_good", r"(used\s*[-–]?\s*very\s*good|مستعمل[^.]{0,20}جيد\s*جدا)"),
        ("used_acceptable", r"(used\s*[-–]?\s*acceptable|مستعمل[^.]{0,20}مقبول)"),
        ("used_good", r"(used\s*[-–]?\s*good|مستعمل[^.]{0,20}جيد)"),
        ("renewed", r"(renewed|refurbished|معاد\s*تجديد|مجدد)"),
        ("open_box", r"(open\s*box|open-box|علبة\s*مفتوحة|صندوق\s*مفتوح)"),
        ("resale", r"(amazon\s*resale|amazon\s*warehouse)"),
    ]

    for condition, pattern in patterns:
        if re.search(pattern, text, re.I):
            return condition

    return "new"


def offer_key(asin, condition):
    return f"{str(asin or '').upper()}|{str(condition or 'new').lower()}"

# V11_PROMO_STACK_OVERRIDE
# Conservative compatibility bridge: never blindly combine promotions when
# legacy extraction did not prove stackability/payment/account eligibility.
from promo_stack_v11 import evaluate_legacy_amazon as _v11_evaluate_legacy_amazon

def calculate_effective_stack(price, stack):
    return _v11_evaluate_legacy_amazon(price, stack)
