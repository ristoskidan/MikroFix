"""Avtomatski E2E pregled na začasni kopiji pripravljene TEST baze."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
os.environ["MIKROFIX_CONFIG"] = str(APP_DIR / "mikrofix_settings_e2e_test.json")
os.environ.pop("MIKROFIX_ALLOW_FURS_PRODUCTION", None)
os.environ["MIKROFIX_E2E_TEST"] = "YES"

from mikrofix_invoice_bridge import load_service_invoice_seed  # noqa: E402
from mikrofix_runtime import load_runtime  # noqa: E402
from mikrofix_v30_pos import (  # noqa: E402
    PosLine,
    calculate_line,
    generate_pos_pdfs,
    issue_invoice,
    mark_printed,
    prepare_used_goods_agreement,
    set_used_goods_agreement_signed,
)
from mikrofix_v30_cash import (  # noqa: E402
    cash_session_snapshot,close_cash_session,generate_cash_journal_pdf,
    open_cash_session,
)
from pripravi_v30_e2e_test import LONG_ARTICLE_NAME, verify  # noqa: E402


RUNTIME = load_runtime(APP_DIR)


def article(connection: sqlite3.Connection, where: str, value: object) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM inventory_items WHERE " + where, (value,)
    ).fetchone()
    if not row:
        raise RuntimeError(f"TEST artikel ni najden: {where}={value}")
    return row


def article_line(
    row: sqlite3.Row, *, discount: str = "0", consumed: bool = False,
    origin_kind: str = "POS_ADDED", origin_id: int | None = None,
    source_locked: bool = False,
) -> PosLine:
    return PosLine(
        line_kind="ARTICLE", name=str(row["article_name"]),
        code=str(row["internal_code"] or ""), barcode=str(row["barcode"] or ""),
        article_id=int(row["id"]), quantity=Decimal("1"),
        base_unit_price=Decimal(str(row["price"])),
        acquisition_cost=Decimal(str(row["acquisition_cost"] or 0)),
        requested_discount_pct=Decimal(discount), stock_already_consumed=consumed,
        origin_kind=origin_kind, origin_id=origin_id, source_locked=source_locked,
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    baseline = verify()
    if baseline["production_cert_path"] or baseline["furs_enabled"]:
        raise RuntimeError("Produkcijska varovalka E2E ni zaprta.")

    with tempfile.TemporaryDirectory(prefix="mikrofix-e2e-") as raw:
        root = Path(raw)
        office = root / "office.db"
        inventory = root / "inventory.db"
        service = root / "service.db"
        shutil.copy2(RUNTIME.db_path("office"), office)
        shutil.copy2(RUNTIME.db_path("inventory"), inventory)
        shutil.copy2(RUNTIME.db_path("service"), service)
        pdf_dir = root / "pdf"
        open_cash_session(
            office,premise_id="MFD",device_id="PIS",operator_code="DR",opening_cash=500
        )
        open_cash_session(
            office,premise_id="MFT",device_id="PIS",operator_code="DR",opening_cash=500
        )

        inv = sqlite3.connect(inventory)
        long_item = article(inv, "barcode=?", "TEST-E2E-LONG-0001")
        long_line = article_line(long_item, discount="15")
        inv.close()
        # Uporabi isto varovalko cene kot blagajna. Pri artiklu lahko
        # nabavna cena + 5 % omeji zahtevani rabat.
        long_total = calculate_line(long_line, labor_floor=22).line_total
        long_id, long_no, total = issue_invoice(
            office_db=office, inventory_db=inventory, service_db=service,
            lines=[long_line], payments={"CASH": long_total},
            customer={
                "name": "[TEST E2E] Kupec z dolgim nazivom računa",
                "address": "Preizkusna ulica 22, 2000 Maribor",
                "tax_number": "12345678", "phone": "040 000 222",
                "email": "kupec@example.invalid",
                "notes": "[TEST E2E] Preverjanje vseh polj kupca in dolge postavke.",
            },
            operator_code="DR", operator_tax_number="84514841",
            premise_id="MFD", electronic_device_id="PIS", labor_floor=22,
            issue_token="E2E-AUTO-LONG",
        )
        if total != long_total:
            raise RuntimeError("Končni znesek dolge postavke ni pravilen.")
        a4, thermal = generate_pos_pdfs(
            office_db=office, document_id=long_id, output_dir=pdf_dir,
            company={
                **RUNTIME.settings["business"], "short_name": "MikroFix",
                "address": "Paradiž 73, 2282 Cirkulane",
            },
            tax_note=RUNTIME.tax_invoice_note, premise="MFD", device="PIS",
            paper_width_mm=80,
        )
        if not a4.is_file() or not thermal.is_file() or "_80mm" not in thermal.name:
            raise RuntimeError("A4 ali 80-mm TEST PDF ni bil pravilno ustvarjen.")
        if len(LONG_ARTICLE_NAME) < 180 or thermal.stat().st_size < 3000:
            raise RuntimeError("Dolgi naziv ni ustvaril uporabnega 80-mm PDF-ja.")
        mark_printed(office, long_id, "DR")

        service_connection = sqlite3.connect(service)
        ready_service_id = int(service_connection.execute(
            "SELECT id FROM service WHERE invoice_no IS NULL AND service_order_id IS NOT NULL ORDER BY id LIMIT 1"
        ).fetchone()[0])
        seed = load_service_invoice_seed(service_connection, ready_service_id, labor_floor_eur=22)
        service_connection.close()
        lines: list[PosLine] = []
        for manual_id, (kind, description, quantity, price) in zip(
            seed.manual_line_ids, seed.manual_lines
        ):
            tier = "T2" if str(kind).upper() == "T2" else "T1"
            lines.append(PosLine(
                line_kind="SERVICE", name=str(description), code=tier,
                service_tier=tier, quantity=Decimal(str(quantity)),
                base_unit_price=Decimal(str(price)),
                origin_kind="SERVICE_MANUAL", origin_id=int(manual_id),
                source_locked=True,
            ))
        inv = sqlite3.connect(inventory)
        for part_line_id, (article_id, _barcode, _name, _code, _price) in zip(
            seed.part_line_ids, seed.parts
        ):
            lines.append(article_line(
                article(inv, "id=?", article_id), consumed=True,
                origin_kind="SERVICE_PART", origin_id=int(part_line_id),
                source_locked=True,
            ))
        inv.close()
        service_total = sum(
            (calculate_line(line, labor_floor=22).line_total for line in lines),
            Decimal("0.00"),
        )
        service_invoice_id, service_no, _ = issue_invoice(
            office_db=office, inventory_db=inventory, service_db=service,
            lines=lines, payments={"CASH": service_total},
            customer={
                "name": seed.customer, "address": seed.customer_address,
                "phone": seed.customer_phone, "email": seed.customer_email,
            },
            operator_code="DR", operator_tax_number="84514841",
            premise_id="MFD", electronic_device_id="PIS", labor_floor=22,
            issue_token="E2E-AUTO-SERVICE", linked_service_id=ready_service_id,
        )
        mark_printed(office, service_invoice_id, "DR")
        check = sqlite3.connect(service)
        linked = check.execute(
            "SELECT invoice_no,office_invoice_id FROM service WHERE id=?", (ready_service_id,)
        ).fetchone()
        check.close()
        if linked != (service_no, service_invoice_id):
            raise RuntimeError("Servis ni bil enolično povezan z izdanim TEST računom.")

        inv = sqlite3.connect(inventory)
        refurbished = article(inv, "source_kind=?", "BUYBACK_DEVICE")
        refurbished_line = article_line(refurbished)
        refurbished_article_id = int(refurbished["id"])
        inv.close()
        refurbished_total = refurbished_line.base_unit_price
        refurbished_customer = {
            "name": "[TEST E2E] Kupec obnovljene naprave",
            "address": "Garancijska ulica 12, 4000 Kranj",
            "phone": "040 000 444", "email": "obnovljeno@example.invalid",
        }
        agreement_id, _agreement_no, _agreement_pdf = prepare_used_goods_agreement(
            office_db=office, inventory_db=inventory,
            article_ids=[refurbished_article_id], customer=refurbished_customer,
            issue_token="E2E-AUTO-REFURB", output_dir=pdf_dir,
            company={
                **RUNTIME.settings["business"], "short_name":"MikroFix",
                "street":"Paradiž 73", "city":"2282 Cirkulane",
            }, operator_code="DR",
        )
        set_used_goods_agreement_signed(office, agreement_id, True)
        third_id, third_no, _ = issue_invoice(
            office_db=office, inventory_db=inventory, service_db=service,
            lines=[refurbished_line],
            payments={"CASH": Decimal("49"), "BANK_TRANSFER": refurbished_total - Decimal("49")},
            customer=refurbished_customer,
            operator_code="DR", operator_tax_number="84514841",
            premise_id="MFT", electronic_device_id="PIS", labor_floor=22,
            issue_token="E2E-AUTO-REFURB", used_goods_12m_signed=True,
            bank_transfer_received=True,
            used_goods_agreement_id=agreement_id,
        )
        mark_printed(office, third_id, "DR")

        for cash_premise in ("MFD","MFT"):
            cash_session,cash_entries=cash_session_snapshot(
                office,premise_id=cash_premise,device_id="PIS"
            )
            if not cash_session or not cash_entries:
                raise RuntimeError(f"Blagajniški dnevnik {cash_premise}/PIS ni zajel gotovine.")
            closed=close_cash_session(
                office,premise_id=cash_premise,device_id="PIS",operator_code="DR",
                counted_cash=cash_session["expected_cash"],note="[TEST E2E] Točno stanje",
            )
            journal_pdf=generate_cash_journal_pdf(
                office,session_id=closed["id"],output_dir=pdf_dir,
                company={**RUNTIME.settings["business"],"address":"Paradiž 73, 2282 Cirkulane"},
            )
            if not journal_pdf.is_file() or closed["difference"] != Decimal("0.00"):
                raise RuntimeError("Blagajniški dnevnik se ni zaključil z natančnim stanjem.")

        c = sqlite3.connect(office)
        rows = c.execute(
            "SELECT fiscal_invoice_no,fiscal_status,print_status FROM documents "
            "WHERE pos_issue_token LIKE 'E2E-AUTO-%' ORDER BY id"
        ).fetchall()
        outbox = c.execute("SELECT COUNT(*) FROM furs_test_outbox").fetchone()[0]
        c.close()
        if rows != [
            (long_no, "TEST_DISABLED", "PRINTED"),
            (service_no, "TEST_DISABLED", "PRINTED"),
            (third_no, "TEST_DISABLED", "PRINTED"),
        ]:
            raise RuntimeError(f"Nepričakovana stanja TEST računov: {rows}")
        if outbox != 0:
            raise RuntimeError("Lokalni E2E računi ne smejo sami v FURS TEST čakalno vrsto.")

        print("[OK] Dolg naziv: A4 + 80-mm PDF")
        print("[OK] Servis -> blagajna -> račun -> zaključeno servisno naročilo")
        print("[OK] Obnovljena naprava -> MFT -> razdeljeno plačilo -> 12-mesečni podpis")
        print("[OK] Blagajniški dnevnik MFD/MFT -> odprtje -> dogodki -> točen zaključek -> PDF")
        print("[OK] Vsi računi: TEST_DISABLED; FURS čakalna vrsta je prazna")
        print("[OK] Produkcijski FURS in produkcijsko potrdilo: nedosegljiva")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
