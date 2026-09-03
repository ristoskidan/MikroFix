import sqlite3
import unittest

from mikrofix_workflows import (
    begin_service_buyback_offer,
    cancel_service_buyback_offer,
    convert_service_order_to_buyback,
    create_service_order,
    ensure_workflow_schema,
    link_service_result,
)


class ServiceBuybackConversionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_workflow_schema(self.connection)
        self.connection.executescript(
            """
            CREATE TABLE service(
                id INTEGER PRIMARY KEY,
                invoice_no TEXT,
                office_invoice_id INTEGER,
                resolution_status TEXT NOT NULL DEFAULT 'AWAITING_INVOICE',
                converted_buyback_id INTEGER
            );
            CREATE TABLE service_parts(
                id INTEGER PRIMARY KEY,
                service_id INTEGER NOT NULL,
                part_name TEXT,
                unit_price REAL
            );
            """
        )
        self.order_id, _ = create_service_order(
            self.connection,
            {
                "customer_name": "Test Stranka",
                "customer_phone": "040000000",
                "device_model": "Test telefon",
                "device_serial": "SER-SERVICE-1",
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
        self.connection.execute("INSERT INTO service(id) VALUES(1)")
        self.connection.execute(
            "INSERT INTO service_parts(service_id,part_name,unit_price) VALUES(1,'Vgrajen del',40)"
        )
        link_service_result(self.connection, self.order_id, 1, commit=False)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def _buyback_data(self):
        return {
            "seller_name": "Test Stranka",
            "seller_tax_number": "12345678",
            "seller_address": "Testna 1",
            "seller_contact": "040000000",
            "seller_iban": "SI56040000000000000",
            "identity_checked": True,
            "device_model": "Test telefon",
            "device_serial": "SER-SERVICE-1",
            "device_imei": "123456789012345",
            "condition_text": "Naprava po začetem servisu",
            "acquisition_method": "KUPLJENA_NOVA",
            "microdetect_confirmed": True,
            "account_removed": True,
            "activation_lock_removed": True,
            "mdm_clear": True,
            "ownership_declaration": True,
            "privacy_notice_received": True,
            "risk_level": "GREEN",
            "purchase_price": 120,
            "payout_method": "BANK_TRANSFER",
            "operator_code": "DR",
        }

    def test_rejected_offer_returns_to_open(self):
        begin_service_buyback_offer(self.connection, self.order_id)
        cancel_service_buyback_offer(self.connection, self.order_id)
        status = self.connection.execute(
            "SELECT status FROM service_orders WHERE id=?", (self.order_id,)
        ).fetchone()[0]
        self.assertEqual(status, "OPEN")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM buybacks").fetchone()[0], 0)

    def test_signed_conversion_keeps_part_trace_and_blocks_invoice_path(self):
        begin_service_buyback_offer(self.connection, self.order_id)
        buyback_id, number = convert_service_order_to_buyback(
            self.connection,
            self.order_id,
            self._buyback_data(),
            test_mode=True,
            installed_parts_cost=40,
            commercial_internal_labor_eur=15,
        )
        again = convert_service_order_to_buyback(
            self.connection,
            self.order_id,
            self._buyback_data(),
            test_mode=True,
            installed_parts_cost=40,
            commercial_internal_labor_eur=15,
        )
        self.assertEqual(again, (buyback_id, number))
        order = self.connection.execute(
            "SELECT status,buyback_id FROM service_orders WHERE id=?", (self.order_id,)
        ).fetchone()
        self.assertEqual(order, ("PREUSMERJENO_V_ODKUP", buyback_id))
        service = self.connection.execute(
            "SELECT resolution_status,converted_buyback_id FROM service WHERE id=1"
        ).fetchone()
        self.assertEqual(service, ("CONVERTED_TO_BUYBACK", buyback_id))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM service_parts").fetchone()[0], 1)
        costs = self.connection.execute(
            "SELECT installed_parts_cost,commercial_internal_labor_eur,accounting_owner_labor_eur "
            "FROM buybacks WHERE id=?", (buyback_id,)
        ).fetchone()
        self.assertEqual(costs, (40.0, 15.0, 0.0))

    def test_invoiced_service_cannot_be_converted(self):
        self.connection.execute(
            "UPDATE service SET invoice_no='MFD-PIS-1',office_invoice_id=5 WHERE id=1"
        )
        self.connection.commit()
        begin_service_buyback_offer(self.connection, self.order_id)
        with self.assertRaisesRegex(Exception, "storno"):
            convert_service_order_to_buyback(
                self.connection, self.order_id, self._buyback_data(), test_mode=True
            )


if __name__ == "__main__":
    unittest.main()
