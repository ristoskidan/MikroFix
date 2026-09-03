import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mikrofix_v29_core import ensure_inventory_schema, ensure_office_schema
from mikrofix_v30_receipts import (
    ReceiptError,
    ReceiptLineV30,
    complete_with_invoice,
    confirm_receipt,
    create_draft,
    delete_draft,
    ensure_receipt_schema,
    load_draft_payload,
    save_draft_payload,
    storno_receipt,
)


PRICING = {
    "categories": {
        "SPLOŠNO": {
            "target_margin": 0.30,
            "hard_margin": 0.15,
            "min_contribution_eur": 10.0,
            "consumables_eur": 0.0,
            "warranty_reserve": 0.0,
        }
    }
}


class V30ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.office = self.root / "office.db"
        self.inventory = self.root / "inventory.db"
        self.archive = self.root / "archive"
        ensure_office_schema(self.office)
        ensure_inventory_schema(self.inventory, PRICING)
        ensure_receipt_schema(self.office)
        connection = sqlite3.connect(self.inventory)
        stamp = "2026-08-30 12:00:00"
        connection.execute(
            """
            INSERT INTO inventory_items(
                barcode, article_name, stock_qty, price, created_at, updated_at,
                category, price_profile_code, track_mode
            ) VALUES('SUP-001','Testni artikel',0,0,?,?,
                     'SPLOŠNO','SPLOŠNO','QUANTITY')
            """,
            (stamp, stamp),
        )
        self.article_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
        connection.close()
        self.pdf = self.root / "racun.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n% MikroFIX test\n")

    def tearDown(self):
        self.temp.cleanup()

    def line(self, *, qty="2", total="24.40", sale="20.00"):
        return ReceiptLineV30(
            article_id=self.article_id,
            quantity=qty,
            net_unit_price="10.0000",
            discount_percent="0",
            vat_rate="22",
            supplier_line_total=total,
            confirmed_sale_price=sale,
            supplier_barcode="SUP-001",
        )

    def confirm(self, receipt_id, kind="DOBAVNICA_IN_RACUN", **overrides):
        values = dict(
            office_db=self.office,
            inventory_db=self.inventory,
            archive_dir=self.archive,
            receipt_id=receipt_id,
            partner_id=None,
            partner_name="Dobavitelj d.o.o.",
            partner_snapshot={"name": "Dobavitelj d.o.o."},
            document_type=kind,
            custom_document_type="",
            external_document_no="D-100",
            document_date="2026-08-30",
            received_at="2026-08-30",
            currency="EUR",
            document_total="25.40",
            shipping_total="1.0000",
            invoice_external_no="R-100" if kind in {"RACUN", "DOBAVNICA_IN_RACUN"} else "",
            invoice_date="2026-08-30" if kind in {"RACUN", "DOBAVNICA_IN_RACUN"} else "",
            invoice_due_date="2026-09-07",
            invoice_payment_status="NEPLACAN",
            lines=[self.line()],
            attachment_paths=[self.pdf],
            operator_code="DR",
        )
        values.update(overrides)
        return confirm_receipt(**values)

    def stock(self):
        connection = sqlite3.connect(self.inventory)
        value = connection.execute(
            "SELECT stock_qty FROM inventory_items WHERE id=?", (self.article_id,)
        ).fetchone()[0]
        connection.close()
        return value

    def test_draft_is_idempotent_and_deletable(self):
        first_id, key = create_draft(self.office, draft_key="same", operator_code="DR")
        second_id, _ = create_draft(self.office, draft_key="same", operator_code="DR")
        self.assertEqual(first_id, second_id)
        save_draft_payload(self.office, first_id, {"supplier": "Test", "lines": [1]})
        self.assertEqual(load_draft_payload(self.office, first_id)["supplier"], "Test")
        delete_draft(self.office, first_id)
        connection = sqlite3.connect(self.office)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM receipts_v30").fetchone()[0], 0)
        connection.close()

    def test_invoice_addition_is_autosaved_while_delivery_waits(self):
        receipt_id, _ = create_draft(self.office)
        self.confirm(
            receipt_id, kind="DOBAVNICA", invoice_external_no="",
            invoice_date="", attachment_paths=[],
        )
        save_draft_payload(
            self.office, receipt_id,
            {"invoice_no": "R-OSNUTEK", "document_total": "25.40"},
        )
        self.assertEqual(
            load_draft_payload(self.office, receipt_id)["invoice_no"],
            "R-OSNUTEK",
        )

    def test_combined_document_releases_stock_once_and_archives_hash(self):
        receipt_id, _ = create_draft(self.office)
        _, receipt_no = self.confirm(receipt_id)
        self.assertEqual(self.stock(), 2)
        connection = sqlite3.connect(self.office)
        row = connection.execute(
            "SELECT status, stock_released FROM receipts_v30 WHERE id=?", (receipt_id,)
        ).fetchone()
        attachment = connection.execute(
            "SELECT archived_path, sha256 FROM receipt_attachments_v30 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        allocation = connection.execute(
            "SELECT allocated_shipping, landed_unit_cost FROM receipt_lines_v30 WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("BOOKED", 1))
        self.assertTrue(Path(attachment[0]).is_file())
        self.assertEqual(len(attachment[1]), 64)
        self.assertEqual(receipt_no[:4], "PRE-")
        self.assertEqual(Decimal(allocation[0]), Decimal("1.0000"))
        self.assertEqual(Decimal(allocation[1]), Decimal("12.7000"))
        connection = sqlite3.connect(self.office)
        internal_lot = connection.execute(
            "SELECT lot_serial FROM receipt_lines_v30 WHERE receipt_id=?", (receipt_id,)
        ).fetchone()[0]
        connection.close()
        self.assertTrue(internal_lot.startswith(f"LOT-{receipt_no}-"))
        connection = sqlite3.connect(self.inventory)
        history = connection.execute(
            "SELECT acquisition_cost, confirmed_price, source_type, source_id FROM price_history"
        ).fetchone()
        concrete_lot = connection.execute(
            """
            SELECT source_type, source_id, quantity_received, quantity_available,
                   acquisition_cost, status
            FROM inventory_lots_v30 WHERE article_id=?
            """,
            (self.article_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(history[0], 12.7)
        self.assertEqual(history[1], 20.0)
        self.assertEqual(history[2:], ("RECEIPT_V30", receipt_id))
        self.assertEqual(concrete_lot[0:2], ("RECEIPT_V30", receipt_id))
        self.assertEqual(Decimal(str(concrete_lot[2])), Decimal("2.0"))
        self.assertEqual(Decimal(str(concrete_lot[3])), Decimal("2.0"))
        self.assertEqual(Decimal(str(concrete_lot[4])), Decimal("12.7"))
        self.assertEqual(concrete_lot[5], "ACTIVE")

    def test_delivery_is_pending_until_exact_invoice_then_released_once(self):
        receipt_id, _ = create_draft(self.office)
        self.confirm(
            receipt_id,
            kind="DOBAVNICA",
            document_total="24.40",
            shipping_total="0",
        )
        self.assertEqual(self.stock(), 0)
        connection = sqlite3.connect(self.office)
        self.assertEqual(
            connection.execute(
                "SELECT status FROM receipts_v30 WHERE id=?", (receipt_id,)
            ).fetchone()[0],
            "PENDING_INVOICE",
        )
        self.assertEqual(
            connection.execute(
                "SELECT status FROM pending_stock_v30 WHERE receipt_id=?", (receipt_id,)
            ).fetchone()[0],
            "PENDING_INVOICE",
        )
        connection.close()
        complete_with_invoice(
            office_db=self.office,
            inventory_db=self.inventory,
            archive_dir=self.archive,
            receipt_id=receipt_id,
            invoice_external_no="R-101",
            invoice_date="2026-08-30",
            invoice_due_date="2026-09-07",
            invoice_payment_status="NEPLACAN",
            document_total="25.40",
            shipping_total="1.0000",
            lines=[self.line()],
            attachment_paths=[self.pdf],
        )
        self.assertEqual(self.stock(), 2)
        with self.assertRaises(ReceiptError):
            complete_with_invoice(
                office_db=self.office,
                inventory_db=self.inventory,
                archive_dir=self.archive,
                receipt_id=receipt_id,
                invoice_external_no="R-101",
                invoice_date="2026-08-30",
                invoice_due_date="",
                invoice_payment_status="NEPLACAN",
                document_total="25.40",
                shipping_total="1",
                lines=[self.line()],
                attachment_paths=[self.pdf],
            )
        self.assertEqual(self.stock(), 2)

    def test_line_and_document_difference_must_be_exactly_zero(self):
        receipt_id, _ = create_draft(self.office)
        with self.assertRaisesRegex(ReceiptError, "Dovoljeno odstopanje je 0,00"):
            self.confirm(receipt_id, lines=[self.line(total="24.41")], document_total="25.41")
        self.assertEqual(self.stock(), 0)
        with self.assertRaisesRegex(ReceiptError, "Dovoljeno odstopanje je 0,00"):
            self.confirm(receipt_id, document_total="25.41")
        self.assertEqual(self.stock(), 0)

    def test_attachment_is_optional_and_supplied_content_is_checked(self):
        receipt_id, _ = create_draft(self.office)
        self.confirm(receipt_id, attachment_paths=[])
        second_id, _ = create_draft(self.office)
        fake = self.root / "fake.pdf"
        fake.write_text("not really a pdf", encoding="utf-8")
        with self.assertRaisesRegex(ReceiptError, "ni prepoznan"):
            self.confirm(
                second_id, attachment_paths=[fake],
                external_document_no="D-101", invoice_external_no="R-101",
            )

    def test_full_storno_reverses_released_stock_and_keeps_document(self):
        receipt_id, _ = create_draft(self.office)
        self.confirm(receipt_id)
        self.assertEqual(self.stock(), 2)
        storno_receipt(
            office_db=self.office,
            inventory_db=self.inventory,
            receipt_id=receipt_id,
            reason="Napačen dobaviteljev dokument",
        )
        self.assertEqual(self.stock(), 0)
        connection = sqlite3.connect(self.office)
        row = connection.execute(
            "SELECT status, storno_reason, receipt_no FROM receipts_v30 WHERE id=?",
            (receipt_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "STORNIRANO")
        self.assertTrue(row[1])
        self.assertTrue(row[2])

    def test_storno_is_blocked_when_that_concrete_lot_was_partly_consumed(self):
        receipt_id, _ = create_draft(self.office)
        self.confirm(receipt_id)
        connection = sqlite3.connect(self.inventory)
        lot_id = connection.execute(
            "SELECT id FROM inventory_lots_v30 WHERE source_type='RECEIPT_V30' AND source_id=?",
            (receipt_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE inventory_lots_v30 SET quantity_available=1 WHERE id=?",
            (lot_id,),
        )
        connection.execute(
            """
            INSERT INTO inventory_lots_v30(
                article_id, source_type, source_id, source_line_id, lot_serial,
                quantity_received, quantity_available, acquisition_cost, sale_price,
                location, status, received_at, updated_at
            ) VALUES(?, 'TEST_OTHER_LOT', 999, 999, 'LOT-DRUGI', 1, 1, 10, 20,
                     '', 'ACTIVE', '2026-08-31 10:00:00', '2026-08-31 10:00:00')
            """,
            (self.article_id,),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(ReceiptError, "konkretnega LOT-a"):
            storno_receipt(
                office_db=self.office,
                inventory_db=self.inventory,
                receipt_id=receipt_id,
                reason="Napačen prejem",
            )

        connection = sqlite3.connect(self.office)
        self.assertEqual(
            connection.execute(
                "SELECT status FROM receipts_v30 WHERE id=?", (receipt_id,)
            ).fetchone()[0],
            "BOOKED",
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
