import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from mikrofix_v29_core import (
    HarvestPart,
    InventoryExceptionLine,
    ReceiptLine,
    V29Error,
    attach_invoice_to_receipt,
    complete_refurbishment,
    create_inventory_exception,
    create_and_book_receipt,
    ensure_inventory_schema,
    ensure_office_schema,
    ensure_service_schema,
    harvest_donor_device,
    recommend_price,
    reverse_full_writeoff,
    register_buyback_device,
    start_refurbishment,
    storno_inventory_exception,
    storno_receipt,
    trace_article,
    update_price_profile,
    validate_document_quantity,
    validate_document_line_price,
)


PRICING = {
    "categories": {
        "Battery FixPremium": {
            "warranty_reserve": 0.08,
            "tiers": [
                {"max_cost_eur": 15, "target_margin": 0.35},
                {"max_cost_eur": 30, "target_margin": 0.30},
                {"max_cost_eur": None, "target_margin": 0.20},
            ],
        }
    }
}


class V29Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inv = root / "inventory.db"
        self.office = root / "office.db"
        self.service = root / "service.db"
        ensure_inventory_schema(self.inv, PRICING)
        ensure_office_schema(self.office)
        ensure_service_schema(self.service)
        c = sqlite3.connect(self.inv)
        stamp = "2026-08-28 10:00:00"
        c.execute(
            """
            INSERT INTO inventory_items(
                barcode, article_name, internal_code, stock_qty, price,
                created_at, updated_at, category, price_profile_code
            ) VALUES('BAT-1','Testna baterija','BAT1',0,50,?,?,?,?)
            """,
            (stamp, stamp, "Battery FixPremium", "Battery FixPremium"),
        )
        self.article_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        c.close()

        c = sqlite3.connect(self.service)
        c.execute(
            """
            INSERT INTO buybacks(
                buyback_no, created_at, status, seller_name, seller_birth_date,
                seller_address, seller_iban, identity_checked, device_model,
                device_serial, device_imei, condition_text, acquisition_method,
                imei_check_provider, imei_check_result, imei_check_reference,
                imei_checked_at, account_removed, activation_lock_removed,
                mdm_clear, ownership_declaration, privacy_notice_received,
                risk_level, purchase_price, payout_method, operator_code,
                microdetect_confirmed, microdetect_at, payment_status
            ) VALUES(
                'MF-ODK-2026-0001',?,'COMPLETED','Prodajalec','1990-01-01',
                'Naslov','SI56',1,'iPhone 11 Pro','SN1','IMEI1','Test','RABLJENO',
                'TEST','TEST_CLEAR','REF',?,1,1,1,1,1,'GREEN',120,'BANK_TRANSFER',
                'DR',1,?,'PAID'
            )
            """,
            (stamp, stamp, stamp),
        )
        self.buyback_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dynamic_price_is_recommendation(self):
        rec = recommend_price(20, PRICING["categories"]["Battery FixPremium"], profile_code="BAT")
        self.assertEqual(rec.landed_cost, 21.6)
        self.assertEqual(rec.target_margin, 0.30)
        self.assertEqual(rec.recommended_price, 31.0)
        self.assertEqual(rec.hard_floor, 21.0)
        self.assertEqual(rec.profile_code, "BAT")

    def test_buyback_cannot_enter_stock_before_signed_and_paid(self):
        c = sqlite3.connect(self.service)
        c.execute(
            "UPDATE buybacks SET status='CONTRACT_READY', payment_status='PENDING' WHERE id=?",
            (self.buyback_id,),
        )
        c.commit()
        c.close()
        with self.assertRaisesRegex(V29Error, "podpisu pogodbe"):
            register_buyback_device(
                service_db=self.service,
                inventory_db=self.inv,
                buyback_id=self.buyback_id,
                purpose="UNDECIDED",
                buyer={"legal_name": "MikroFIX"},
            )

    def test_office_profile_change_survives_restart(self):
        update_price_profile(
            self.inv, code="Battery FixPremium", label="Baterije",
            warranty_reserve=0.08,
            tiers=[
                {"max_cost_eur": 15, "target_margin": 0.36},
                {"max_cost_eur": 30, "target_margin": 0.27},
                {"max_cost_eur": None, "target_margin": 0.19},
            ],
        )
        ensure_inventory_schema(self.inv, PRICING)
        c = sqlite3.connect(self.inv)
        value = c.execute(
            "SELECT version_no,tiers_json FROM price_profile_versions "
            "WHERE code='Battery FixPremium' AND active=1"
        ).fetchone()
        c.close()
        import json
        self.assertEqual(value[0], 2)
        self.assertEqual(json.loads(value[1])[1]["target_margin"], 0.27)

    def test_discount_floors_are_separate(self):
        self.assertEqual(
            validate_document_line_price(
                line_kind="PART", base_price=20, discount_pct=40,
                acquisition_cost=10,
            ),
            12.0,
        )
        with self.assertRaises(V29Error):
            validate_document_line_price(
                line_kind="LABOR", base_price=30, discount_pct=30,
                labor_floor_eur=22,
            )

    def test_document_quantity_rejects_corrupt_huge_value(self):
        self.assertEqual(validate_document_quantity("12"), 12)
        with self.assertRaisesRegex(V29Error, "previsoka"):
            validate_document_quantity("11001101111010110")
        with self.assertRaises(V29Error):
            validate_document_quantity("1.5")

    def test_receipt_invoice_and_storno_are_one_chain(self):
        rid, number = create_and_book_receipt(
            office_db=self.office, inventory_db=self.inv,
            partner_id=None, partner_name="FixShop",
            source_document_type="DOBAVNICA", source_external_no="D-1",
            invoice_external_no="", invoice_date="", received_at="2026-08-28",
            provisional_cost=True,
            lines=[ReceiptLine(self.article_id, 2, 20, 35)],
        )
        self.assertTrue(number.startswith("PRE-"))
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items").fetchone()[0], 2)
        c.close()
        attach_invoice_to_receipt(
            office_db=self.office, receipt_id=rid,
            invoice_external_no="R-1", invoice_date="2026-08-29",
        )
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items").fetchone()[0], 2)
        c.close()
        storno_receipt(
            office_db=self.office, inventory_db=self.inv,
            receipt_id=rid, reason="Napačna količina",
        )
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 2)
        c.close()

    def test_buyback_enters_quarantine_and_proportional_donor_allocation(self):
        device_id, code = register_buyback_device(
            service_db=self.service, inventory_db=self.inv,
            buyback_id=self.buyback_id, purpose="PARTS",
            buyer={"legal_name": "MikroFIX", "representative": "Daniel Ristoski"},
        )
        self.assertTrue(code.startswith("NAP-"))
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT status FROM used_devices").fetchone()[0], "QUARANTINE")
        c.execute(
            """
            INSERT INTO inventory_items(
                barcode, article_name, stock_qty, price, created_at, updated_at
            ) VALUES('CAM-1','Kamera',0,40,'x','x')
            """
        )
        second = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        c.close()
        _hid, _hno, unit_cost = harvest_donor_device(
            inventory_db=self.inv, device_id=device_id,
            parts=[HarvestPart(self.article_id), HarvestPart(second)],
        )
        self.assertEqual(unit_cost, 75.0)
        trace = trace_article(self.inv, self.article_id)
        self.assertEqual(len(trace["donor_sources"]), 1)
        # Celotni strošek 120 EUR + ena ura T1 (30 EUR) se deli v
        # razmerju prodajnih vrednosti 50 : 40.
        self.assertEqual(trace["donor_sources"][0]["allocated_cost"], 83.33)

    def test_empty_harvest_requires_explicit_full_writeoff_and_records_waste(self):
        device_id, _code = register_buyback_device(
            service_db=self.service, inventory_db=self.inv,
            buyback_id=self.buyback_id, purpose="PARTS",
            buyer={"legal_name": "MikroFix"},
        )
        with self.assertRaisesRegex(V29Error, "POPOLNEGA ODPISA"):
            harvest_donor_device(
                inventory_db=self.inv, device_id=device_id, parts=[]
            )
        harvest_id, _number, value = harvest_donor_device(
            inventory_db=self.inv, device_id=device_id, parts=[],
            full_writeoff_confirmed=True,
        )
        c = sqlite3.connect(self.inv)
        waste = c.execute(
            "SELECT waste_type,status FROM donor_waste_records_v30 "
            "WHERE harvest_id=? ORDER BY waste_type", (harvest_id,),
        ).fetchall()
        status = c.execute(
            "SELECT status FROM used_devices WHERE id=?", (device_id,)
        ).fetchone()[0]
        c.close()
        self.assertEqual(value, 0.0)
        self.assertEqual(waste, [
            ("BATTERY", "AWAITING_HANDOFF"),
            ("ELECTRONIC", "AWAITING_HANDOFF"),
        ])
        self.assertEqual(status, "WRITTEN_OFF")

        number = reverse_full_writeoff(
            inventory_db=self.inv, device_id=device_id,
            operator_code="DR", reason="Napačen testni klik",
        )
        c = sqlite3.connect(self.inv)
        device_status = c.execute(
            "SELECT status,allocated_cost FROM used_devices WHERE id=?", (device_id,)
        ).fetchone()
        harvest = c.execute(
            "SELECT status,reversed_by,reversal_reason FROM donor_harvests WHERE id=?",
            (harvest_id,),
        ).fetchone()
        waste_statuses = c.execute(
            "SELECT DISTINCT status FROM donor_waste_records_v30 WHERE harvest_id=?",
            (harvest_id,),
        ).fetchall()
        c.close()
        self.assertTrue(number.startswith("RAZ-"))
        self.assertEqual(device_status, ("QUARANTINE", 0.0))
        self.assertEqual(harvest, ("STORNIRAN", "DR", "Napačen testni klik"))
        self.assertEqual(waste_statuses, [("CANCELLED",)])

    def test_module_harvest_creates_master_article_unique_piece_and_waste(self):
        device_id, _code = register_buyback_device(
            service_db=self.service, inventory_db=self.inv,
            buyback_id=self.buyback_id, purpose="PARTS",
            buyer={"legal_name": "MikroFix"},
        )
        harvest_id, _number, _value = harvest_donor_device(
            inventory_db=self.inv, device_id=device_id,
            parts=[HarvestPart(
                module_code="DISPLAY", module_name="zaslon",
                device_model="iPhone 11 Pro", recommended_sale_price=80,
                price_source_kind="iFixit EU",
                price_source_url="https://www.ifixit.com/en-eu/products/pulled-display",
                price_observed_on=date.today().isoformat(), location="KARANTENA-DELI",
            )],
            waste_modules=[("BATTERY", "Neuporabna baterija")],
        )
        c = sqlite3.connect(self.inv)
        article = c.execute(
            "SELECT article_name,track_mode,stock_qty,price FROM inventory_items "
            "WHERE category='RABLJENI IZVLEČENI DELI'"
        ).fetchone()
        piece = c.execute(
            "SELECT lot_serial,module_code,price_source_url FROM donor_parts "
            "WHERE harvest_id=?", (harvest_id,),
        ).fetchone()
        lot = c.execute(
            "SELECT lot_serial,quantity_available FROM inventory_lots_v30 "
            "WHERE source_type='DONOR_HARVEST' AND source_id=?", (harvest_id,),
        ).fetchone()
        waste_types = [row[0] for row in c.execute(
            "SELECT waste_type FROM donor_waste_records_v30 WHERE harvest_id=?",
            (harvest_id,),
        )]
        c.close()
        self.assertEqual(article, (
            "Rabljeni izvlečeni del – zaslon za iPhone 11 Pro", "UNIQUE", 1.0, 80.0
        ))
        self.assertTrue(piece[0].startswith("PP"))
        self.assertEqual(piece[1:], (
            "DISPLAY", "https://www.ifixit.com/en-eu/products/pulled-display"
        ))
        self.assertEqual(lot, (piece[0], 1.0))
        self.assertEqual(sorted(waste_types), ["BATTERY", "ELECTRONIC"])

    def test_exception_document_and_storno_keep_trace(self):
        c = sqlite3.connect(self.inv)
        c.execute("UPDATE inventory_items SET stock_qty=5 WHERE id=?", (self.article_id,))
        c.execute(
            """
            INSERT INTO stock_movements(
                movement_no, created_at, article_id, quantity_delta, balance_after,
                movement_type, source_type, reason, operator_code
            ) VALUES('OPEN-1','x',?,5,5,'OPENING','MIGRATION','test','DR')
            """,
            (self.article_id,),
        )
        c.commit()
        c.close()
        document_id, _number = create_inventory_exception(
            inventory_db=self.inv, document_type="ODPIS", reason="Poškodovan del",
            lines=[InventoryExceptionLine(self.article_id, -2)],
        )
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items").fetchone()[0], 3)
        c.close()
        storno_inventory_exception(
            inventory_db=self.inv, document_id=document_id, reason="Napačen artikel"
        )
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items").fetchone()[0], 5)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 3)
        c.close()

    def test_refurbishment_locks_sale_price_after_tests(self):
        device_id, _code = register_buyback_device(
            service_db=self.service, inventory_db=self.inv,
            buyback_id=self.buyback_id, purpose="REFURBISH",
            buyer={"legal_name": "MikroFIX"},
        )
        order_id, _number = start_refurbishment(
            service_db=self.service, inventory_db=self.inv, device_id=device_id,
        )
        with self.assertRaises(V29Error):
            complete_refurbishment(
                service_db=self.service, inventory_db=self.inv, order_id=order_id,
                labor_cost=20, parts_cost=30, other_cost=0, sale_price=160,
                tests_passed=False,
            )
        _number, total = complete_refurbishment(
            service_db=self.service, inventory_db=self.inv, order_id=order_id,
            labor_cost=20, parts_cost=30, other_cost=0, sale_price=180,
            tests_passed=True,
        )
        self.assertEqual(total, 170.0)
        c = sqlite3.connect(self.inv)
        self.assertEqual(c.execute("SELECT status FROM used_devices").fetchone()[0], "READY_FOR_SALE")
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM inventory_items WHERE source_kind='BUYBACK_DEVICE' AND stock_qty=1").fetchone()[0],
            1,
        )
        c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
