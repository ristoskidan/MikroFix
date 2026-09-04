import sqlite3
import tempfile
import unittest
from pathlib import Path

from mikrofix_transactions import complete_buyback_atomic
from mikrofix_v29_core import ensure_inventory_schema, ensure_service_schema
from mikrofix_v30_cash import (
    close_cash_session, ensure_cash_schema, open_cash_session, record_cash_entry,
)
from mikrofix_workflows import (
    begin_service_buyback_offer,
    create_buyback,
    create_service_order,
    ensure_workflow_schema,
)


class AtomicBuybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = root / "service.db"
        self.office = root / "office.db"
        self.inventory = root / "inventory.db"
        connection = sqlite3.connect(self.service)
        ensure_workflow_schema(connection)
        connection.close()
        ensure_service_schema(self.service)
        ensure_inventory_schema(self.inventory, {})
        ensure_cash_schema(self.office)

    def tearDown(self):
        self.temp.cleanup()

    def _buyback(self, payout="BANK_TRANSFER", amount=90.0):
        connection = sqlite3.connect(self.service)
        try:
            buyback_id, number = create_buyback(
                connection,
                {
                    "seller_name": "Test Prodajalec",
                    "seller_tax_number": "12345678",
                    "seller_address": "Testna 1",
                    "seller_contact": "040000000",
                    "seller_iban": "SI56040000000000000" if payout == "BANK_TRANSFER" else "",
                    "identity_checked": True,
                    "device_model": "Testni telefon",
                    "device_serial": "SERIAL-TEST-1",
                    "device_imei": "123456789012345",
                    "condition_text": "Testno stanje",
                    "acquisition_method": "KUPLJENA_NOVA",
                    "microdetect_confirmed": True,
                    "account_removed": True,
                    "activation_lock_removed": True,
                    "mdm_clear": True,
                    "ownership_declaration": True,
                    "privacy_notice_received": True,
                    "risk_level": "GREEN",
                    "purchase_price": amount,
                    "payout_method": payout,
                    "operator_code": "DR",
                },
                test_mode=True,
            )
            return buyback_id, number
        finally:
            connection.close()

    def _complete(self, buyback_id):
        return complete_buyback_atomic(
            service_db=self.service,
            office_db=self.office,
            inventory_db=self.inventory,
            buyback_id=buyback_id,
            purpose="REFURBISH",
            buyer={
                "legal_name": "MikroFix",
                "tax_number": "84514841",
                "registration_number": "7565178000",
                "address": "Paradiž 73",
                "representative": "Daniel Ristoski",
            },
            premise_id="MFD",
            device_id="PIS",
            operator_code="DR",
        )

    def test_bank_buyback_is_atomic_and_idempotent(self):
        buyback_id, _number = self._buyback()
        first = self._complete(buyback_id)
        second = self._complete(buyback_id)
        self.assertEqual(first["inventory_code"], second["inventory_code"])
        service = sqlite3.connect(self.service)
        inventory = sqlite3.connect(self.inventory)
        try:
            row = service.execute(
                "SELECT status,payment_status,device_inventory_id FROM buybacks WHERE id=?",
                (buyback_id,),
            ).fetchone()
            self.assertEqual(row[:2], ("COMPLETED", "PAID"))
            self.assertEqual(
                inventory.execute(
                    "SELECT COUNT(*) FROM used_devices WHERE buyback_id=?", (buyback_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                service.execute("SELECT COUNT(*) FROM operation_outbox_v31").fetchone()[0],
                1,
            )
        finally:
            service.close()
            inventory.close()

    def test_cash_failure_changes_none_of_business_tables(self):
        buyback_id, _number = self._buyback("CASH", 50.0)
        with self.assertRaises(Exception):
            self._complete(buyback_id)
        service = sqlite3.connect(self.service)
        inventory = sqlite3.connect(self.inventory)
        try:
            self.assertEqual(
                service.execute(
                    "SELECT status FROM buybacks WHERE id=?", (buyback_id,)
                ).fetchone()[0],
                "CONTRACT_READY",
            )
            self.assertEqual(
                inventory.execute("SELECT COUNT(*) FROM used_devices").fetchone()[0], 0
            )
        finally:
            service.close()
            inventory.close()

    def test_cash_buyback_books_exactly_one_cash_entry(self):
        open_cash_session(
            self.office,
            premise_id="MFD",
            device_id="PIS",
            operator_code="DR",
            opening_cash=200,
        )
        buyback_id, _number = self._buyback("CASH", 50.0)
        self._complete(buyback_id)
        self._complete(buyback_id)
        office = sqlite3.connect(self.office)
        service = sqlite3.connect(self.service)
        try:
            rows = office.execute(
                "SELECT id,session_id,amount,balance_after FROM cash_journal_entries_v30"
            ).fetchall()
            self.assertEqual([(row[2], row[3]) for row in rows], [(-50.0, 150.0)])
            voucher = service.execute(
                "SELECT status,cash_session_id,cash_entry_id,booked_at "
                "FROM cash_disbursements WHERE buyback_id=?",
                (buyback_id,),
            ).fetchone()
            self.assertEqual(voucher[:3], ("BOOKED", rows[0][1], rows[0][0]))
            self.assertTrue(voucher[3])
        finally:
            office.close()
            service.close()

    def test_existing_buyback_in_closed_cash_session_needs_no_new_session(self):
        buyback_id, _number = self._buyback("CASH", 50.0)
        service = sqlite3.connect(self.service)
        voucher_no = service.execute(
            "SELECT voucher_no FROM cash_disbursements WHERE buyback_id=?",
            (buyback_id,),
        ).fetchone()[0]
        service.close()
        session_id, _ = open_cash_session(
            self.office, premise_id="MFD", device_id="PIS",
            operator_code="DR", opening_cash=200,
        )
        office = sqlite3.connect(self.office)
        office.execute("BEGIN IMMEDIATE")
        entry_id = record_cash_entry(
            office, session_id=session_id, entry_type="CASH_BUYBACK", amount=-50,
            reference_type="BUYBACK", reference_id=buyback_id,
            reference_no=voucher_no, description="Obstoječi gotovinski odkup",
            operator_code="DR",
        )
        office.commit(); office.close()
        close_cash_session(
            self.office, premise_id="MFD", device_id="PIS",
            operator_code="DR", counted_cash=150, note="Zaključeno",
        )
        result = self._complete(buyback_id)
        self.assertEqual(result["cash_entry_id"], entry_id)
        office = sqlite3.connect(self.office)
        self.assertEqual(
            office.execute("SELECT COUNT(*) FROM cash_journal_entries_v30").fetchone()[0],
            1,
        )
        office.close()

    def test_service_is_converted_only_with_final_buyback_confirmation(self):
        service = sqlite3.connect(self.service)
        try:
            service.execute(
                """
                CREATE TABLE service(
                    id INTEGER PRIMARY KEY,invoice_no TEXT,office_invoice_id INTEGER,
                    resolution_status TEXT NOT NULL DEFAULT 'AWAITING_INVOICE',
                    converted_buyback_id INTEGER
                )
                """
            )
            service.execute("INSERT INTO service(id) VALUES(1)")
            order_id, _ = create_service_order(
                service,
                {
                    "customer_name": "Test Prodajalec",
                    "customer_phone": "040000000",
                    "device_model": "Testni telefon",
                    "device_serial": "SERIAL-TEST-1",
                    "device_imei": "123456789012345",
                    "device_type": "APPLE_MOBILE",
                    "identity_method": "MICRODETECT",
                    "customer_request": "Popravilo",
                    "contract_context": "MFD",
                    "delivery_channel": "PAPER",
                    "microdetect_confirmed": True,
                    "operator_code": "DR",
                },
                commit=False,
            )
            service.execute(
                "UPDATE service_orders SET service_id=1 WHERE id=?", (order_id,)
            )
            begin_service_buyback_offer(service, order_id, commit=False)
            service.commit()
        finally:
            service.close()
        buyback_id, _ = self._buyback()
        service = sqlite3.connect(self.service)
        try:
            service.execute(
                "UPDATE buybacks SET source_service_order_id=?,source_service_id=1 WHERE id=?",
                (order_id, buyback_id),
            )
            service.commit()
        finally:
            service.close()
        self._complete(buyback_id)
        service = sqlite3.connect(self.service)
        try:
            self.assertEqual(
                service.execute(
                    "SELECT status,buyback_id FROM service_orders WHERE id=?", (order_id,)
                ).fetchone(),
                ("PREUSMERJENO_V_ODKUP", buyback_id),
            )
            self.assertEqual(
                service.execute(
                    "SELECT resolution_status,converted_buyback_id FROM service WHERE id=1"
                ).fetchone(),
                ("CONVERTED_TO_BUYBACK", buyback_id),
            )
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()

