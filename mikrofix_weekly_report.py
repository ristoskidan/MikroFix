"""Zasebno tedensko poročilo odkupnih cen MikroFIX.

Poročilo uporablja samo sveže, sledljive javne ponudbe. Ne pošilja e-pošte,
ne objavlja cen in nikoli ne izvede odkupa ali spremeni drugega cenika.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


FORMULA_VERSION = "MF-ODKUP-1.0.1"
MAX_PAGE_BYTES = 3_000_000


class WeeklyReportError(RuntimeError):
    """Pričakovana napaka vhodnih podatkov ali izdelave poročila."""


@dataclass(frozen=True)
class CalculatedRow:
    model: str
    storage: str
    reference_eur: float | None
    refurbishment_eur: float | None
    recommended_eur: float | None
    maximum_eur: float | None
    source_count: int
    status: str
    detail: str
    audit: dict[str, Any]


def _float(value: Any, label: str, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: manjka veljavno število.") from exc
    if not math.isfinite(parsed) or parsed < minimum:
        raise ValueError(f"{label}: vrednost mora biti najmanj {minimum:.2f} EUR.")
    return parsed


def _parse_date(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: datum mora biti YYYY-MM-DD.") from exc


def _https_url(value: Any, allowed_hosts: set[str] | None = None) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not host:
        raise ValueError("Vir mora uporabljati veljaven HTTPS naslov.")
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError(f"Gostitelj {host} ni na dovoljenem seznamu.")
    return url


def clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def floor_to_step(value: float, step: float) -> float:
    if value <= 0:
        return 0.0
    return float(math.floor((value + 1e-9) / step) * step)


def round_half_up_to_step(value: float, step: float) -> float:
    if value <= 0:
        return 0.0
    return float(math.floor((value + step / 2.0 + 1e-9) / step) * step)


def week_bounds(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _policy(settings: dict[str, Any]) -> dict[str, float | int | bool]:
    return {
        "source_max_age_days": int(settings.get("source_max_age_days", 8)),
        "min_market_sources": int(settings.get("min_market_sources", 2)),
        "room_pct": float(settings.get("room_pct", 0.20)),
        "room_min_eur": float(settings.get("room_min_eur", 40.0)),
        "room_max_eur": float(settings.get("room_max_eur", 150.0)),
        "internal_refurb_labor_eur": float(
            settings.get("internal_refurb_labor_eur", 20.0)
        ),
        "general_material_eur": float(settings.get("general_material_eur", 1.25)),
        "package_eur": float(settings.get("package_eur", 30.0)),
        "haggle_pct": float(settings.get("haggle_pct", 0.10)),
        "haggle_min_eur": float(settings.get("haggle_min_eur", 10.0)),
        "haggle_max_eur": float(settings.get("haggle_max_eur", 50.0)),
        "round_step_eur": float(settings.get("round_step_eur", 10.0)),
    }


def validate_policy(settings: dict[str, Any]) -> None:
    p = _policy(settings)
    if not 1 <= p["source_max_age_days"] <= 31:
        raise ValueError("Tedenski viri morajo biti stari največ 1 do 31 dni.")
    if not 2 <= p["min_market_sources"] <= 5:
        raise ValueError("Za referenco sta potrebna najmanj 2 in največ 5 virov.")
    if not 0.0 < p["room_pct"] < 1.0:
        raise ValueError("Varnostni odstotek mora biti med 0 in 1.")
    if p["room_min_eur"] > p["room_max_eur"]:
        raise ValueError("Najnižji varnostni prostor presega najvišjega.")
    if p["haggle_min_eur"] > p["haggle_max_eur"]:
        raise ValueError("Najnižja pogajalska rezerva presega najvišjo.")
    if p["round_step_eur"] <= 0:
        raise ValueError("Korak zaokroževanja mora biti pozitiven.")


def calculate_entry(
    entry: dict[str, Any],
    settings: dict[str, Any],
    *,
    as_of: date | None = None,
) -> CalculatedRow:
    as_of = as_of or date.today()
    p = _policy(settings)
    model = str(entry.get("model") or "").strip()
    storage = str(entry.get("storage") or "").strip()
    label = " ".join(value for value in (model, storage) if value) or "Neznan model"
    base_audit: dict[str, Any] = {
        "model": model,
        "storage": storage,
        "as_of": as_of.isoformat(),
        "formula_version": FORMULA_VERSION,
    }
    try:
        if not model or not storage:
            raise ValueError("Manjka model ali kapaciteta.")

        cutoff = as_of - timedelta(days=int(p["source_max_age_days"]))
        offers: list[dict[str, Any]] = []
        seen_sellers: set[str] = set()
        for source in entry.get("market_offers") or []:
            seller = str(source.get("seller") or "").strip()
            if not seller or seller.casefold() in seen_sellers:
                continue
            observed = _parse_date(source.get("observed_on"), f"{label}, {seller}")
            if observed > as_of or observed < cutoff:
                continue
            if int(source.get("warranty_months") or 0) < 12:
                continue
            price = _float(source.get("price_eur"), f"{label}, {seller}", minimum=1.0)
            url = _https_url(source.get("url"))
            offers.append({
                "seller": seller,
                "price_eur": round(price, 2),
                "url": url,
                "observed_on": observed.isoformat(),
                "warranty_months": int(source.get("warranty_months") or 0),
            })
            seen_sellers.add(seller.casefold())

        if len(offers) < int(p["min_market_sources"]):
            raise ValueError(
                f"Premalo svežih primerljivih virov ({len(offers)}/"
                f"{int(p['min_market_sources'])})."
            )

        battery = entry.get("battery") or {}
        battery_observed = _parse_date(battery.get("observed_on"), f"{label}, baterija")
        if battery_observed > as_of or battery_observed < cutoff:
            raise ValueError("Javna cena baterije je zastarela.")
        battery_url = _https_url(battery.get("url"))
        battery_eur = _float(battery.get("cost_eur"), f"{label}, baterija", minimum=0.01)
        seal_eur = _float(
            entry.get("seal_material_eur"), f"{label}, tesnilo/material", minimum=0.0
        )

        reference = float(statistics.median(row["price_eur"] for row in offers))
        refurbishment = (
            battery_eur
            + float(p["internal_refurb_labor_eur"])
            + seal_eur
            + float(p["general_material_eur"])
            + float(p["package_eur"])
        )
        room = clamp(
            reference * float(p["room_pct"]),
            float(p["room_min_eur"]),
            float(p["room_max_eur"]),
        )
        raw_maximum = max(0.0, reference - refurbishment - room)
        maximum = floor_to_step(raw_maximum, float(p["round_step_eur"]))
        if maximum <= 0:
            haggle = 0.0
        else:
            haggle = round_half_up_to_step(
                clamp(
                    maximum * float(p["haggle_pct"]),
                    float(p["haggle_min_eur"]),
                    float(p["haggle_max_eur"]),
                ),
                float(p["round_step_eur"]),
            )
        recommended = max(0.0, maximum - haggle)

        base_audit.update({
            "market_offers": offers,
            "reference_eur": round(reference, 2),
            "battery": {
                "cost_eur": round(battery_eur, 2),
                "url": battery_url,
                "observed_on": battery_observed.isoformat(),
            },
            "seal_material_eur": round(seal_eur, 2),
            "internal_refurb_labor_eur": float(p["internal_refurb_labor_eur"]),
            "general_material_eur": float(p["general_material_eur"]),
            "package_eur": float(p["package_eur"]),
            "refurbishment_eur": round(refurbishment, 2),
            "room_eur": round(room, 2),
            "raw_maximum_eur": round(raw_maximum, 2),
            "haggle_eur": round(haggle, 2),
            "maximum_eur": round(maximum, 2),
            "recommended_eur": round(recommended, 2),
        })
        return CalculatedRow(
            model=model,
            storage=storage,
            reference_eur=round(reference, 2),
            refurbishment_eur=round(refurbishment, 2),
            recommended_eur=round(recommended, 2),
            maximum_eur=round(maximum, 2),
            source_count=len(offers),
            status="OK",
            detail="",
            audit=base_audit,
        )
    except ValueError as exc:
        base_audit["error"] = str(exc)
        return CalculatedRow(
            model=model or "Neznan model",
            storage=storage or "-",
            reference_eur=None,
            refurbishment_eur=None,
            recommended_eur=None,
            maximum_eur=None,
            source_count=0,
            status="N/A",
            detail=str(exc),
            audit=base_audit,
        )


def load_inputs(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WeeklyReportError("Manjka datoteka vhodnih cen tedenskega poročila.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WeeklyReportError(f"Vhodnih cen ni mogoče prebrati: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
        raise WeeklyReportError("Nepodprta oblika vhodnih cen tedenskega poročila.")
    return payload


def save_inputs(path: Path, payload: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return path


def ensure_inputs(path: Path, seed_path: Path) -> Path:
    path = Path(path)
    if path.exists():
        payload = load_inputs(path)
        seed = load_inputs(seed_path)
        existing = {
            (
                str(entry.get("model") or "").strip().casefold(),
                str(entry.get("storage") or "").strip().casefold(),
            )
            for entry in payload["entries"]
        }
        additions = [
            entry for entry in seed["entries"]
            if (
                str(entry.get("model") or "").strip().casefold(),
                str(entry.get("storage") or "").strip().casefold(),
            ) not in existing
        ]
        if additions:
            # Novi modeli iz kataloga se dodajo, že osvežene oziroma ročno
            # preverjene cene uporabnika pa se nikoli ne prepišejo.
            payload["entries"].extend(additions)
            payload["catalog_merged_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            save_inputs(path, payload)
        return path
    payload = load_inputs(seed_path)
    return save_inputs(path, payload)


def evaluate_inputs(
    payload: dict[str, Any],
    settings: dict[str, Any],
    *,
    as_of: date | None = None,
) -> list[CalculatedRow]:
    validate_policy(settings)
    return [calculate_entry(entry, settings, as_of=as_of) for entry in payload["entries"]]


def _extract_json_ld(document: str) -> list[Any]:
    blocks = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result: list[Any] = []
    for block in blocks:
        try:
            result.append(json.loads(html.unescape(block).strip()))
        except json.JSONDecodeError:
            continue
    return result


def parse_swappie_prices(document: str) -> dict[str, float]:
    prices: dict[str, float] = {}
    for payload in _extract_json_ld(document):
        graph = payload.get("@graph", []) if isinstance(payload, dict) else []
        for node in graph:
            if not isinstance(node, dict) or node.get("@type") != "ItemList":
                continue
            for element in node.get("itemListElement", []):
                product = element.get("item", {}) if isinstance(element, dict) else {}
                offer = product.get("offers", {}) if isinstance(product, dict) else {}
                name = str(product.get("name") or "").strip()
                try:
                    price = float(offer.get("price"))
                except (TypeError, ValueError):
                    continue
                if name and offer.get("priceCurrency") == "EUR" and price > 0:
                    prices[name] = round(price, 2)
    if not prices:
        raise WeeklyReportError("Swappie stran nima pričakovanega javnega cenika.")
    return prices


def parse_refurbed_price(document: str, storage: str) -> float:
    prices: list[float] = []
    for payload in _extract_json_ld(document):
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "ProductGroup":
                continue
            for variant in node.get("hasVariant", []):
                if str(variant.get("size") or "").strip() != storage:
                    continue
                offer = variant.get("offers", {})
                if not isinstance(offer, dict):
                    continue
                availability = str(offer.get("availability") or "")
                if availability and not availability.endswith("InStock"):
                    continue
                try:
                    price = float(offer.get("price"))
                except (TypeError, ValueError):
                    continue
                if offer.get("priceCurrency") == "EUR" and price > 0:
                    prices.append(price)
    if not prices:
        raise WeeklyReportError(
            f"Refurbed nima pričakovane ponudbe za kapaciteto {storage}."
        )
    return round(min(prices), 2)


def parse_fixshop_price(document: str) -> float:
    match = re.search(
        r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([0-9.,]+)["\']',
        document,
        flags=re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r'<meta[^>]+content=["\']([0-9.,]+)["\'][^>]+property=["\']product:price:amount["\']',
            document,
            flags=re.IGNORECASE,
        )
    if match is None:
        raise WeeklyReportError("FixShop stran nima pričakovane javne cene.")
    try:
        price = float(match.group(1).replace(",", "."))
    except ValueError as exc:
        raise WeeklyReportError("FixShop cena ni veljavno število.") from exc
    if not 0.01 <= price <= 2_000:
        raise WeeklyReportError("FixShop cena je zunaj varnega obsega.")
    return round(price, 2)


def _fetch_text(url: str, *, timeout: float, user_agent: str) -> str:
    allowed = {"swappie.com", "www.swappie.com", "refurbed.si", "www.refurbed.si", "fixshop.si", "www.fixshop.si"}
    safe_url = _https_url(url, allowed)
    request = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "sl,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type") or "")
        if "text/html" not in content_type.lower():
            raise WeeklyReportError("Vir ni vrnil spletne strani HTML.")
        body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise WeeklyReportError("Spletni vir je nepričakovano velik.")
        charset = response.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")


def refresh_public_prices(
    path: Path,
    *,
    timeout: float = 15.0,
    user_agent: str = "MikroFIX/28 TEST weekly-price-check",
    as_of: date | None = None,
    progress: Callable[[int, int], None] | None = None,
    fetcher: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Osveži javne tržne in baterijske cene, napake pa izolira po virih."""
    as_of = as_of or date.today()
    payload = load_inputs(path)
    fetch = fetcher or _fetch_text
    shared_cache: dict[str, str] = {}
    jobs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for entry in payload["entries"]:
        for offer in entry.get("market_offers") or []:
            jobs.append(("market", entry, offer))
        battery = entry.get("battery") or {}
        if battery.get("url"):
            jobs.append(("battery", entry, battery))

    requested = len(jobs)
    succeeded = failed = 0
    errors: list[str] = []
    for index, (job_type, entry, source) in enumerate(jobs, 1):
        model = str(entry.get("model") or "Neznan model")
        url = str(source.get("url") or "")
        try:
            if url not in shared_cache:
                shared_cache[url] = fetch(url, timeout=timeout, user_agent=user_agent)
            document = shared_cache[url]
            kind = str(source.get("kind") or "")
            if job_type == "battery":
                if kind != "fixshop_product":
                    raise WeeklyReportError("Neznan tip baterijskega vira.")
                source["cost_eur"] = parse_fixshop_price(document)
            elif kind == "swappie_model":
                prices = parse_swappie_prices(document)
                source["price_eur"] = prices[model]
            elif kind == "refurbed_storage":
                source["price_eur"] = parse_refurbed_price(
                    document, str(entry.get("storage") or "")
                )
            else:
                raise WeeklyReportError("Neznan tip tržnega vira.")
            source["observed_on"] = as_of.isoformat()
            source.pop("last_error", None)
            succeeded += 1
        except Exception as exc:
            failed += 1
            source["last_error"] = str(exc)[:300]
            errors.append(f"{model}: {exc}")
        if progress:
            progress(index, requested)

    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["last_refresh"] = {
        "date": as_of.isoformat(),
        "requested": requested,
        "succeeded": succeeded,
        "failed": failed,
    }
    save_inputs(path, payload)
    return {
        "requested": requested,
        "succeeded": succeeded,
        "failed": failed,
        "errors": errors,
    }


