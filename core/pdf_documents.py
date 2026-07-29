from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


_DEFAULT_PRIMARY = "#c9a84c"
GREEN = colors.HexColor(_DEFAULT_PRIMARY)
DARK = colors.HexColor("#13161b")
TEXT = colors.HexColor("#0d0f12")
SLATE = colors.HexColor("#64748b")
BORDER = colors.HexColor("#cbd5e1")
SUBTLE = colors.HexColor("#f8fafc")
AMBER = colors.HexColor("#f59e0b")


def _money(settings, value):
    amount = value or Decimal("0.00")
    return f"{settings.currency} {amount:.2f}"


def _date(value):
    return value.strftime("%d %b %Y") if value else "-"


def _draw_text(c, text, x, y, size=10, color=TEXT, font="Helvetica"):
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawString(x, y, str(text or ""))


def _draw_right(c, text, x, y, size=10, color=TEXT, font="Helvetica"):
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawRightString(x, y, str(text or ""))


def _draw_wrapped(c, text, x, y, width, size=10, color=TEXT, font="Helvetica", leading=14, max_lines=4):
    words = str(text or "").replace("\r", "").split()
    if not words:
        return y
    c.setFillColor(color)
    c.setFont(font, size)
    line = ""
    lines = []
    for word in words:
        candidate = f"{line} {word}".strip()
        if c.stringWidth(candidate, font, size) <= width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    for line in lines[:max_lines]:
        c.drawString(x, y, line)
        y -= leading
    return y


def _primary_color(settings):
    raw = getattr(settings, "pdf_primary_color", None) or _DEFAULT_PRIMARY
    try:
        return colors.HexColor(raw)
    except Exception:
        return GREEN


def _draw_brand_mark(c, x, y, size=28, primary=None):
    c.setFillColor(primary or GREEN)
    c.roundRect(x, y, size, size, 6, fill=1, stroke=0)
    gap = size * 0.13
    tile = (size - gap * 3) / 2
    sx = x + gap
    sy = y + gap
    c.setFillColor(colors.Color(0.05, 0.06, 0.07, alpha=0.86))
    c.roundRect(sx, sy + tile + gap, tile, tile, 2, fill=1, stroke=0)
    c.roundRect(sx + tile + gap, sy, tile, tile, 2, fill=1, stroke=0)
    c.setFillColor(colors.Color(0.05, 0.06, 0.07, alpha=0.48))
    c.roundRect(sx + tile + gap, sy + tile + gap, tile, tile, 2, fill=1, stroke=0)
    c.roundRect(sx, sy, tile, tile, 2, fill=1, stroke=0)


def _response(buffer, filename):
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def _quantity_label(booking):
    is_per_person = bool(booking.room_id and booking.room.pricing_model == "per_person")
    guest_suffix = f" × {booking.num_guests} guests" if is_per_person and booking.num_guests > 1 else ""
    if booking.is_hourly:
        return booking.duration_label + guest_suffix
    if booking.booking_duration_type == "weekly":
        weeks = max((Decimal(booking.num_nights) / Decimal("7")), Decimal("1.00"))
        return f"{weeks.normalize()} week" + ("" if weeks == 1 else "s") + guest_suffix
    if booking.booking_duration_type == "24_hours":
        quantity = max(booking.num_nights, 1)
        return f"{quantity} x 24-hour" + guest_suffix
    quantity = max(booking.num_nights, 1)
    return f"{quantity} night" + ("" if quantity == 1 else "s") + guest_suffix


