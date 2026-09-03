import sqlite3
import tempfile
import unittest
from pathlib import Path

from mikrofix_invoice_bridge import ensure_service_invoice_schema, mark_service_invoiced
from mikrofix_workflows import create_service_order, ensure_workflow_schema, link_service_result


class V30ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "service.db"
        self.connection = sqlite3.connect(self.db)
        self.connection.execute(
            """
            CREATE TABLE service(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_no TEXT, customer TEXT, customer_address TEXT,
                customer_phone TEXT, model TEXT, serial TEXT, work TEXT,
                service_order_id INTEGER, invoice_no TEXT
            )
            """
        )
        ensure_workflow_schema(self.connection)
        ensure_service_invoice_schema(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_service_stays_in_progress_until_invoice_is_issued(self):
        order_id, _ = create_service_order(
            self.connection,
            {
                "customer_name": "Stranka",
                "customer_phone": "040000000",
                "device_model": "Računalnik",
                "device_serial": "PC-001",
                "device_type": "PC",
                "identity_method": "MANUAL_OTHER",
                "manual_identity_confirmed": True,
                "customer_request": "Menjava procesorja",
                "contract_context": "MFD",
                "delivery_channel": "SMS_PDF",
                "operator_code": "DR",
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO service(
                service_no, customer, customer_phone, model, serial, work,
                service_order_id, pricing_mode
            ) VALUES('MF-2026-0001','Stranka','040000000','Računalnik','PC-001',
                     'Menjava procesorja',?,'MANUAL_LINES_V29')
            """,
            (order_id,),
        )
        service_id = int(cursor.lastrowid)
        link_service_result(self.connection, order_id, service_id)
        row = self.connection.execute(
            "SELECT status, service_id, completed_at FROM service_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        self.assertEqual(row, ("OPEN", service_id, None))
        mark_service_invoiced(self.connection, service_id, "IRA-2026-0001", 10)
        row = self.connection.execute(
            "SELECT status, service_id, completed_at FROM service_orders WHERE id=?",
            (order_id,),
        ).fetchone()
        self.assertEqual(row[0], "COMPLETED")
        self.assertEqual(row[1], service_id)
        self.assertTrue(row[2])


if __name__ == "__main__":
    unittest.main()
