from __future__ import annotations

def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def _money(value):
    value = _num(value)
    if abs(value - round(value)) < 0.005:
        return f"{int(round(value)):,} جنيه"
    return f"{value:,.2f} جنيه"

def _discount(current, reference):
    current = _num(current)
    reference = _num(reference)
    if reference > current >= 0:
        return (reference - current) / reference * 100.0
    return 0.0

def choose_hype_label(
    current_price,
    *,
    reference_price=None,
    reference_verified=False,
    discount_percent=0,
    route=None,
):
    current = _num(current_price)
    reference = _num(reference_price)
    discount = _num(discount_percent)
    route = str(route or "")

    if current <= 0:
        return "🔥 ببلاش"

    if (
        reference_verified
        and reference > current
        and current / reference <= 0.05
    ):
        return "🔥 ببلاش تقريبًا"

    verified_drop = (
        _discount(current, reference)
        if reference_verified and reference > 0
        else 0.0
    )

    if reference_verified and verified_drop >= 80:
        return "🚨 SUPER ULTRA موثّق — شبه ببلاش"

    if reference_verified and verified_drop >= 60:
        return "🔥🔥 ULTRA حقيقي — موثّق"

    if reference_verified and verified_drop >= 40:
        return "⚡ عرض قوي موثّق"

    if route == "PRIVATE_URGENT":
        return "🚨 سعر غير طبيعي — مراجعة عاجلة"

    if discount >= 60:
        return "🔎 خصم ضخم ظاهر — يحتاج إثبات مستقل"

    if discount >= 40:
        return "🔎 خصم قوي ظاهر — يحتاج إثبات مستقل"

    return "🔎 عرض للمراجعة"

def build_flash_review_card(
    *,
    store,
    title,
    current_price,
    old_price=None,
    discount_percent=0,
    url="",
    image_url="",
    route="PRIVATE_REVIEW",
    priority_class="FAST_40_REVIEW",
    price_verified=False,
    reference_verified=False,
    reference_price=None,
    asin="",
):
    current = _num(current_price)
    old = _num(old_price)
    reference = _num(reference_price)

    display_reference = reference if reference > 0 else old
    display_discount = _num(discount_percent)

    if display_discount <= 0 and old > current > 0:
        display_discount = _discount(current, old)

    hype = choose_hype_label(
        current,
        reference_price=reference,
        reference_verified=reference_verified,
        discount_percent=display_discount,
        route=route,
    )

    state = (
        "✅ السعر مؤكد من صفحة المنتج"
        if price_verified
        else "⏳ السعر يحتاج إعادة تأكيد"
    )

    ref_state = (
        "✅ المرجع السعري مؤكد"
        if reference_verified
        else "⏳ المرجع السعري ما زال قيد التحقق"
    )

    true_ultra = bool(
        reference_verified
        and display_discount >= 60
    )

    header = (
        "🔥🔥 TRUE ULTRA — أولوية فورية 🔥🔥"
        if true_ultra
        else "🚨 FLASH REVIEW"
    )

    lines = [
        header,
        "",
        hype,
        f"📦 {str(title or '').strip()}",
        f"🏪 المتجر: {store}",
        f"💰 السعر الآن: {_money(current)}",
    ]

    if display_reference > current > 0:
        lines.append(
            f"🏷️ السعر الظاهر/المرجعي: {_money(display_reference)}"
        )

    if display_discount > 0:
        lines.append(
            f"📉 الخصم الظاهر: {display_discount:.1f}%"
        )

    lines += [
        f"🖼️ الصورة: {'متاحة من رابط المتجر' if image_url else 'غير متاحة'}",
        f"🔎 {state}",
        f"📊 {ref_state}",
    ]

    if asin:
        lines.append(f"📦 ASIN: {asin}")

    if route == "PRIVATE_URGENT":
        lines.append("⚡ أولوية قصوى — راجعه فورًا")

    urgent_lines = [
        ("🔥🔥 ULTRA حقيقي — أولوية فورية" if true_ultra else hype),
        f"📦 {str(title or '').strip()}",
        f"🏪 المتجر: {store}",
        f"💰 {_money(current)}",
    ]

    if display_reference > current > 0:
        urgent_lines.append(
            f"🏷️ بدل/مرجع ظاهر: {_money(display_reference)}"
        )

    if display_discount > 0:
        urgent_lines.append(
            f"🔥 خصم ظاهر {display_discount:.0f}%"
        )

    urgent_lines += [
        "",
        "⚡ السعر ممكن يتغير أو يختفي بسرعة",
        f"🛒 {url}",
    ]

    normal_lines = [
        hype,
        "",
        f"📦 {str(title or '').strip()}",
        f"🏪 المتجر: {store}",
        f"💰 السعر: {_money(current)}",
    ]

    if display_reference > current > 0:
        normal_lines.append(
            f"🏷️ بدل/مرجع ظاهر: {_money(display_reference)}"
        )

    if display_discount > 0:
        normal_lines.append(
            f"📉 الخصم الظاهر: {display_discount:.1f}%"
        )

    normal_lines += [
        "",
        f"🛒 رابط الشراء: {url}",
    ]

    return {
        "version": "flash_review_v9",
        "hype_label": hype,
        "route": route,
        "class": priority_class,
        "image_url": str(image_url or ""),
        "review_text": "\n".join(lines),
        "urgent_post_text": "\n".join(urgent_lines),
        "normal_post_text": "\n".join(normal_lines),
        "publish_actions": [
            {
                "id": "urgent",
                "label": "🚀 نشر عاجل",
                "requires_recheck": False,
                "intended_scope": "configured_urgent_destinations",
            },
            {
                "id": "normal",
                "label": "📢 نشر عادي",
                "requires_recheck": True,
                "intended_scope": "configured_normal_destinations",
            },
            {
                "id": "open",
                "label": "🔗 فتح المنتج",
                "url": str(url or ""),
            },
            {
                "id": "reject",
                "label": "❌ رفض",
            },
        ],
        "evidence": {
            "price_verified": bool(price_verified),
            "reference_verified": bool(reference_verified),
            "reference_price": reference or None,
            "display_reference": display_reference or None,
        },
    }