def render_booking_invoice_pdf(booking, settings, payment_totals, vat_amount=Decimal("0.00")):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 18 * mm
    right = width - margin
    y = height - margin

    PRIMARY = _primary_color(settings)
    invoice_number = f"INV-{booking.booking_reference}"
    balance_due = payment_totals["balance_due"]
    status = "PAID" if balance_due <= 0 else "BALANCE DUE"

    c.setFillColor(PRIMARY)
    c.rect(0, height - 8, width, 8, fill=1, stroke=0)

    _draw_brand_mark(c, margin, y - 31, 28, primary=PRIMARY)
    _draw_text(c, settings.guest_house_name or "Circle Core Guest House", margin + 38, y - 10, 18, TEXT, "Helvetica-Bold")
    company_y = y - 28
    company_y = _draw_wrapped(c, settings.address, margin + 38, company_y, 230, 9, SLATE, max_lines=3, leading=11)
    contact = " | ".join(v for v in [settings.phone, settings.email] if v)
    _draw_text(c, contact, margin + 38, company_y - 2, 9, SLATE)
    if settings.vat_registered and settings.vat_number:
        _draw_text(c, f"VAT No: {settings.vat_number}", margin + 38, company_y - 14, 9, SLATE)

    _draw_right(c, "INVOICE", right, y - 8, 28, TEXT, "Helvetica-Bold")
    c.setFillColor(PRIMARY if balance_due <= 0 else AMBER)
    c.roundRect(right - 70, y - 34, 70, 17, 8, fill=1, stroke=0)
    _draw_right(c, status, right - 9, y - 29, 8, colors.white, "Helvetica-Bold")

    meta_y = y - 56
    for label, value in [("Invoice", invoice_number), ("Issued", _date(timezone.localdate())), ("Due", _date(booking.check_in_date))]:
        _draw_text(c, label, right - 160, meta_y, 9, SLATE)
        _draw_right(c, value, right, meta_y, 9, TEXT, "Helvetica-Bold")
        c.setStrokeColor(colors.HexColor("#e2e8f0"))
        c.line(right - 160, meta_y - 5, right, meta_y - 5)
        meta_y -= 18

    y -= 126
    c.setStrokeColor(DARK)
    c.setLineWidth(1.2)
    c.line(margin, y, right, y)
    y -= 18

    box_w = (right - margin - 14) / 2
    for x, title in [(margin, "BILL TO"), (margin + box_w + 14, "BOOKING DETAILS")]:
        c.setFillColor(SUBTLE)
        c.setStrokeColor(BORDER)
        c.rect(x, y - 96, box_w, 96, fill=1, stroke=1)
        _draw_text(c, title, x + 12, y - 18, 8, PRIMARY, "Helvetica-Bold")

    left_x = margin + 12
    right_x = margin + box_w + 26
    _draw_text(c, booking.guest.full_name, left_x, y - 42, 13, TEXT, "Helvetica-Bold")
    if booking.vehicle_registration:
        _draw_text(c, "Number plate", left_x, y - 62, 9, SLATE)
        _draw_text(c, booking.vehicle_registration, left_x + 70, y - 62, 9, TEXT, "Helvetica-Bold")
    else:
        _draw_text(c, "Phone", left_x, y - 62, 9, SLATE)
        _draw_text(c, booking.guest.phone or "N/A", left_x + 70, y - 62, 9, TEXT, "Helvetica-Bold")
    if getattr(booking.guest, "email", "") and not booking.vehicle_registration:
        _draw_text(c, "Email", left_x, y - 78, 9, SLATE)
        _draw_text(c, booking.guest.email, left_x + 70, y - 78, 9, TEXT, "Helvetica-Bold")

    details = [
        ("Reference", booking.booking_reference),
        ("Room", booking.room.name),
        ("Stay", f"{_date(booking.check_in_date)} to {_date(booking.check_out_date)}"),
        ("Type", booking.duration_label),
    ]
    detail_y = y - 36
    for label, value in details:
        _draw_text(c, label, right_x, detail_y, 9, SLATE)
        _draw_text(c, value, right_x + 72, detail_y, 9, TEXT, "Helvetica-Bold")
        detail_y -= 16

    y -= 126
    col = {
        "desc": margin,
        "qty": margin + 280,
        "rate": margin + 365,
        "amount": right,
    }
    table_w = right - margin
    c.setFillColor(DARK)
    c.rect(margin, y - 30, table_w, 30, fill=1, stroke=0)
    _draw_text(c, "DESCRIPTION", col["desc"] + 12, y - 19, 8, colors.white, "Helvetica-Bold")
    _draw_right(c, "QTY", col["qty"] + 50, y - 19, 8, colors.white, "Helvetica-Bold")
    _draw_right(c, "RATE", col["rate"] + 64, y - 19, 8, colors.white, "Helvetica-Bold")
    _draw_right(c, "AMOUNT", col["amount"] - 12, y - 19, 8, colors.white, "Helvetica-Bold")

    line_total = booking.total_amount + booking.discount
    row_y = y - 30
    c.setStrokeColor(BORDER)
    c.rect(margin, row_y - 58, table_w, 58, fill=0, stroke=1)
    is_per_person = bool(booking.room_id and booking.room.pricing_model == "per_person")
    if is_per_person:
        # Per-person (shared-capacity) accommodation: this guest's own space
        # count, nights and PPPN rate, spelled out — never any other
        # occupant's name, booking reference, or how many people the room
        # holds in total.
        description = f"{booking.room.name} — {booking.room.room_type} accommodation"
        nights = max(booking.num_nights, 1)
        guests = booking.num_guests
        subtext = (
            f"{guests} guest{'' if guests == 1 else 's'} × "
            f"{nights} night{'' if nights == 1 else 's'} × "
            f"{_money(settings, booking.rate_per_night)} PPPN"
        )
    else:
        description = f"{booking.room.name} accommodation"
        subtext = f"{_date(booking.check_in_date)} to {_date(booking.check_out_date)}"
    _draw_text(c, description, margin + 12, row_y - 22, 10, TEXT, "Helvetica-Bold")
    _draw_text(c, subtext, margin + 12, row_y - 39, 9, SLATE)
    _draw_right(c, _quantity_label(booking), col["qty"] + 50, row_y - 28, 9, TEXT)
    _draw_right(c, _money(settings, booking.rate_per_night), col["rate"] + 64, row_y - 28, 9, TEXT)
    _draw_right(c, _money(settings, line_total), col["amount"] - 12, row_y - 28, 9, TEXT, "Helvetica-Bold")
    row_y -= 58
    if booking.discount > 0:
        c.rect(margin, row_y - 34, table_w, 34, fill=0, stroke=1)
        _draw_text(c, "Discount", margin + 12, row_y - 22, 10, TEXT, "Helvetica-Bold")
        _draw_right(c, f"- {_money(settings, booking.discount)}", col["amount"] - 12, row_y - 22, 9, TEXT, "Helvetica-Bold")
        row_y -= 34

    totals_x = right - 230
    totals_y = row_y - 18
    total_rows = []
    if settings.vat_registered:
        total_rows.append((f"VAT included at {settings.vat_rate}%", _money(settings, vat_amount), False))
    total_rows.extend([
        ("Total", _money(settings, booking.total_amount), True),
        ("Amount paid", _money(settings, payment_totals["total_paid"]), False),
    ])
    if payment_totals["total_refunded"] > 0:
        total_rows.append(("Refunded", f"- {_money(settings, payment_totals['total_refunded'])}", False))
        total_rows.append(("Net paid", _money(settings, payment_totals["net_paid"]), False))
    total_rows.append(("Balance due", _money(settings, balance_due), False))

    for label, value, is_total in total_rows:
        if is_total:
            c.setFillColor(DARK)
            c.rect(totals_x, totals_y - 24, 230, 24, fill=1, stroke=0)
            _draw_text(c, label, totals_x + 10, totals_y - 16, 11, colors.white, "Helvetica-Bold")
            _draw_right(c, value, totals_x + 220, totals_y - 16, 11, colors.white, "Helvetica-Bold")
        else:
            c.setStrokeColor(BORDER)
            c.rect(totals_x, totals_y - 24, 230, 24, fill=0, stroke=1)
            color = PRIMARY if label == "Balance due" and balance_due <= 0 else (AMBER if label == "Balance due" else TEXT)
            _draw_text(c, label, totals_x + 10, totals_y - 16, 9, SLATE if label != "Balance due" else color, "Helvetica-Bold" if label == "Balance due" else "Helvetica")
            _draw_right(c, value, totals_x + 220, totals_y - 16, 9, color, "Helvetica-Bold")
        totals_y -= 24

    notes_y = totals_y - 24
    if settings.banking_details or settings.invoice_notes:
        note_w = (right - margin - 14) / 2
        if settings.banking_details:
            c.setStrokeColor(BORDER)
            c.rect(margin, notes_y - 78, note_w, 78, fill=0, stroke=1)
            _draw_text(c, "PAYMENT DETAILS", margin + 10, notes_y - 15, 8, PRIMARY, "Helvetica-Bold")
            _draw_wrapped(c, settings.banking_details, margin + 10, notes_y - 31, note_w - 20, 8.5, SLATE, leading=11, max_lines=4)
        if settings.invoice_notes:
            x = margin + note_w + 14
            c.setStrokeColor(BORDER)
            c.rect(x, notes_y - 78, note_w, 78, fill=0, stroke=1)
            _draw_text(c, "NOTES", x + 10, notes_y - 15, 8, PRIMARY, "Helvetica-Bold")
            _draw_wrapped(c, settings.invoice_notes, x + 10, notes_y - 31, note_w - 20, 8.5, SLATE, leading=11, max_lines=4)

    _draw_text(c, "Thank you for your business. Generated by Circle Core Guest House.", margin, 22 * mm, 8, SLATE)
    c.save()
    return _response(buffer, f"{booking.booking_reference}-invoice.pdf")


