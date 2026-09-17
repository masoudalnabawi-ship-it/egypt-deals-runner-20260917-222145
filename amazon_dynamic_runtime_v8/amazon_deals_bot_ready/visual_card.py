import io
import os
import json
import uuid
import urllib.request
import mimetypes
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    def ar(text):
        text = str(text or "")
        try:
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
except Exception:
    def ar(text):
        return str(text or "")


W = 1080
H = 1350

BG = "#F4F5F7"
WHITE = "#FFFFFF"
DARK = "#131921"
ORANGE = "#FF9900"
GREEN = "#067D62"
RED = "#B12704"
GRAY = "#667085"
LIGHT = "#EAECF0"


def _font(size, bold=False):
    candidates = []

    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
        ]

    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)

    return ImageFont.load_default()


def _money(v):
    try:
        v = float(v)
        if v <= 0:
            return "—"
        if abs(v - round(v)) < 0.01:
            return f"{v:,.0f} EGP"
        return f"{v:,.2f} EGP"
    except Exception:
        return "—"


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _right(draw, text, xy, font, fill):
    text = ar(text)
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    draw.text(
        (xy[0] - width, xy[1]),
        text,
        font=font,
        fill=fill
    )


def _wrap(draw, text, font, max_width, max_lines=3):
    words = str(text or "").split()

    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:
        test = (current + " " + word).strip()
        shaped = ar(test)

        try:
            width = draw.textlength(shaped, font=font)
        except Exception:
            width = len(test) * 20

        if width <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines:
        original = " ".join(words)
        joined = " ".join(lines)
        if len(joined) < len(original):
            lines[-1] = lines[-1].rstrip(" .") + "..."

    return lines


def _load_product_image(url):
    req = urllib.request.Request(
        str(url),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        }
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()

    img = Image.open(io.BytesIO(data)).convert("RGB")
    return img


def _rounded(draw, box, radius=24, fill=WHITE, outline=None, width=1):
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=fill,
        outline=outline,
        width=width
    )


def _timestamp(p):
    raw = (
        p.get("live_checked_at")
        or p.get("checked_at")
        or p.get("last_checked")
        or p.get("seen_at")
        or p.get("timestamp")
    )

    try:
        raw = float(raw)
        if raw > 1000000000:
            return datetime.fromtimestamp(raw).strftime("%Y-%m-%d  %H:%M")
    except Exception:
        pass

    if p.get("live_rechecked"):
        return "تم التحقق قبل إرسال التنبيه"

    return "غير محدد"


