"""Print harmless production-readiness pages and record physical confirmation."""

from __future__ import annotations

import json
import os
import tempfile
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from mikrofix_printing import (
    preferred_a4_printer,
    preferred_receipt_printer,
    send_pdf_to_printer,
)
from mikrofix_v30_pos import PosLine, generate_pos_preview_pdfs


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "mikrofix_settings.json"


def _a4_test(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    bold_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if not font_path.is_file() or not bold_path.is_file():
        raise RuntimeError("Za slovenske znake manjka pisava DejaVu Sans.")
    pdfmetrics.registerFont(TTFont("MikroFIXA4", str(font_path)))
    pdfmetrics.registerFont(TTFont("MikroFIXA4Bold", str(bold_path)))
    styles = getSampleStyleSheet()
    styles["Title"].fontName = "MikroFIXA4Bold"
    styles["Heading2"].fontName = "MikroFIXA4Bold"
    styles["BodyText"].fontName = "MikroFIXA4"
    document = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=25 * mm, bottomMargin=25 * mm,
    )
    document.build([
        Paragraph("MikroFIX – TEST TISKALNIKA A4", styles["Title"]),
        Spacer(1, 12 * mm),
        Paragraph("To ni račun in ni poslovni dokument.", styles["Heading2"]),
        Spacer(1, 6 * mm),
        Paragraph(
            "Preveri, da je besedilo ostro, celotno in pravilno umeščeno na A4 list. "
            "Šumniki: č Č š Š ž Ž. Datum preizkusa: "
            + datetime.now().astimezone().isoformat(timespec="seconds"),
            styles["BodyText"],
        ),
    ])
    return path


def _record_confirmation(thermal: str, a4: str | None) -> None:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    printer = settings.setdefault("printer", {})
    printer["printer_name"] = thermal
    if a4:
        printer["a4_printer_name"] = a4
    readiness = settings.setdefault("production_readiness", {})
    readiness["thermal_print_confirmed"] = True
    if a4:
        readiness["a4_print_confirmed"] = True
    readiness["printer_tested_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)
    SETTINGS_PATH.chmod(0o600)


def _confirm(thermal: str, a4: str | None) -> bool:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("MikroFIX - potrditev tiskalnikov")
    root.resizable(False, False)
    thermal_ok = tk.BooleanVar(value=False)
    a4_ok = tk.BooleanVar(value=a4 is None)
    result = {"ok": False}
    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack()
    tk.Label(frame, text="Preveri oba natisnjena testna lista", font=("Sans", 13, "bold")).pack(anchor="w")
    tk.Checkbutton(
        frame, variable=thermal_ok,
        text=f"{thermal}: 80-mm izpis je cel, berljiv in pravilno odrezan",
    ).pack(anchor="w", pady=(14, 4))
    if a4:
        tk.Checkbutton(
            frame, variable=a4_ok,
            text=f"{a4}: A4 izpis je cel, oster in vsebuje č, š, ž",
        ).pack(anchor="w", pady=4)

    def accept() -> None:
        if not thermal_ok.get() or not a4_ok.get():
            messagebox.showerror("MikroFIX", "Potrdi oba izpisa ali preizkus prekliči.", parent=root)
            return
        result["ok"] = True
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.pack(anchor="e", pady=(14, 0))
    tk.Button(buttons, text="PREKLIČI", command=root.destroy).pack(side="right", padx=(8, 0))
    tk.Button(buttons, text="POTRDI OBA", command=accept).pack(side="right")
    root.mainloop()
    return bool(result["ok"])


def main() -> int:
    thermal_only = "--thermal-only" in sys.argv[1:]
    thermal = preferred_receipt_printer("TM-T20II")
    a4 = None if thermal_only else preferred_a4_printer("HP-LaserJet-1020")
    if not thermal or (not thermal_only and not a4):
        raise RuntimeError("Zahtevani tiskalnik ni dosegljiv.")
    with tempfile.TemporaryDirectory(prefix="mikrofix-printer-test-") as raw:
        folder = Path(raw)
        _, receipt = generate_pos_preview_pdfs(
            lines=[
                PosLine(
                    line_kind="ARTICLE", name="Zaščitno steklo – šumniki č Č š Š ž Ž",
                    quantity=Decimal("1"), base_unit_price=Decimal("19.90"), acquisition_cost=Decimal("5"),
                ),
                PosLine(
                    line_kind="SERVICE", name="Diagnostika in popravilo naprave",
                    quantity=Decimal("1"), base_unit_price=Decimal("35.00"), service_tier="T1",
                ),
            ],
            payments={"CASH": Decimal("54.90")}, customer={"name": "NI STRANKE"},
            labor_floor=22, output_dir=folder,
            company={"short_name": "MikroFIX", "legal_name": "MikroFix Daniel Ristoski s.p."},
            premise="TEST", device="TEST", token="TISK-VECJA-PISAVA", paper_width_mm=80,
        )
        send_pdf_to_printer(receipt, thermal, timeout=30)
        if a4:
            page = _a4_test(folder / "mikrofix-test-a4.pdf")
            send_pdf_to_printer(page, a4, timeout=30)
        if not _confirm(thermal, a4):
            return 1
    _record_confirmation(thermal, a4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
