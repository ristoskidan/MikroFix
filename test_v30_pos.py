import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mikrofix_v30_pos import (
    PosError, PosLine, abandon_pos_draft, calculate_line, default_service_description,
    customer_partner_by_id, list_customer_partners, save_customer_partner,
    eligible_storned_invoices, ensure_pos_schema, invoice_snapshot, issue_invoice,
    generate_pos_pdfs, generate_pos_preview_pdfs,
    latest_pending_print, load_active_pos_draft, mark_pdf_ready, mark_printed,
    register_copy, save_pos_draft, search_inventory,
    pending_furs_documents, reconcile_pending_furs_document,
    open_sales_claim, search_issued_invoices,
    net_public_pos_lines,
    prepare_used_goods_agreement, set_used_goods_agreement_signed,
    settle_unpaid_bank_invoice, unpaid_bank_invoices,
    service_labor_for_tier_minutes, service_line_public_name,
    service_line_public_quantity, service_tariff_snapshot, storno_invoice,
)
from mikrofix_v30_cash import open_cash_session
from mikrofix_v30_lots import allocate_fifo, ensure_lot_schema


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

    def test_card_payment_is_recorded_without_cash_journal_entry(self):
        document_id, _number, total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()],
            payments={"CARD": "100,00"}, customer={"name": "Testni kupec"},
            operator_code="DR", labor_floor=22, **self.fiscal(),
        )
        connection = sqlite3.connect(self.office)
        payment = connection.execute(
            "SELECT method, amount, status FROM pos_payments_v30 WHERE document_id=?",
            (document_id,),
        ).fetchone()
        cash_sales = connection.execute(
            "SELECT COUNT(*) FROM cash_journal_entries_v30 WHERE entry_type='CASH_SALE'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(total, Decimal("100.00"))
        self.assertEqual(payment, ("CARD", 100.0, "PAID"))
        self.assertEqual(cash_sales, 0)

    def test_invoice_history_search_and_standalone_sales_claim(self):
        document_id, number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CARD": 100},
            customer={"name": "Iskani kupec", "address": "Testna 12"},
            operator_code="DR", labor_floor=22, **self.fiscal(),
        )
        rows = search_issued_invoices(self.office, "Iskani kupec")
        self.assertEqual(rows[0]["id"], document_id)
        self.assertEqual(rows[0]["invoice_no"], number)
        claim_id, claim_no = open_sales_claim(
            self.office, document_id=document_id,
            reason="Naprava se ne vklopi", operator_code="DR",
        )
        second_id, second_no = open_sales_claim(
            self.office, document_id=document_id,
            reason="Ponovljen klik", operator_code="DR",
        )
        self.assertEqual((second_id, second_no), (claim_id, claim_no))

    def test_production_cash_invoice_persists_eor_zoi_and_fiscal_qr(self):
        zoi = "a7e5f55e1dbb48b799268e1a6d8618a3"
        def fiscalizer(request):
            self.assertEqual(request["invoice_amount"], Decimal("100.00"))
            return {
                "status": "CONFIRMED", "zoi": zoi,
                "eor": "11111111-2222-3333-4444-555555555555",
                "message_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "tax_number": 84514841,
                "payload": {"InvoiceRequest": {}}, "response": {"token": "signed"},
            }
        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CASH": 100},
            customer={"name": "Testni kupec"}, operator_code="DR", labor_floor=22,
            issue_token="production-fiscal", fiscalization_required=True,
            fiscalizer=fiscalizer, test_mode=False, **self.fiscal(),
        )
        connection = sqlite3.connect(self.office)
        doc = connection.execute(
            "SELECT fiscal_status,furs_zoi,furs_eor,furs_qr_payload FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
        outbox = connection.execute(
            "SELECT status,document_id FROM furs_outbox_v30 WHERE document_id=?",
            (document_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(doc[:3], (
            "CONFIRMED", zoi, "11111111-2222-3333-4444-555555555555"
        ))
        self.assertEqual(len(doc[3]), 60)
        self.assertTrue(doc[3].isdigit())
        self.assertEqual(outbox, ("CONFIRMED", document_id))

    def test_production_storno_is_fiscalized_with_negative_amount_and_reference(self):
        requests = []

        def fiscalizer(request):
            requests.append(request)
            suffix = len(requests)
            return {
                "status": "CONFIRMED",
                "zoi": f"{suffix:032x}",
                "eor": f"11111111-2222-3333-4444-{suffix:012d}",
                "message_id": f"aaaaaaaa-bbbb-cccc-dddd-{suffix:012d}",
                "tax_number": 84514841,
                "payload": {"InvoiceRequest": {"n": suffix}},
                "response": {"token": f"signed-{suffix}"},
            }

        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CASH": 100},
            customer={"name": "Testni kupec"}, operator_code="DR", labor_floor=22,
            issue_token="production-storno-source", fiscalization_required=True,
            fiscalizer=fiscalizer, test_mode=False, **self.fiscal(),
        )
        storno_id, _storno_no = storno_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, document_id=document_id,
            reason="Napačna postavka", operator_code="DR",
            authenticated_username="daniel",
            action_permissions={"STORNO_DOCUMENT"},
            fiscalization_required=True, fiscalizer=fiscalizer,
            test_mode=False,
        )
        self.assertEqual(len(requests), 2)
        correction = requests[1]
        self.assertEqual(correction["invoice_amount"], Decimal("-100.00"))
        self.assertEqual(correction["payment_amount"], Decimal("-100.00"))
        reference = correction["reference_invoice"]
        self.assertEqual(
            reference["ReferenceInvoiceIdentifier"],
            {
                "BusinessPremiseID": "MFD",
                "ElectronicDeviceID": "PIS",
                "InvoiceNumber": "1",
            },
        )
        connection = sqlite3.connect(self.office)
        storno = connection.execute(
            "SELECT fiscal_status,fiscal_invoice_no,furs_eor,furs_qr_payload "
            "FROM documents WHERE id=?", (storno_id,),
        ).fetchone()
        outbox = connection.execute(
            "SELECT status FROM furs_outbox_v30 WHERE document_id=?", (storno_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(storno[:3], (
            "CONFIRMED", "MFD-PIS-2", "11111111-2222-3333-4444-000000000002",
        ))
        self.assertEqual(len(storno[3]), 60)
        self.assertEqual(outbox, ("CONFIRMED",))

    def test_late_cash_payment_fiscalizes_same_bank_invoice_once(self):
        document_id, invoice_no, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()],
            payments={"BANK_TRANSFER": 100}, customer={"name": "Testni kupec"},
            operator_code="DR", labor_floor=22,
            payment_reference="SI0012345678901234",
            fiscalization_required=True, fiscalizer=lambda _request: None,
            test_mode=False, **self.fiscal(),
        )
        self.assertEqual([row["id"] for row in unpaid_bank_invoices(self.office)], [document_id])
        requests = []

        def fiscalizer(request):
            requests.append(request)
            return {
                "status": "CONFIRMED", "zoi": "1" * 32,
                "eor": "11111111-2222-3333-4444-555555555555",
                "message_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "tax_number": 84514841,
                "payload": {"InvoiceRequest": {}}, "response": {"token": "signed"},
            }

        result = settle_unpaid_bank_invoice(
            office_db=self.office, document_id=document_id, method="CASH",
            operator_code="DR", fiscalization_required=True, fiscalizer=fiscalizer,
            authenticated_username="daniel", test_mode=False,
        )
        self.assertEqual(result["fiscal_status"], "CONFIRMED")
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["subsequent_submit"])
        self.assertEqual(requests[0]["public_invoice_number"], invoice_no)
        connection = sqlite3.connect(self.office)
        document = connection.execute(
            "SELECT COUNT(*),payment_status,payment_method,fiscal_status,furs_eor "
            "FROM documents WHERE id=?", (document_id,),
        ).fetchone()
        methods = connection.execute(
            "SELECT method,status,amount FROM pos_payments_v30 WHERE document_id=? ORDER BY id",
            (document_id,),
        ).fetchall()
        all_documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        connection.close()
        self.assertEqual(document, (
            1, "PAID", "CASH", "CONFIRMED",
            "11111111-2222-3333-4444-555555555555",
        ))
        self.assertEqual(all_documents, 1)
        self.assertEqual(methods, [
            ("BANK_TRANSFER", "REPLACED_UNPAID", 100.0),
            ("CASH", "PAID", 100.0),
        ])
        self.assertEqual(unpaid_bank_invoices(self.office), [])
        repeated = settle_unpaid_bank_invoice(
            office_db=self.office, document_id=document_id, method="CASH",
            operator_code="DR", fiscalization_required=True, fiscalizer=fiscalizer,
            authenticated_username="daniel", test_mode=False,
        )
        self.assertEqual(repeated["status"], "ALREADY_PAID")
        self.assertEqual(len(requests), 1)

    def test_pending_furs_request_retries_same_payload_and_unblocks_queue(self):
        zoi = "2" * 32
        message_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        payload = {
            "InvoiceRequest": {
                "Header": {"MessageID": message_id},
                "Invoice": {"ProtectedID": zoi, "TaxNumber": 84514841},
            }
        }

        def uncertain(_request):
            return {
                "status": "PENDING_RECONCILIATION", "zoi": zoi, "eor": "",
                "message_id": message_id, "tax_number": 84514841,
                "payload": payload, "response": {},
                "error_code": "CONNECTION_FAILED", "error_message": "timeout",
            }

        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CASH": 100},
            customer={"name": "Testni kupec"}, operator_code="DR", labor_floor=22,
            fiscalization_required=True, fiscalizer=uncertain,
            test_mode=False, **self.fiscal(),
        )
        mark_pdf_ready(self.office, document_id, "DR")
        message_id_2 = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        payload_2 = {
            "InvoiceRequest": {
                "Header": {"MessageID": message_id_2},
                "Invoice": {"ProtectedID": "3" * 32, "TaxNumber": 84514841},
            }
        }

        def uncertain_2(_request):
            return {
                "status": "PENDING_RECONCILIATION", "zoi": "3" * 32, "eor": "",
                "message_id": message_id_2, "tax_number": 84514841,
                "payload": payload_2, "response": {},
                "error_code": "CONNECTION_FAILED", "error_message": "offline",
            }

        second_id, _second_number, _second_total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CASH": 100},
            customer={"name": "Drugi kupec"}, operator_code="DR", labor_floor=22,
            fiscalization_required=True, fiscalizer=uncertain_2,
            test_mode=False, **self.fiscal(),
        )
        self.assertEqual(
            [row["id"] for row in pending_furs_documents(self.office)],
            [document_id, second_id],
        )

        class Reconciler:
            seen = None

            def retry_existing_payload(self, saved):
                self.seen = saved
                return {
                    "status": "CONFIRMED", "eor": "99999999-2222-3333-4444-555555555555",
                    "response": {"token": "signed"}, "error_code": "", "error_message": "",
                }

        reconciler = Reconciler()
        result = reconcile_pending_furs_document(
            office_db=self.office, document_id=document_id, fiscalizer=reconciler,
            operator_code="DR", authenticated_username="daniel",
        )
        self.assertEqual(reconciler.seen, payload)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(
            [row["id"] for row in pending_furs_documents(self.office)], [second_id]
        )
        connection = sqlite3.connect(self.office)
        row = connection.execute(
            "SELECT d.fiscal_status,d.furs_eor,f.status,f.attempt_count "
            "FROM documents d JOIN furs_outbox_v30 f ON f.document_id=d.id WHERE d.id=?",
            (document_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(row, (
            "CONFIRMED", "99999999-2222-3333-4444-555555555555", "CONFIRMED", 2,
        ))
        reconcile_pending_furs_document(
            office_db=self.office, document_id=second_id, fiscalizer=reconciler,
            operator_code="DR", authenticated_username="daniel",
        )
        self.assertEqual(reconciler.seen, payload_2)
        self.assertEqual(pending_furs_documents(self.office), [])

    @unittest.skipUnless(
        shutil.which("pdftoppm") and shutil.which("zbarimg"),
        "PDF/QR decoder ni nameščen",
    )
    def test_thermal_bank_receipt_contains_complete_scannable_upn_qr(self):
        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()],
            payments={"BANK_TRANSFER": 100},
            customer={"name": "Testni kupec", "address": "Cesta 1"},
            operator_code="DR", labor_floor=22,
            payment_reference="SI0012345678901234", **self.fiscal(),
        )
        output = Path(self.tmp.name) / "pdf"
        _a4, thermal = generate_pos_pdfs(
            office_db=self.office, document_id=document_id, output_dir=output,
            company={
                "legal_name": "MikroFix, Ristoski Daniel s.p.",
                "short_name": "MikroFix",
                "address": "Paradiž 73, 2282 Cirkulane",
                "street": "Paradiž 73", "city": "2282 Cirkulane",
                "tax_number": "84514841", "registration_number": "7565178000",
                "iban": "SI56040000283154872",
            },
            tax_note="Oproščeno po 94. členu ZDDV-1.",
            premise="MFD", device="PIS", paper_width_mm=80, test_mode=True,
        )
        from pypdf import PdfReader
        thermal_text = "\n".join(
            page.extract_text() or "" for page in PdfReader(str(thermal)).pages
        )
        self.assertIn("Kupec: Testni kupec", thermal_text)
        self.assertIn("Cesta 1", thermal_text)
        image = output / "thermal"
        subprocess.run(
            ["pdftoppm", "-png", "-r", "300", "-singlefile", str(thermal), str(image)],
            check=True, capture_output=True,
        )
        decoded = subprocess.run(
            ["zbarimg", "--raw", str(image.with_suffix(".png"))],
            check=True, capture_output=True,
        ).stdout.decode("utf-8").splitlines()
        self.assertEqual(decoded[0], "UPNQR")
        self.assertEqual(decoded[8], "00000010000")
        self.assertEqual(decoded[14], "SI56040000283154872")
        self.assertEqual(decoded[15], "SI0012345678901234")

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

    def test_customer_can_be_saved_in_an_empty_unissued_draft(self):
        payload = {
            "schema_version": 1,
            "lines": [],
            "customer": {
                "name": "Kupec obnovljene naprave",
                "address": "Glavna cesta 1",
                "phone": "040 000 000",
            },
        }
        save_pos_draft(
            self.office, issue_token="empty-customer", operator_code="DR",
            actor_username="daniel", premise_id="MFD", device_id="PIS",
            payload=payload,
        )
        loaded = load_active_pos_draft(
            self.office, actor_username="daniel", device_id="PIS"
        )
        self.assertEqual(loaded["payload"], payload)

    def test_f9_customer_partner_is_created_selected_and_linked_to_invoice(self):
        customer = {
            "name": "Kupec za garancijo", "address": "Glavna cesta 1",
            "phone": "040 123 456", "email": "kupec@example.invalid",
            "tax_number": "12345678",
        }
        partner_id = save_customer_partner(self.office, customer)
        saved = customer_partner_by_id(self.office, partner_id)
        self.assertEqual(saved["name"], customer["name"])
        self.assertEqual(saved["address"], customer["address"])
        self.assertIn(partner_id, [row["id"] for row in list_customer_partners(self.office)])
        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CASH": 100},
            customer=customer, customer_partner_id=partner_id,
            operator_code="DR", labor_floor=22, issue_token="f9-partner-link",
            **self.fiscal(),
        )
        connection = sqlite3.connect(self.office)
        linked = connection.execute(
            "SELECT partner_id,partner_name_snapshot FROM documents WHERE id=?",
            (document_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(linked, (partner_id, customer["name"]))

    def test_used_goods_agreement_and_invoice_share_f9_partner(self):
        customer = {
            "name": "Kupec obnovljene naprave", "address": "Garancijska 12",
            "phone": "040 987 654", "email": "garancija@example.invalid",
            "tax_number": "",
        }
        partner_id = save_customer_partner(self.office, customer)
        connection = sqlite3.connect(self.inventory)
        connection.execute("ALTER TABLE used_devices ADD COLUMN imei TEXT")
        connection.execute("ALTER TABLE used_devices ADD COLUMN model TEXT")
        connection.execute("ALTER TABLE used_devices ADD COLUMN storage TEXT")
        connection.execute(
            "UPDATE inventory_items SET source_kind='BUYBACK_DEVICE',source_id=1 WHERE id=1"
        )
        connection.execute(
            "INSERT INTO used_devices(id,status,buyer_name,sale_document_id,warranty_until,updated_at,imei,model,storage) "
            "VALUES(1,'READY_FOR_SALE',NULL,NULL,NULL,'x','TEST-IMEI','iPhone TEST','128 GB')"
        )
        connection.commit(); connection.close()
        agreement_id, _agreement_no, _path = prepare_used_goods_agreement(
            office_db=self.office, inventory_db=self.inventory, article_ids=[1],
            customer=customer, customer_partner_id=partner_id,
            issue_token="used-f9-link", output_dir=Path(self.tmp.name) / "agreements",
            company={"legal_name": "MikroFix", "street": "Paradiž 73", "city": "2282 Cirkulane"},
            operator_code="DR",
        )
        set_used_goods_agreement_signed(self.office, agreement_id, True)
        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[self.line()], payments={"CASH": 100},
            customer=customer, customer_partner_id=partner_id,
            used_goods_12m_signed=True, used_goods_agreement_id=agreement_id,
            operator_code="DR", labor_floor=22, issue_token="used-f9-link",
            **self.fiscal(),
        )
        connection = sqlite3.connect(self.office)
        agreement = connection.execute(
            "SELECT customer_partner_id,sale_document_id FROM used_goods_agreements_v30 WHERE id=?",
            (agreement_id,),
        ).fetchone()
        invoice_partner = connection.execute(
            "SELECT partner_id FROM documents WHERE id=?", (document_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(agreement, (partner_id, document_id))
        self.assertEqual(invoice_partner, partner_id)

    def test_manual_t1_t2_minutes_round_to_started_half_hour(self):
        settings = {"pricing": {
            "t1_hour_eur": 30, "onsite_first_hour_eur": 90,
            "extra_half_hour_eur": 15,
        }}
        t1 = service_labor_for_tier_minutes("T1", 61, settings)
        t2 = service_labor_for_tier_minutes("T2", 91, settings)
        self.assertEqual((t1["billable_minutes"], t1["price"]), (90, Decimal("45")))
        self.assertEqual((t2["billable_minutes"], t2["price"]), (120, Decimal("120")))

    def test_unique_pulled_piece_barcode_is_searchable_in_pos(self):
        c = sqlite3.connect(self.inventory)
        c.execute("ALTER TABLE inventory_items ADD COLUMN category TEXT DEFAULT ''")
        c.execute("ALTER TABLE inventory_items ADD COLUMN supplier_sku TEXT DEFAULT ''")
        c.commit(); c.close()
        search_inventory(self.inventory)
        c = sqlite3.connect(self.inventory)
        c.execute(
            "UPDATE inventory_lots_v30 SET lot_serial='PP00012301' WHERE article_id=1"
        )
        c.commit(); c.close()
        rows = search_inventory(self.inventory, "PP00012301")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[0]["requested_lot_serial"], "PP00012301")

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

    def test_internal_negative_correction_becomes_one_positive_public_line(self):
        public = net_public_pos_lines([
            self.line(quantity=Decimal("3")),
            self.line(quantity=Decimal("-1"), origin_kind="POS_CORRECTION"),
        ])
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0].quantity, Decimal("2.00"))
        with self.assertRaises(PosError):
            net_public_pos_lines([
                self.line(quantity=Decimal("1")),
                self.line(quantity=Decimal("-2"), origin_kind="POS_CORRECTION"),
            ])

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

    def test_service_tariff_keeps_time_rates_and_version_for_document(self):
        tariff = service_tariff_snapshot("T1", 2, {"pricing": {
            "t1_hour_eur": 30, "extra_half_hour_eur": 15,
            "tariff_version": "T1-test-v1",
        }})
        line = PosLine(
            line_kind="SERVICE", name="Menjava procesorja", service_tier="T1",
            base_unit_price=tariff["price"],
            service_billable_minutes=tariff["billable_minutes"],
            service_tariff_version=tariff["tariff_version"],
            service_first_rate=tariff["first_rate"],
            service_extra_half_hour_rate=tariff["extra_half_hour_rate"],
        )
        self.assertEqual(tariff["price"], Decimal("60.00"))
        self.assertEqual(tariff["billable_minutes"], 120)
        self.assertIn("1 začeta ura + 2 × 30 min", service_line_public_name(line))
        self.assertIn("30.00 EUR + 2 × 15.00 EUR", service_line_public_name(line))
        self.assertIn("T1-test-v1", service_line_public_name(line))
        self.assertEqual(service_line_public_quantity(line), "120 min")

    def test_issued_service_line_persists_tariff_snapshot(self):
        tariff = service_tariff_snapshot("T2", 1, {"pricing": {
            "onsite_first_hour_eur": 90, "extra_half_hour_eur": 15,
            "tariff_version": "T2-test-v1",
        }})
        line = PosLine(
            line_kind="SERVICE", name="Delo na lokaciji", code="T2",
            service_tier="T2", base_unit_price=tariff["price"],
            service_billable_minutes=tariff["billable_minutes"],
            service_tariff_version=tariff["tariff_version"],
            service_first_rate=tariff["first_rate"],
            service_extra_half_hour_rate=tariff["extra_half_hour_rate"],
        )
        document_id, _number, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory,
            service_db=self.service, lines=[line], payments={"CARD": "105.00"},
            customer={"name": "Testni kupec"}, operator_code="DR",
            labor_floor=22, issue_token="tariff-snapshot", **self.fiscal(),
        )
        _doc, lines, _payments = invoice_snapshot(self.office, document_id)
        self.assertEqual(lines[0]["service_billable_minutes"], 90)
        self.assertEqual(lines[0]["service_tariff_version"], "T2-test-v1")
        self.assertEqual(lines[0]["service_first_rate"], 90)
        self.assertEqual(lines[0]["service_extra_half_hour_rate"], 15)

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

    def test_reserved_service_part_is_consumed_on_invoice_and_same_lot_is_reserved_on_storno(self):
        c = sqlite3.connect(self.service)
        cursor = c.execute(
            "INSERT INTO service_parts(service_id,article_id,barcode,part_name,part_id,unit_price) "
            "VALUES(7,1,'TEST-1','Testni zaslon','LCD',100)"
        )
        service_part_id = int(cursor.lastrowid)
        c.commit(); c.close()

        c = sqlite3.connect(self.inventory)
        ensure_lot_schema(c)
        allocate_fifo(
            c, schema="main", article_id=1, quantity=1,
            allocation_type="SERVICE_RESERVATION", source_id=7,
            source_line_id=service_part_id, created_at="2026-09-05T08:00:00",
        )
        c.execute("UPDATE inventory_items SET reserved_qty=1 WHERE id=1")
        c.commit(); c.close()

        line = self.service_part_line(
            origin_id=service_part_id,
            stock_already_consumed=False,
        )
        invoice_id, _invoice_no, _total = issue_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            lines=[line], payments={"CASH": 100}, customer={"name": "Kupec"},
            operator_code="DR", labor_floor=22, issue_token="reserved-service-part",
            linked_service_id=7, **self.fiscal(),
        )
        c = sqlite3.connect(self.inventory)
        self.assertEqual(
            c.execute("SELECT stock_qty,reserved_qty FROM inventory_items WHERE id=1").fetchone(),
            (2.0, 0.0),
        )
        self.assertEqual(
            c.execute(
                "SELECT allocation_type,lot_serial FROM inventory_lot_allocations_v30"
            ).fetchone(),
            ("POS_INVOICE", "LOT-1"),
        )
        c.close()

        mark_printed(self.office, invoice_id, "DR")
        storno_invoice(
            office_db=self.office, inventory_db=self.inventory, service_db=self.service,
            document_id=invoice_id, reason="Ponovno odpri servis", operator_code="DR",
            authenticated_username="daniel", action_permissions={"STORNO_DOCUMENT"},
        )
        c = sqlite3.connect(self.inventory)
        self.assertEqual(
            c.execute("SELECT stock_qty,reserved_qty FROM inventory_items WHERE id=1").fetchone(),
            (3.0, 1.0),
        )
        self.assertEqual(
            c.execute(
                "SELECT allocation_type,source_id,source_line_id,lot_serial "
                "FROM inventory_lot_allocations_v30"
            ).fetchone(),
            ("SERVICE_RESERVATION", 7, service_part_id, "LOT-1"),
        )
        c.close()

        c = sqlite3.connect(self.service)
        self.assertEqual(
            c.execute("SELECT status FROM service_orders WHERE id=70").fetchone()[0],
            "OPEN",
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

    def test_receipt_preview_renders_exact_brand_and_larger_header(self):
        from pypdf import PdfReader

        output = Path(self.tmp.name) / "preview"
        _a4, receipt = generate_pos_preview_pdfs(
            lines=[self.line()], payments={"CASH": Decimal("100")},
            customer={"name": "NI STRANKE"}, labor_floor=22, output_dir=output,
            company={"short_name": "MikroFix", "legal_name": "MikroFix Daniel Ristoski s.p."},
            premise="TEST", device="TEST", token="brand-preview", paper_width_mm=80,
        )
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(receipt)).pages)
        self.assertIn("MikroFix", text)
        self.assertEqual(text.count("MikroFix"), 2)
        self.assertNotIn("MIKROFIX", text)
        self.assertIn("PREDOGLED", text)


if __name__ == "__main__":
    unittest.main()
