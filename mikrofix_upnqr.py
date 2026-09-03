"""UPN QR plačilni nalog po tehničnem standardu ZBS (brez bančne povezave)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


class UpnQrError(RuntimeError):
    pass


def _latin2(value: object, maximum: int) -> str:
    text = " ".join(str(value or "").strip().split())[:maximum]
    try:
        text.encode("iso-8859-2")
    except UnicodeEncodeError as exc:
        raise UpnQrError(f"Besedilo vsebuje znak, ki ga UPN QR ne podpira: {text}") from exc
    return text


def normalize_iban(value: object) -> str:
    iban = "".join(str(value or "").upper().split())
    if len(iban) != 19 or not iban.startswith("SI56") or not iban[4:].isdigit():
        raise UpnQrError("Prejemnikov IBAN mora biti veljaven slovenski račun oblike SI56 + 15 številk.")
    return iban


def build_upnqr_payload(
    *, amount: object, recipient_iban: str, recipient_reference: str,
    recipient_name: str, recipient_street: str, recipient_city: str,
    purpose: str, payer_name: str = "", payer_street: str = "",
    payer_city: str = "", purpose_code: str = "OTHR",
    due_date: date | None = None,
) -> bytes:
    cents = int(
        (Decimal(str(amount).replace(",", ".")) * 100).quantize(
            Decimal("1"), ROUND_HALF_UP
        )
    )
    if cents <= 0 or cents > 99_999_999_999:
        raise UpnQrError("UPN QR znesek mora biti pozitiven in največ 999.999.999,99 EUR.")
    code = str(purpose_code or "OTHR").upper().strip()
    if len(code) != 4 or not code.isalpha():
        raise UpnQrError("Koda namena mora imeti štiri črke.")
    fields = [
        "UPNQR", "", "", "", "",
        _latin2(payer_name, 33), _latin2(payer_street, 33), _latin2(payer_city, 33),
        f"{cents:011d}", "", "", code, _latin2(purpose, 42),
        (due_date or date.today()).strftime("%d.%m.%Y"),
        normalize_iban(recipient_iban), _latin2(recipient_reference, 26),
        _latin2(recipient_name, 33), _latin2(recipient_street, 33),
        _latin2(recipient_city, 33),
    ]
    first = "".join(f"{field}\n" for field in fields).encode("iso-8859-2")
    payload = first + f"{len(first):03d}\n".encode("ascii")
    if len(payload) > 411:
        raise UpnQrError("Podatki UPN QR so predolgi.")
    return payload


def generate_upnqr_pdf(
    *, output_dir: Path | str, amount: object, reference: str,
    payer: dict[str, str], company: dict[str, str], paper_width_mm: int = 80,
) -> Path:
    try:
        import segno
    except Exception as exc:
        raise UpnQrError(
            "Za UPN QR manjka komponenta segno. Zaženi namestitev dodatkov v30."
        ) from exc
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    payload = build_upnqr_payload(
        amount=amount,
        recipient_iban=str(company.get("iban") or ""),
        recipient_reference=reference,
        recipient_name=str(company.get("legal_name") or company.get("name") or "MikroFix"),
        recipient_street=str(company.get("street") or "Paradiž 73"),
        recipient_city=str(company.get("city") or "2282 Cirkulane"),
        payer_name=str(payer.get("name") or ""),
        payer_street=str(payer.get("address") or ""),
        payer_city="",
        purpose=f"Plačilo računa {reference}",
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    safe_ref = "".join(ch for ch in reference if ch.isalnum())
    png = output / f"UPN-{safe_ref}.png"
    pdf = output / f"UPN-{safe_ref}_{int(paper_width_mm)}mm.pdf"
    qr = segno.make_qr(
        payload.decode("iso-8859-2"), version=15, error="m", mode="byte",
        encoding="iso-8859-2",
        eci=True, boost_error=False,
    )
    qr.save(str(png), kind="png", scale=8, border=4, dark="black", light="white")

    width = int(paper_width_mm) * mm
    height = 150 * mm
    c = canvas.Canvas(str(pdf), pagesize=(width, height))
    y = height - 8 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(width / 2, y, "UPN QR – PREDLOG PLAČILA")
    y -= 8 * mm
    c.drawImage(str(png), (width - 58 * mm) / 2, y - 58 * mm, 58 * mm, 58 * mm)
    y -= 65 * mm
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(width / 2, y, f"{Decimal(str(amount)):.2f} EUR")
    y -= 7 * mm
    c.setFont("Helvetica", 8)
    for label, value in (
        ("Prejemnik", company.get("legal_name") or company.get("name") or "MikroFix"),
        ("IBAN", company.get("iban") or ""), ("Sklic", reference),
    ):
        c.drawString(5 * mm, y, f"{label}: {value}")
        y -= 5 * mm
    c.setFont("Helvetica", 7)
    c.drawString(5 * mm, y - 2 * mm, "Ta izpis ni račun. Znesek mora biti plačan natančno do centa.")
    c.save()
    try:
        png.unlink()
    except OSError:
        pass
    return pdf
