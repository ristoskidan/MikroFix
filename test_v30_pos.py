import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mikrofix_v30_pos import (
    PosError, PosLine, abandon_pos_draft, calculate_line, default_service_description,
    eligible_storned_invoices, ensure_pos_schema, invoice_snapshot, issue_invoice,
    latest_pending_print, load_active_pos_draft, mark_pdf_ready, mark_printed,
    register_copy, save_pos_draft,
    storno_invoice,
)
from mikrofix_v30_cash import open_cash_session


class V30PosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.office = base / "office.db"
        self.inventory = base / "inventory.db"
        self.service = base / "service.db"
        c = sqlite3.connect(self.office)
        c.executescript("""
            CREATE TABLE documents(
                id INTEGER PRIMARY KEY AUTOINCREMENT, doc_no TEXT NOT NULL UNIQUE,
                doc_type TEXT NOT NULL, partner_id INTEGER, partner_name_snapshot TEXT,
                partner_address_snapshot TEXT, partner_tax_snapshot TEXT,
                partner_pcd_snapshot REAL NOT NULL DEFAULT 0, external_no TEXT,
                date TEXT NOT NULL, order_date TEXT, received_date TEXT, due_date TEXT,
                currency TEXT NOT NULL DEFAULT 'EUR', delivery_cost REAL NOT NULL DEFAULT 0,
                stock_effect INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'OSNUTEK',
                linked_claim_id INTEGER, linked_claim_no TEXT, linked_service_id INTEGER,
                notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, booked_at TEXT
            );
            CREATE TABLE document_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
                article_id INTEGER, barcode TEXT, article_name TEXT NOT NULL,
                internal_code TEXT, qty REAL NOT NULL DEFAULT 1,
                unit_price REAL NOT NULL DEFAULT 0, discount_pct REAL NOT NULL DEFAULT 0,
                vat_pct REAL NOT NULL DEFAULT 0, delivery_share REAL NOT NULL DEFAULT 0,
                actual_unit_cost REAL NOT NULL DEFAULT 0, margin_pct REAL NOT NULL DEFAULT 0,
                calculated_price REAL NOT NULL DEFAULT 0, recommended_price REAL NOT NULL DEFAULT 0,
                sell_price REAL NOT NULL DEFAULT 0, line_net REAL NOT NULL DEFAULT 0,
                line_vat REAL NOT NULL DEFAULT 0, line_total REAL NOT NULL DEFAULT 0
            );
        """)
        c.commit(); c.close()
        c = sqlite3.connect(self.inventory)
        c.executescript("""
            CREATE TABLE inventory_items(
                id INTEGER PRIMARY KEY, barcode TEXT UNIQUE, article_name TEXT,
                internal_code TEXT, stock_qty REAL, price REAL, acquisition_cost REAL,
                reserved_qty REAL DEFAULT 0, active INTEGER DEFAULT 1, lot_serial TEXT,
                location TEXT, source_kind TEXT, source_id INTEGER, updated_at TEXT
            );
            CREATE TABLE stock_movements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, movement_no TEXT UNIQUE,
                created_at TEXT, article_id INTEGER, quantity_delta REAL,
                balance_after REAL, movement_type TEXT, source_type TEXT,
                source_id INTEGER, source_line_id INTEGER, lot_serial TEXT,
                location_from TEXT, location_to TEXT, reason TEXT,
                operator_code TEXT, reversal_of_id INTEGER
            );
            CREATE UNIQUE INDEX idx_source ON stock_movements(
                source_type, source_id, source_line_id, movement_type
            );
            CREATE TABLE used_devices(
                id INTEGER PRIMARY KEY, status TEXT, buyer_name TEXT,
                sale_document_id INTEGER, warranty_until TEXT, updated_at TEXT
            );
            INSERT INTO inventory_items VALUES(
                1,'TEST-1','Testni zaslon','LCD',3,100,50,0,1,'LOT-1','GLAVNA','',0,''
            );
        """)
        c.commit(); c.close()
        c = sqlite3.connect(self.service)
        c.executescript("""
            CREATE TABLE service(
                id INTEGER PRIMARY KEY, invoice_no TEXT, office_invoice_id INTEGER,
                service_order_id INTEGER
            );
            CREATE TABLE service_orders(
                id INTEGER PRIMARY KEY, status TEXT, completed_at TEXT, service_id INTEGER
            );
            CREATE TABLE service_parts(
                id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER,
                article_id INTEGER, barcode TEXT, part_name TEXT,
                part_id TEXT, unit_price REAL
            );
            INSERT INTO service VALUES(7,NULL,NULL,70);
            INSERT INTO service_orders VALUES(70,'OPEN',NULL,7);
        """)
        c.commit(); c.close()
        ensure_pos_schema(self.office)
        open_cash_session(
            self.office,premise_id="MFD",device_id="PIS",
            operator_code="DR",opening_cash=500,
        )
        open_cash_session(
            self.office,premise_id="MFT",device_id="PIS",
            operator_code="DR",opening_cash=500,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def line(self, **changes):
        values = dict(line_kind="ARTICLE", name="Testni zaslon", code="LCD",
                      barcode="TEST-1", article_id=1, quantity=Decimal("1"),
                      base_unit_price=Decimal("100"), acquisition_cost=Decimal("50"))
        values.update(changes)
        return PosLine(**values)

    def service_part_line(self, **changes):
        values = dict(
            stock_already_consumed=True, origin_kind="SERVICE_PART",
            origin_id=1, source_locked=True,
        )
        values.update(changes)
        return self.line(**values)

    def fiscal(self, premise="MFD"):
        return {
            "operator_tax_number": "84514841",
            "premise_id": premise,
            "electronic_device_id": "PIS",
            "authenticated_username": "daniel",
        }

    def test_pos_draft_is_persistent_verified_and_abandonable(self):
        payload = {
            "schema_version": 1,
            "lines": [{"name": "Zelo dolg testni naziv artikla", "quantity": "1.00"}],
            "customer": {"name": "Testna stranka"},
        }
        save_pos_draft(
            self.office, issue_token="draft-one", operator_code="DR",
            actor_username="daniel",
            premise_id="MFD", device_id="PIS", payload=payload,
        )
        loaded = load_active_pos_draft(
            self.office, actor_username="daniel", device_id="PIS"
        )
        self.assertEqual(loaded["issue_token"], "draft-one")
        self.assertEqual(loaded["payload"], payload)

        changed = dict(payload)
        changed["customer"] = {"name": "Popravljena stranka"}
        save_pos_draft(
            self.office, issue_token="draft-one", operator_code="DR",
            actor_username="daniel",
            premise_id="MFD", device_id="PIS", payload=changed,
        )
        loaded = load_active_pos_draft(
            self.office, actor_username="daniel", device_id="PIS"
        )
        self.assertEqual(loaded["payload"]["customer"]["name"], "Popravljena stranka")
        abandon_pos_draft(
            self.office, "draft-one", actor_username="daniel", device_id="PIS"
        )
        self.assertIsNone(
            load_active_pos_draft(
                self.office, actor_username="daniel", device_id="PIS"
            )
        )
        with self.assertRaisesRegex(PosError, "ni mogoče prepisati"):
            save_pos_draft(
                self.office, issue_token="draft-one", operator_code="DR",
                actor_username="daniel",
                premise_id="MFD", device_id="PIS", payload=payload,
            )

    def test_issuing_invoice_atomically_closes_matching_draft(self):
        save_pos_draft(
            self.office, issue_token="draft-to-issue", operator_code="DR",
            actor_username="daniel",
            premise_id="MFD", device_id="PIS", payload={"lines": ["test"]},
        )
        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="draft-to-issue",
            **self.fiscal(),
        )
        c = sqlite3.connect(self.office)
        row = c.execute(
            "SELECT status,final_document_id FROM pos_drafts_v31 WHERE issue_token=?",
            ("draft-to-issue",),
        ).fetchone()
        c.close()
        self.assertEqual(row, ("ISSUED", document_id))
        self.assertIsNone(
            load_active_pos_draft(
                self.office, actor_username="daniel", device_id="PIS"
            )
        )

    def test_damaged_pos_draft_is_never_restored(self):
        save_pos_draft(
            self.office, issue_token="damaged-draft", operator_code="DR",
            actor_username="daniel",
            premise_id="MFD", device_id="PIS", payload={"customer": {"name": "A"}},
        )
        c = sqlite3.connect(self.office)
        c.execute(
            "UPDATE pos_drafts_v31 SET payload_json=? WHERE issue_token=?",
            ('{"customer":{"name":"B"}}', "damaged-draft"),
        )
        c.commit(); c.close()
        with self.assertRaisesRegex(PosError, "poškodovan"):
            load_active_pos_draft(
                self.office, actor_username="daniel", device_id="PIS"
            )

    def test_pos_drafts_are_isolated_by_authenticated_user_and_device(self):
        save_pos_draft(
            self.office, issue_token="daniel-only", operator_code="DR",
            actor_username="daniel", premise_id="MFD", device_id="PIS",
            payload={"customer": {"name": "Zasebni osnutek"}},
        )
        self.assertIsNone(
            load_active_pos_draft(
                self.office, actor_username="drugi", device_id="PIS"
            )
        )
        with self.assertRaisesRegex(PosError, "drugemu uporabniku"):
            save_pos_draft(
                self.office, issue_token="daniel-only", operator_code="DR",
                actor_username="drugi", premise_id="MFD", device_id="PIS",
                payload={"customer": {"name": "Prevzem"}},
            )
        with self.assertRaisesRegex(PosError, "drugemu uporabniku"):
            issue_invoice(
                office_db=self.office, inventory_db=self.inventory,
                service_db=self.service, lines=[self.line()], payments={"CASH": 100},
                customer={"name": "Drugi"}, operator_code="DR", labor_floor=22,
                issue_token="daniel-only", authenticated_username="drugi",
                operator_tax_number="84514841", premise_id="MFD",
                electronic_device_id="PIS",
            )
        with self.assertRaisesRegex(PosError, "drugemu uporabniku"):
            abandon_pos_draft(
                self.office, "daniel-only", actor_username="drugi", device_id="PIS"
            )

    def test_legacy_unowned_pos_draft_is_not_automatically_restored(self):
        c = sqlite3.connect(self.office)
        raw = '{"customer":{"name":"Stari osnutek"}}'
        import hashlib
        c.execute(
            """
            INSERT INTO pos_drafts_v31(
                issue_token,operator_code,actor_username,premise_id,device_id,status,
                payload_json,payload_sha256,created_at,updated_at
            ) VALUES('legacy','DR',NULL,'MFD','PIS','ACTIVE',?,?,?,?)
            """,
            (raw, hashlib.sha256(raw.encode("utf-8")).hexdigest(), "2026-01-01", "2026-01-01"),
        )
        c.commit(); c.close()
        self.assertIsNone(
            load_active_pos_draft(
                self.office, actor_username="daniel", device_id="PIS"
            )
        )

    def test_article_discount_is_silently_clamped_to_cost_plus_five_percent(self):
        result = calculate_line(self.line(requested_discount_pct=Decimal("100")), 22)
        self.assertEqual(result.final_unit_price, Decimal("52.50"))
        self.assertEqual(result.applied_discount_pct, Decimal("47.50"))

    def test_service_discount_is_clamped_to_l0(self):
        line = PosLine(line_kind="SERVICE", name="Delo", service_tier="T1",
                       base_unit_price=Decimal("30"), requested_discount_pct=Decimal("100"))
        result = calculate_line(line, 22)
        self.assertEqual(result.final_unit_price, Decimal("22.00"))

    def test_service_tiers_have_fixed_cashier_descriptions(self):
        self.assertEqual(default_service_description("T1"), "Delo v delavnici")
        self.assertEqual(default_service_description("t2"), "Delo na terenu")
        with self.assertRaisesRegex(PosError, "T1 ali T2"):
            default_service_description("T3")

    def test_issue_rejects_price_changed_in_office_after_cart_load(self):
        c = sqlite3.connect(self.inventory)
        c.execute("UPDATE inventory_items SET price=110 WHERE id=1")
        c.commit(); c.close()
        with self.assertRaisesRegex(PosError, "v Office spremenjena"):
            issue_invoice(
                office_db=self.office, inventory_db=self.inventory, service_db=self.service,
                lines=[self.line()], payments={"CASH":100}, customer={"name":"Kupec"},
                operator_code="DR", labor_floor=22, issue_token="stale-price",
                **self.fiscal(),
            )

    def test_double_issue_token_creates_one_invoice_and_one_stock_movement(self):
        kwargs = dict(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100},
            customer={"name": "Kupec"}, operator_code="DR", labor_floor=22,
            issue_token="same-token",
            **self.fiscal(),
        )
        first = issue_invoice(**kwargs)
        second = issue_invoice(**kwargs)
        self.assertEqual(first, second)
        c = sqlite3.connect(self.office)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        c.close()
        c = sqlite3.connect(self.inventory)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items WHERE id=1").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 1)
        allocation = c.execute(
            """
            SELECT allocation_type, quantity, lot_serial
            FROM inventory_lot_allocations_v30
            """
        ).fetchone()
        remaining = c.execute(
            "SELECT quantity_available FROM inventory_lots_v30 WHERE article_id=1"
        ).fetchone()[0]
        c.close()
        self.assertEqual(allocation[0], "POS_INVOICE")
        self.assertEqual(Decimal(str(allocation[1])), Decimal("1.0"))
        self.assertEqual(allocation[2], "LOT-1")
        self.assertEqual(Decimal(str(remaining)), Decimal("2.0"))

    def test_service_consumed_part_is_not_removed_twice(self):
        c = sqlite3.connect(self.service)
        c.execute(
            "INSERT INTO service_parts(service_id,article_id,barcode,part_name,part_id,unit_price) "
            "VALUES(7,1,'TEST-1','Testni zaslon','LCD',100)"
        ); c.commit(); c.close()
        issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.service_part_line()], payments={"CASH":100},
            customer={"name":"Kupec"}, operator_code="DR", labor_floor=22,
            issue_token="service-token", linked_service_id=7,
            **self.fiscal(),
        )
        c=sqlite3.connect(self.inventory)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items WHERE id=1").fetchone()[0],3)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0],0)
        c.close()

        c = sqlite3.connect(self.service)
        self.assertEqual(
            c.execute("SELECT status FROM service_orders WHERE id=70").fetchone()[0],
            "COMPLETED",
        )
        c.close()

    def test_extra_quantity_added_to_service_line_is_removed_from_stock(self):
        c = sqlite3.connect(self.service)
        c.execute(
            "INSERT INTO service_parts(service_id,article_id,barcode,part_name,part_id,unit_price) "
            "VALUES(7,1,'TEST-1','Testni zaslon','LCD',100)"
        ); c.commit(); c.close()
        issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.service_part_line(), self.line()],
            payments={"CASH": 200}, customer={"name":"Kupec"}, operator_code="DR",
            labor_floor=22, issue_token="service-extra-token", linked_service_id=7,
            **self.fiscal(),
        )
        c = sqlite3.connect(self.inventory)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items WHERE id=1").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT quantity_delta FROM stock_movements").fetchone()[0], -1)
        c.close()

    def test_same_service_cannot_be_invoiced_twice(self):
        c = sqlite3.connect(self.service)
        c.execute(
            "INSERT INTO service_parts(service_id,article_id,barcode,part_name,part_id,unit_price) "
            "VALUES(7,1,'TEST-1','Testni zaslon','LCD',100)"
        ); c.commit(); c.close()
        kwargs = dict(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.service_part_line()], payments={"CASH":100},
            customer={"name":"Kupec"}, operator_code="DR", labor_floor=22,
            linked_service_id=7,
            **self.fiscal(),
        )
        issue_invoice(**kwargs, issue_token="service-first")
        mark_printed(self.office, 1, "DR")
        with self.assertRaisesRegex(PosError, "že ima izdan račun"):
            issue_invoice(**kwargs, issue_token="service-second")
        c = sqlite3.connect(self.office)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        c.close()

    def test_new_invoice_is_blocked_until_previous_original_print_is_confirmed(self):
        first_id, first_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH":100}, customer={"name":"Kupec"},
            operator_code="DR", labor_floor=22, issue_token="first-print",
            **self.fiscal(),
        )
        self.assertEqual(latest_pending_print(self.office)["doc_no"], first_no)
        with self.assertRaisesRegex(PosError, "čaka na potrditev"):
            issue_invoice(
                office_db=self.office, inventory_db=self.inventory, service_db=self.service,
                lines=[self.line()], payments={"CASH":100}, customer={"name":"Drugi"},
                operator_code="DR", labor_floor=22, issue_token="second-print",
                **self.fiscal(),
            )
        mark_printed(self.office, first_id, "DR")
        self.assertIsNone(latest_pending_print(self.office))

    def test_original_pdf_releases_next_invoice_without_claiming_physical_print(self):
        first_id, _first_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH":100}, customer={"name":"Kupec"},
            operator_code="DR", labor_floor=22, issue_token="pdf-ready",
            **self.fiscal(),
        )
        mark_pdf_ready(self.office, first_id, "DR")
        self.assertIsNone(latest_pending_print(self.office))
        c = sqlite3.connect(self.office)
        status = c.execute(
            "SELECT print_status FROM documents WHERE id=?", (first_id,)
        ).fetchone()[0]
        c.close()
        self.assertEqual(status, "PDF_READY")
        self.assertEqual(register_copy(self.office, first_id, "DR"), 1)

    def test_full_storno_keeps_both_documents_and_restores_exact_lot(self):
        invoice_id, _invoice_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH":100}, customer={"name":"Kupec"},
            operator_code="DR", labor_floor=22, issue_token="for-storno",
            **self.fiscal(),
        )
        mark_printed(self.office, invoice_id, "DR")
        storno_id, storno_no = storno_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            document_id=invoice_id, reason="Napačen račun", operator_code="DR",
            authenticated_username="daniel",
            action_permissions={"STORNO_DOCUMENT"},
        )
        self.assertTrue(storno_no.startswith("STR-"))
        c = sqlite3.connect(self.office)
        self.assertEqual(
            c.execute("SELECT status FROM documents WHERE id=?",(invoice_id,)).fetchone()[0],
            "STORNIRANO",
        )
        row = c.execute(
            "SELECT linked_invoice_id,invoice_total FROM documents WHERE id=?",(storno_id,)
        ).fetchone()
        c.close()
        self.assertEqual(row[0],invoice_id)
        self.assertEqual(Decimal(str(row[1])),Decimal("-100.0"))
        c = sqlite3.connect(self.inventory)
        self.assertEqual(
            Decimal(str(c.execute("SELECT stock_qty FROM inventory_items WHERE id=1").fetchone()[0])),
            Decimal("3.0"),
        )
        self.assertIsNotNone(
            c.execute(
                "SELECT reversed_at FROM inventory_lot_allocations_v30 WHERE source_id=?",
                (invoice_id,),
            ).fetchone()[0]
        )
        c.close()

    def test_linked_service_invoice_storno_fails_closed_without_service_database(self):
        invoice_id, _invoice_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="service-storno-offline",
            linked_service_id=7, **self.fiscal(),
        )
        mark_printed(self.office, invoice_id, "DR")
        unavailable = self.service.with_suffix(".offline")
        self.service.rename(unavailable)
        try:
            with self.assertRaisesRegex(PosError, "ni dosegljiva"):
                storno_invoice(
                    office_db=self.office, inventory_db=self.inventory,
                    service_db=self.service, document_id=invoice_id,
                    reason="Preizkus manjkajoče baze", operator_code="DR",
                    authenticated_username="daniel",
                    action_permissions={"STORNO_DOCUMENT"},
                )
        finally:
            unavailable.rename(self.service)
        c = sqlite3.connect(self.office)
        self.assertEqual(
            c.execute("SELECT status FROM documents WHERE id=?", (invoice_id,)).fetchone()[0],
            "KNJIŽENO",
        )
        self.assertEqual(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        c.close()
        c = sqlite3.connect(self.inventory)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items WHERE id=1").fetchone()[0], 2)
        c.close()

    def test_linked_service_invoice_storno_reopens_service_atomically(self):
        invoice_id, _invoice_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="service-storno-success",
            linked_service_id=7, **self.fiscal(),
        )
        mark_printed(self.office, invoice_id, "DR")
        storno_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            document_id=invoice_id, reason="Pravilna razveljavitev", operator_code="DR",
            authenticated_username="daniel",
            action_permissions={"STORNO_DOCUMENT"},
        )
        c = sqlite3.connect(self.service)
        self.assertEqual(
            c.execute("SELECT invoice_no,office_invoice_id FROM service WHERE id=7").fetchone(),
            (None, None),
        )
        self.assertEqual(
            c.execute("SELECT status,completed_at FROM service_orders WHERE id=70").fetchone(),
            ("OPEN", None),
        )
        c.close()

    def test_split_payment_storno_reverses_every_payment_line(self):
        invoice_id, _invoice_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 40, "BANK_TRANSFER": 60},
            customer={"name": "Kupec"}, operator_code="DR", labor_floor=22,
            issue_token="split-for-storno", bank_transfer_received=True,
            **self.fiscal(),
        )
        mark_printed(self.office, invoice_id, "DR")
        storno_id, _storno_no = storno_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            document_id=invoice_id, reason="Napačen račun", operator_code="DR",
            authenticated_username="daniel",
            action_permissions={"STORNO_DOCUMENT"},
        )
        c = sqlite3.connect(self.office)
        original = c.execute(
            "SELECT method,status,amount FROM pos_payments_v30 "
            "WHERE document_id=? ORDER BY method", (invoice_id,),
        ).fetchall()
        reversed_lines = c.execute(
            "SELECT method,status,amount,reversal_of_id FROM pos_payments_v30 "
            "WHERE document_id=? ORDER BY method", (storno_id,),
        ).fetchall()
        c.close()
        self.assertEqual(
            original,
            [("BANK_TRANSFER", "REVERSED", 60.0), ("CASH", "REVERSED", 40.0)],
        )
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in reversed_lines],
            [("BANK_TRANSFER", "REVERSED", -60.0), ("CASH", "REVERSED", -40.0)],
        )
        self.assertTrue(all(row[3] for row in reversed_lines))

    def test_cashier_without_storno_permission_cannot_change_invoice(self):
        invoice_id, _invoice_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="protected-storno",
            **self.fiscal(),
        )
        mark_printed(self.office, invoice_id, "DR")
        with self.assertRaisesRegex(PosError, "nima dovoljenja"):
            storno_invoice(
                office_db=self.office, inventory_db=self.inventory,
                service_db=self.service, document_id=invoice_id,
                reason="Napačen račun", operator_code="DR",
                authenticated_username="blagajnik", action_permissions={"CASHIER"},
            )
        c = sqlite3.connect(self.office)
        self.assertEqual(
            c.execute("SELECT status FROM documents WHERE id=?", (invoice_id,)).fetchone()[0],
            "KNJIŽENO",
        )
        self.assertEqual(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM pos_payments_v30 WHERE reversal_of_id IS NOT NULL").fetchone()[0],
            0,
        )
        c.close()
        c = sqlite3.connect(self.inventory)
        self.assertEqual(c.execute("SELECT stock_qty FROM inventory_items WHERE id=1").fetchone()[0], 2)
        c.close()

    def test_replacement_invoice_forms_one_traceable_chain(self):
        original_id, original_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="original-for-replacement",
            **self.fiscal(),
        )
        mark_printed(self.office, original_id, "DR")
        _storno_id, storno_no = storno_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            document_id=original_id, reason="Napačen naziv", operator_code="DR",
            authenticated_username="daniel",
            action_permissions={"STORNO_DOCUMENT"},
        )
        eligible = eligible_storned_invoices(self.office)
        self.assertEqual([row["id"] for row in eligible], [original_id])

        replacement_id, _replacement_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="replacement",
            replacement_of_invoice_id=original_id,
            action_permissions={"STORNO_DOCUMENT"}, **self.fiscal(),
        )
        doc, _lines, _payments = invoice_snapshot(self.office, replacement_id)
        self.assertEqual(doc["replacement_of_invoice_id"], original_id)
        self.assertEqual(doc["replacement_original_no"], original_no)
        self.assertEqual(doc["replacement_storno_no"], storno_no)
        self.assertEqual(eligible_storned_invoices(self.office), [])
        c = sqlite3.connect(self.office)
        audit = c.execute(
            "SELECT operator_code,actor_username FROM pos_audit_v30 "
            "WHERE document_id=? AND event_type='REPLACEMENT_INVOICE_LINKED'",
            (replacement_id,),
        ).fetchone()
        c.close()
        self.assertEqual(audit, ("DR", "daniel"))

    def test_replacement_invoice_requires_storno_permission(self):
        original_id, _original_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="replacement-permission-source",
            **self.fiscal(),
        )
        mark_printed(self.office, original_id, "DR")
        storno_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            document_id=original_id, reason="Napačen račun", operator_code="DR",
            authenticated_username="daniel", action_permissions={"STORNO_DOCUMENT"},
        )
        with self.assertRaisesRegex(PosError, "nima dovoljenja"):
            issue_invoice(
                office_db=self.office, inventory_db=self.inventory,
                service_db=self.service, lines=[self.line()], payments={"CASH": 100},
                customer={"name": "Kupec"}, operator_code="DR", labor_floor=22,
                issue_token="unauthorized-replacement",
                replacement_of_invoice_id=original_id,
                authenticated_username="blagajnik", action_permissions={"CASHIER"},
                operator_tax_number="84514841", premise_id="MFD",
                electronic_device_id="PIS",
            )
        c = sqlite3.connect(self.office)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
        c.close()

    def test_replacement_requires_storned_invoice(self):
        original_id, _original_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="not-yet-storned",
            **self.fiscal(),
        )
        mark_printed(self.office, original_id, "DR")
        with self.assertRaisesRegex(PosError, "v celoti storniran"):
            issue_invoice(
                office_db=self.office, inventory_db=self.inventory, service_db=self.service,
                lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
                operator_code="DR", labor_floor=22, issue_token="invalid-replacement",
                replacement_of_invoice_id=original_id,
                action_permissions={"STORNO_DOCUMENT"}, **self.fiscal(),
            )

    def test_fiscal_numbering_is_separate_per_premise_and_snapshotted(self):
        first_id, first_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="mfd-1", **self.fiscal("MFD"),
        )
        self.assertEqual(first_no, "MFD-PIS-1")
        mark_printed(self.office, first_id, "DR")
        second_id, second_no, _ = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="mft-1", **self.fiscal("MFT"),
        )
        self.assertEqual(second_no, "MFT-PIS-1")
        c = sqlite3.connect(self.office)
        row = c.execute(
            "SELECT fiscal_premise, fiscal_device, fiscal_sequence, fiscal_invoice_no, "
            "operator_code_snapshot, operator_tax_number_snapshot "
            "FROM documents WHERE id=?", (second_id,),
        ).fetchone()
        c.close()
        self.assertEqual(row, ("MFT", "PIS", 1, "MFT-PIS-1", "DR", "84514841"))

    def test_operator_tax_number_must_be_eight_digits(self):
        values = self.fiscal()
        values["operator_tax_number"] = ""
        with self.assertRaisesRegex(PosError, "8 številk"):
            issue_invoice(
                office_db=self.office, inventory_db=self.inventory, service_db=self.service,
                lines=[self.line()], payments={"CASH": 100}, customer={"name": "Kupec"},
                operator_code="DR", labor_floor=22, issue_token="invalid-operator", **values,
            )

    def test_split_payment_requires_received_bank_part(self):
        kwargs = dict(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[self.line()], payments={"CASH": 40, "BANK_TRANSFER": 60},
            customer={"name": "Kupec"}, operator_code="DR", labor_floor=22,
            issue_token="split-payment", **self.fiscal(),
        )
        with self.assertRaisesRegex(PosError, "TRR del"):
            issue_invoice(**kwargs)
        document_id, _number, _total = issue_invoice(
            **kwargs, bank_transfer_received=True
        )
        c = sqlite3.connect(self.office)
        status = c.execute(
            "SELECT payment_status FROM documents WHERE id=?", (document_id,)
        ).fetchone()[0]
        payment_statuses = c.execute(
            "SELECT method, status FROM pos_payments_v30 WHERE document_id=? ORDER BY method",
            (document_id,),
        ).fetchall()
        c.close()
        self.assertEqual(status, "PAID")
        self.assertEqual(payment_statuses, [("BANK_TRANSFER", "PAID"), ("CASH", "PAID")])


if __name__ == "__main__":
    unittest.main()