def render_payment_receipt_pdf(booking, payment, settings, receipt_number, balance_after_payment):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 18 * mm
    right = width - margin
    y = height - margin

    PRIMARY = _primary_color(settings)
    c.setFillColor(PRIMARY)
    c.rect(0, height - 8, width, 8, fill=1, stroke=0)
    _draw_brand_mark(c, margin, y - 31, 28, primary=PRIMARY)
    _draw_text(c, settings.guest_house_name or "Circle Core Guest House", margin + 38, y - 10, 18, TEXT, "Helvetica-Bold")
    _draw_wrapped(c, settings.address, margin + 38, y - 28, 260, 9, SLATE, max_lines=3, leading=11)
    _draw_right(c, "RECEIPT", right, y - 8, 28, TEXT, "Helvetica-Bold")
    _draw_right(c, receipt_number, right, y - 30, 10, SLATE, "Helvetica-Bold")

    y -= 112
    c.setFillColor(DARK)
    c.rect(margin, y - 58, right - margin, 58, fill=1, stroke=0)
    _draw_text(c, "PAYMENT RECEIVED", margin + 18, y - 21, 9, PRIMARY, "Helvetica-Bold")
    _draw_text(c, _money(settings, payment.amount), margin + 18, y - 45, 22, colors.white, "Helvetica-Bold")
    _draw_right(c, _date(payment.payment_date), right - 18, y - 34, 11, colors.white, "Helvetica-Bold")

    y -= 88
    rows = [
        ("Guest", booking.guest.full_name),
    ]
    if booking.vehicle_registration:
        rows.append(("Vehicle number plate", booking.vehicle_registration))
    rows.extend([
        ("Booking reference", booking.booking_reference),
        ("Room", booking.room.name),
        ("Payment type", payment.payment_type),
        ("Payment method", payment.payment_method),
        ("Payment reference", payment.reference or "-"),
        ("Balance after payment", _money(settings, balance_after_payment)),
    ])
    row_h = 28
    for label, value in rows:
        c.setStrokeColor(BORDER)
        c.rect(margin, y - row_h, right - margin, row_h, fill=0, stroke=1)
        _draw_text(c, label, margin + 14, y - 18, 9, SLATE)
        _draw_text(c, value, margin + 170, y - 18, 10, TEXT, "Helvetica-Bold")
        y -= row_h

    if payment.notes or settings.receipt_notes:
        y -= 20
        text = payment.notes or settings.receipt_notes
        c.setStrokeColor(BORDER)
        c.rect(margin, y - 78, right - margin, 78, fill=0, stroke=1)
        _draw_text(c, "NOTES", margin + 12, y - 16, 8, PRIMARY, "Helvetica-Bold")
        _draw_wrapped(c, text, margin + 12, y - 34, right - margin - 24, 9, SLATE, leading=12, max_lines=4)

    _draw_text(c, "Thank you. Generated by Circle Core Guest House.", margin, 22 * mm, 8, SLATE)
    c.save()
    return _response(buffer, f"{booking.booking_reference}-receipt-{payment.pk}.pdf")