def _font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                pdfmetrics.registerFont(TTFont("MikroFIXWeeklyFont", str(candidate)))
                return "MikroFIXWeeklyFont"
            except Exception:
                continue
    return "Helvetica"


def latest_report(reports_dir: Path) -> Path | None:
    files = sorted(Path(reports_dir).glob("MikroFIX_odkupne_cene_*.pdf"))
    return files[-1] if files else None


def report_for_week(reports_dir: Path, day: date) -> Path | None:
    files = sorted(Path(reports_dir).glob(f"MikroFIX_odkupne_cene_{week_key(day)}*.pdf"))
    return files[-1] if files else None


def generate_pdf(
    input_path: Path,
    reports_dir: Path,
    settings: dict[str, Any],
    *,
    as_of: date | None = None,
    generated_at: datetime | None = None,
    protect: Callable[[Path], None] | None = None,
) -> tuple[Path, list[CalculatedRow]]:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise WeeklyReportError(
            "Manjka ReportLab. Zaženi namesti_v28.bat iz MikroFIX mape."
        ) from exc

    as_of = as_of or date.today()
    generated_at = generated_at or datetime.now().astimezone()
    payload = load_inputs(input_path)
    rows = evaluate_inputs(payload, settings, as_of=as_of)
    valid = [row for row in rows if row.status == "OK"]
    if not valid:
        raise WeeklyReportError(
            "PDF ni ustvarjen: ni niti ene vrstice z dovolj svežimi in popolnimi podatki."
        )

    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    input_hash = hashlib.sha256(Path(input_path).read_bytes()).hexdigest()
    base = reports_dir / f"MikroFIX_odkupne_cene_{week_key(as_of)}.pdf"
    output = base
    existing = sorted(reports_dir.glob(f"{base.stem}*.audit.json"))
    for existing_audit in existing:
        try:
            old = json.loads(existing_audit.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        matching_pdf = Path(str(existing_audit).replace(".audit.json", ".pdf"))
        if (
            old.get("input_sha256") == input_hash
            and old.get("formula_version") == FORMULA_VERSION
            and matching_pdf.exists()
        ):
            return matching_pdf, rows
    if base.exists():
        revision = 2
        while True:
            candidate = reports_dir / f"{base.stem}_R{revision}.pdf"
            if not candidate.exists():
                output = candidate
                break
            revision += 1
    audit_path = output.with_suffix(".audit.json")
    font = _font()
    monday, sunday = week_bounds(as_of)

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "WeeklyTitle",
        parent=styles["Title"],
        fontName=font,
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#23679F"),
        spaceAfter=3 * mm,
    )
    body = ParagraphStyle(
        "WeeklyBody", parent=styles["BodyText"], fontName=font, fontSize=8.5, leading=11
    )
    note = ParagraphStyle(
        "WeeklyNote", parent=body, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#505A63")
    )
    document = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"MikroFIX odkupne cene {week_key(as_of)}",
        author="MikroFIX",
    )
    story = [
        Paragraph("MikroFIX - tedenski pregled odkupnih cen", title),
        Paragraph(
            "<b>ZA INTERNO UPORABO • NI JAVNI CENIK • NE POMENI SAMODEJNEGA ODKUPA</b>",
            ParagraphStyle(
                "Private", parent=body, textColor=colors.HexColor("#A63A3A"), fontSize=9.5, leading=12
            ),
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            f"Obdobje: {monday.strftime('%d.%m.%Y')}-{sunday.strftime('%d.%m.%Y')} &nbsp;&nbsp; "
            f"Izdelano: {generated_at.strftime('%d.%m.%Y %H:%M')} &nbsp;&nbsp; "
            f"Formula: {FORMULA_VERSION}",
            body,
        ),
        Spacer(1, 5 * mm),
    ]

    table_rows: list[list[Any]] = [["MODEL", "PRIPOROČEN ODKUP", "MAX ODKUP"]]
    for row in rows:
        name = f"{row.model} {row.storage}".strip()
        if row.status == "OK":
            recommended = f"{row.recommended_eur:.0f} €"
            maximum = f"{row.maximum_eur:.0f} €"
        else:
            recommended = maximum = "N/A"
        table_rows.append([name, recommended, maximum])

    table = Table(table_rows, colWidths=[88 * mm, 45 * mm, 45 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF1")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#2E3439")),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D3D9DE")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9FA")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([
        table,
        Spacer(1, 6 * mm),
        Paragraph(
            "Cene veljajo za funkcionalne naprave brez Activation Lock, napake na matični "
            "plošči ali hujše fizične poškodbe. Obrabljena baterija in običajni kozmetični "
            "znaki so dovoljeni.",
            note,
        ),
        Spacer(1, 2 * mm),
        Paragraph(
            "MAX odkup je fiksna samodejna referenčna meja za interno uporabo in se ročno "
            "ne prilagaja. Pred odkupom je obvezen dejanski pregled naprave.",
            note,
        ),
    ])
    document.build(story)

    audit_payload = {
        "schema_version": 1,
        "formula_version": FORMULA_VERSION,
        "week": week_key(as_of),
        "period": {"from": monday.isoformat(), "to": sunday.isoformat()},
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "input_file": str(Path(input_path).resolve()),
        "input_sha256": input_hash,
        "policy": _policy(settings),
        "valid_rows": len(valid),
        "invalid_rows": len(rows) - len(valid),
        "rows": [row.audit for row in rows],
    }
    audit_path.write_text(
        json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if protect:
        protect(output)
        protect(audit_path)
    return output, rows
