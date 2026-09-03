"""Tiskalniško neodvisen 80-mm TESTNI izpis MikroFIX."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


def _safe(value: object) -> str:
    return escape(str(value or "-")).replace("\n", "<br/>")


def _register_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = (
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        if path.exists():
            try:
                pdfmetrics.registerFont(TTFont("MikroFIXReceipt", str(path)))
                return "MikroFIXReceipt"
            except Exception:
                pass
    return "Helvetica"


def generate_test_receipt(
    output_path: Path,
    *,
    company: dict[str, str],
    document: dict[str, object],
    items: list[dict[str, object]],
    premise: str,
    device: str,
    tax_note: str,
    paper_width_mm: int = 80,
) -> Path:
    """Ustvari jasno označen nefiskalni PDF za preizkus 80-mm postavitve."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()

    estimated_item_mm = sum(
        8 + max(0, len(str(item.get("name", ""))) - 24) // 24 * 4
        for item in items
    )
    page_height_mm = max(155, 130 + estimated_item_mm)

    pdf = SimpleDocTemplate(
        str(output_path),
        pagesize=(paper_width_mm * mm, page_height_mm * mm),
        leftMargin=4 * mm,
        rightMargin=4 * mm,
        topMargin=4 * mm,
        bottomMargin=4 * mm,
    )
    styles = getSampleStyleSheet()
    center = ParagraphStyle(
        "ReceiptCenter",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=7.5,
        leading=9.5,
        alignment=1,
    )
    body = ParagraphStyle(
        "ReceiptBody",
        parent=center,
        alignment=0,
    )
    small = ParagraphStyle(
        "ReceiptSmall",
        parent=center,
        fontSize=6.5,
        leading=8,
    )
    warning = ParagraphStyle(
        "ReceiptWarning",
        parent=center,
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#A63A3A"),
    )

    story = [
        Paragraph(f"<b>{_safe(company.get('name'))}</b>", center),
        Paragraph(_safe(company.get("address")), small),
        Paragraph(_safe(company.get("phone")), small),
        Paragraph(_safe(company.get("email")), small),
        Spacer(1, 2 * mm),
        Paragraph("<b>TESTNI IZPIS - NI FISKALNI RAČUN</b>", warning),
        Spacer(1, 2 * mm),
        Paragraph(
            f"<b>{_safe(document.get('type'))}</b><br/>"
            f"{_safe(document.get('number'))}",
            center,
        ),
        Paragraph(
            f"Datum: {_safe(document.get('date'))}<br/>"
            f"Kupec: {_safe(document.get('partner'))}",
            body,
        ),
        Spacer(1, 2 * mm),
    ]

    rows = [["Artikel", "Kol.", "Znesek"]]
    net_total = 0.0
    vat_total = 0.0
    for item in items:
        qty = float(item.get("qty") or 0)
        line_net = float(item.get("line_net") or 0)
        line_vat = float(item.get("line_vat") or 0)
        net_total += line_net
        vat_total += line_vat
        rows.append([
            Paragraph(_safe(item.get("name")), small),
            f"{qty:.2f}",
            f"{line_net + line_vat:.2f}",
        ])

    item_table = Table(rows, colWidths=[29 * mm, 7 * mm, 14 * mm], repeatRows=1)
    item_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 6.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
        ("LINEBELOW", (0, -1), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ]))
    story.append(item_table)

    delivery = float(document.get("delivery") or 0)
    grand_total = net_total + vat_total + delivery
    totals = Table([
        ["Dostava", f"{delivery:.2f} EUR"],
        ["SKUPAJ", f"{grand_total:.2f} EUR"],
    ], colWidths=[27 * mm, 23 * mm])
    totals.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [Spacer(1, 2 * mm), totals, Spacer(1, 2 * mm)]

    if tax_note:
        story.append(Paragraph(_safe(tax_note), small))
    story += [
        Spacer(1, 2 * mm),
        Paragraph(f"Oznaka TEST: {_safe(premise)}/{_safe(device)}", small),
        Paragraph("ZOI/EOR: NI USTVARJEN - TEST", warning),
        Paragraph("QR bo dodan po uspešni FURS testni integraciji.", small),
        Spacer(1, 2 * mm),
        Paragraph("Hvala.", center),
    ]

    pdf.build(story)
    return output_path