def build_review_card(p, c, output_path):
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_brand = _font(48, True)
    f_title = _font(31, True)
    f_price = _font(61, True)
    f_ref = _font(29, False)
    f_badge = _font(30, True)
    f_label = _font(23, False)
    f_value = _font(28, True)
    f_small = _font(21, False)

    # ---------- Header ----------
    draw.rectangle((0, 0, W, 120), fill=DARK)

    draw.text(
        (55, 29),
        "amazon.eg",
        font=f_brand,
        fill=WHITE
    )

    draw.rounded_rectangle(
        (842, 33, 1025, 88),
        radius=18,
        fill=ORANGE
    )

    badge = ar("مصر")
    bb = draw.textbbox((0, 0), badge, font=f_badge)
    bw = bb[2] - bb[0]
    draw.text(
        (934 - bw / 2, 42),
        badge,
        font=f_badge,
        fill=DARK
    )

    # ---------- Product image ----------
    _rounded(
        draw,
        (45, 145, 1035, 620),
        radius=30,
        fill=WHITE
    )

    image_url = str(p.get("image_url") or "").strip()

    if image_url.startswith("http"):
        try:
            product = _load_product_image(image_url)
            product.thumbnail((900, 430))

            x = (W - product.width) // 2
            y = 165 + (425 - product.height) // 2

            img.paste(product, (x, y))
        except Exception:
            pass

    # ---------- Title ----------
    _rounded(
        draw,
        (45, 645, 1035, 790),
        radius=24,
        fill=WHITE
    )

    title = (
        p.get("title_ar")
        or p.get("title")
        or "منتج من Amazon Egypt"
    )

    lines = _wrap(
        draw,
        title,
        f_title,
        910,
        max_lines=3
    )

    y = 668
    for line in lines:
        _right(
            draw,
            line,
            (995, y),
            f_title,
            DARK
        )
        y += 41

    # ---------- Price block ----------
    _rounded(
        draw,
        (45, 815, 1035, 1015),
        radius=28,
        fill=WHITE
    )

    current = _num(
        c.get("effective_current")
        or p.get("current_price")
        or p.get("price")
    )

    reference = _num(
        c.get("reference")
        or p.get("reference_price")
        or p.get("amazon_old_price")
        or p.get("old_price")
    )

    verified_discount = _num(
        c.get("verified_discount")
    )

    claimed_discount = _num(
        c.get("claimed_discount")
    )

    discount = verified_discount or claimed_discount

    if not discount and reference > current > 0:
        discount = (
            (reference - current) / reference
        ) * 100

    saving = max(0, reference - current)

    draw.text(
        (75, 850),
        _money(current),
        font=f_price,
        fill=RED
    )

    _right(
        draw,
        "السعر الحالي",
        (995, 841),
        f_label,
        GRAY
    )

    if reference > current > 0:
        draw.text(
            (78, 933),
            "Reference: " + _money(reference),
            font=f_ref,
            fill=GRAY
        )

    if discount > 0:
        draw.rounded_rectangle(
            (790, 907, 990, 978),
            radius=21,
            fill=GREEN
        )

        txt = f"{discount:.0f}% OFF"
        bb = draw.textbbox((0, 0), txt, font=f_badge)
        tw = bb[2] - bb[0]

        draw.text(
            (890 - tw / 2, 924),
            txt,
            font=f_badge,
            fill=WHITE
        )

    # ---------- Details ----------
    def info_box(x1, y1, x2, y2, label, value, value_fill=DARK):
        _rounded(
            draw,
            (x1, y1, x2, y2),
            radius=22,
            fill=WHITE
        )

        _right(
            draw,
            label,
            (x2 - 25, y1 + 18),
            f_label,
            GRAY
        )

        _right(
            draw,
            value,
            (x2 - 25, y1 + 56),
            f_value,
            value_fill
        )

    promo = (
        p.get("promo_label")
        or p.get("promo_details")
        or ""
    )

    if not p.get("promo_verified"):
        promo = "لا يوجد عرض إضافي مؤكد"

    condition = (
        p.get("used_condition")
        or p.get("condition")
        or "جديد / غير محدد"
    )

    availability = (
        p.get("availability_text")
        or p.get("availability")
        or ""
    )

    if not availability:
        if p.get("in_stock") is True:
            availability = "متاح"
        elif p.get("in_stock") is False:
            availability = "غير متاح"
        else:
            availability = "غير محدد"

    info_box(
        45, 1040, 520, 1145,
        "التوفير",
        _money(saving) if saving > 0 else "—",
        GREEN if saving > 0 else DARK
    )

    info_box(
        545, 1040, 1035, 1145,
        "الحالة",
        str(condition)[:30]
    )

    promo_short = str(promo)
    if len(promo_short) > 42:
        promo_short = promo_short[:39] + "..."

    info_box(
        45, 1170, 665, 1275,
        "الكوبون / العرض",
        promo_short
    )

    info_box(
        690, 1170, 1035, 1275,
        "التوفر",
        str(availability)[:22],
        GREEN if "متاح" in str(availability) else DARK
    )

    # ---------- Footer ----------
    draw.rectangle(
        (0, 1300, W, H),
        fill=DARK
    )

    draw.text(
        (40, 1314),
        "Last check: " + _timestamp(p),
        font=f_small,
        fill=WHITE
    )

    _right(
        draw,
        "للمراجعة فقط",
        (1030, 1314),
        f_small,
        ORANGE
    )

    img.save(
        output_path,
        format="JPEG",
        quality=92,
        optimize=True
    )

    return output_path


def send_photo_file(
    api_base,
    chat_id,
    photo_path,
    caption,
    reply_markup,
    timeout=45
):
    boundary = "----EgyptDeals" + uuid.uuid4().hex

    fields = {
        "chat_id": str(chat_id),
        "caption": str(caption),
        "parse_mode": "HTML",
        "reply_markup": json.dumps(
            reply_markup,
            ensure_ascii=False
        )
    }

    body = bytearray()

    for name, value in fields.items():
        body.extend(
            f"--{boundary}\r\n".encode()
        )
        body.extend(
            (
                f'Content-Disposition: form-data; '
                f'name="{name}"\r\n\r\n'
            ).encode()
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    filename = os.path.basename(photo_path)
    mime = mimetypes.guess_type(filename)[0] or "image/jpeg"

    with open(photo_path, "rb") as f:
        file_data = f.read()

    body.extend(
        f"--{boundary}\r\n".encode()
    )
    body.extend(
        (
            f'Content-Disposition: form-data; '
            f'name="photo"; filename="{filename}"\r\n'
        ).encode()
    )
    body.extend(
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    body.extend(file_data)
    body.extend(b"\r\n")
    body.extend(
        f"--{boundary}--\r\n".encode()
    )

    req = urllib.request.Request(
        api_base + "/sendPhoto",
        data=bytes(body),
        headers={
            "Content-Type":
                f"multipart/form-data; boundary={boundary}"
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=timeout
    ) as r:
        payload = json.loads(
            r.read().decode()
        )

    if not payload.get("ok"):
        raise RuntimeError(
            f"sendPhoto multipart: {payload}"
        )

    return payload["result"]
