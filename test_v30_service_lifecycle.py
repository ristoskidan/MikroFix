import sqlite3
import tempfile
import unittest
from pathlib import Path

from mikrofix_invoice_bridge import ensure_service_invoice_schema, mark_service_invoiced
from mikrofix_workflows import (
    create_service_order,
    ensure_workflow_schema,
    link_service_result,
    list_running_service_work_sessions,
    load_service_order_parts,
    save_service_order_parts,
    start_service_work_timer,
    pause_service_work_timer,
)


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

    def _create_open_order(self, serial):
        order_id, _ = create_service_order(
            self.connection,
            {
                "customer_name": "Stranka",
                "customer_phone": "040000000",
                "device_model": "Telefon",
                "device_serial": serial,
                "device_type": "TELEFON",
                "identity_method": "MANUAL_OTHER",
                "manual_identity_confirmed": True,
                "customer_request": "Diagnostika",
                "contract_context": "MFD",
                "delivery_channel": "PAPER",
                "operator_code": "DR",
            },
        )
        return order_id

    def test_open_service_parts_survive_workspace_reopen(self):
        order_id = self._create_open_order("SN-002")
        expected = [
            ("Lightning konektor", "CHARGING_PORT", "383000000001", 42, "LOT-7", 39.9),
            ("Tesnilo", "SEAL", "383000000002", 43, "", 4.5),
        ]

        save_service_order_parts(self.connection, order_id, expected)

        self.assertEqual(load_service_order_parts(self.connection, order_id), expected)

    def test_saving_open_service_parts_replaces_removed_lines(self):
        order_id = self._create_open_order("SN-003")
        first = ("Baterija", "BATTERY", "383000000003", 44, "LOT-8", 29.0)
        removed = ("Zaslon", "DISPLAY", "383000000004", 45, "LOT-9", 89.0)
        save_service_order_parts(self.connection, order_id, [first, removed])

        save_service_order_parts(self.connection, order_id, [first])

        self.assertEqual(load_service_order_parts(self.connection, order_id), [first])

    def test_running_service_session_blocks_close_until_timer_is_paused(self):
        order_id = self._create_open_order("SN-AKTIVNA")
        start_service_work_timer(self.connection, order_id, operator_code="DR")

        running = list_running_service_work_sessions(self.connection)
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["service_order_id"], order_id)
        self.assertEqual(running[0]["device_model"], "Telefon")

        pause_service_work_timer(self.connection, order_id, operator_code="DR")
        self.assertEqual(list_running_service_work_sessions(self.connection), [])


if __name__ == "__main__":
    unittest.main()
