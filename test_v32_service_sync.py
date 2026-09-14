import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mikrofix_service_sync import (
    enqueue_removed_service_part,
    enqueue_service_order_created,
    enqueue_service_technical_completion,
    service_device_id,
)
from mikrofix_service_returns import service_returnable_parts
from mikrofix_sync_store import (
    apply_service_part_return_updates,
    ensure_sync_store_schema,
)
from mikrofix_workflows import (
    create_service_order,
    ensure_workflow_schema,
    finish_service_work_timer,
    get_service_order,
    save_service_order_parts,
    start_service_work_timer,
)


def intake_payload():
    return {
        "customer_name": "Testna stranka",
        "customer_phone": "040000000",
        "customer_email": "",
        "device_model": "Testna naprava",
        "device_serial": "SYNC-LOCAL-1",
        "device_type": "DRUGO",
        "identity_method": "MANUAL_OTHER",
        "manual_identity_confirmed": True,
        "customer_request": "Popravilo",
        "quote_eur": "0",
        "logistics_eur": "0",
        "contract_context": "MFD",
        "delivery_channel": "PAPER",
        "operator_code": "serviser",
    }


class ServiceOutboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_workflow_schema(self.connection)
        ensure_sync_store_schema(self.connection)
        self.connection.execute(
            "CREATE TABLE service_tests(id INTEGER PRIMARY KEY,service_id INTEGER,"
            "stage TEXT,test_name TEXT,status TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE service_photos(id INTEGER PRIMARY KEY,service_id INTEGER,"
            "stage TEXT,rel_path TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE service_manual_lines(id INTEGER PRIMARY KEY,service_id INTEGER,"
            "line_uuid TEXT,line_kind TEXT,description TEXT,quantity REAL,"
            "unit_price REAL,billable_minutes INTEGER,tariff_version TEXT)"
        )
        self.connection.execute(
            "INSERT INTO service_manual_lines(service_id,line_uuid,line_kind,description,"
            "quantity,unit_price,billable_minutes,tariff_version) "
            "VALUES(99,?,'T1','Delo',1,30,30,'T1-v1')",
            (str(uuid4()),),
        )
        self.temp = tempfile.TemporaryDirectory()
        self.device_id = service_device_id("MFD", "PIS")

    def tearDown(self):
        self.temp.cleanup()
        self.connection.close()

    def test_create_and_completion_are_ordered_and_keep_central_version_separate(self):
        order_id, _ = create_service_order(
            self.connection,
            dict(intake_payload(), origin_device_id=self.device_id),
            commit=False,
        )
        record = get_service_order(self.connection, order_id)
        created = enqueue_service_order_created(
            self.connection,
            record,
            actor_id="serviser",
            device_id=self.device_id,
        )
        start_service_work_timer(
            self.connection,
            order_id,
            started_at=datetime(2026, 9, 12, 8, 0),
            operator_code="serviser",
            origin_device_id=self.device_id,
            commit=False,
        )
        save_service_order_parts(
            self.connection,
            order_id,
            [("Baterija", "BAT-1", "BAR-BAT-1", 7, "LOT-1", 49.9)],
            commit=False,
        )
        finish_service_work_timer(
            self.connection,
            order_id,
            ended_at=datetime(2026, 9, 12, 8, 30),
            operator_code="serviser",
            commit=False,
        )
        self.connection.execute(
            "INSERT INTO service_tests(service_id,stage,test_name,status) "
            "VALUES(99,'PO','Polnjenje','OK')"
        )
        completed = enqueue_service_technical_completion(
            self.connection,
            service_order_id=order_id,
            service_id=99,
            completed_at=datetime(2026, 9, 12, 8, 30),
            work_summary="Zamenjana baterija.",
            findings="",
            actor_id="serviser",
            device_id=self.device_id,
            data_dir=Path(self.temp.name),
        )
        self.connection.commit()

        rows = self.connection.execute(
            "SELECT operation_type,device_sequence,expected_version,payload_json "
            "FROM sync_outbox_v32 ORDER BY device_sequence"
        ).fetchall()
        self.assertEqual(created.device_sequence, 1)
        self.assertEqual(completed.device_sequence, 2)
        self.assertEqual([row[0] for row in rows], [
            "CreateServiceOrder", "CompleteServiceTechnically"
        ])
        self.assertEqual([row[2] for row in rows], [0, 1])
        completion_payload = json.loads(rows[1][3])
        self.assertEqual(completion_payload["parts"][0]["barcode"], "BAR-BAT-1")
        self.assertEqual(completion_payload["parts"][0]["quantity"], "1")
        self.assertEqual(completion_payload["intervals"][0]["elapsed_seconds"], 1800)
        self.assertEqual(completion_payload["tests"][0]["status"], "OK")
        self.assertEqual(completion_payload["billing_lines"][0]["unit_price"], "30")
        self.assertGreater(get_service_order(self.connection, order_id)["row_version"], 1)
        self.assertEqual(get_service_order(self.connection, order_id)["sync_state"], "PENDING")

    def test_missing_barcode_cannot_enter_completion_outbox(self):
        order_id, _ = create_service_order(self.connection, intake_payload())
        record = get_service_order(self.connection, order_id)
        enqueue_service_order_created(
            self.connection, record, actor_id="serviser", device_id=self.device_id
        )
        start_service_work_timer(
            self.connection, order_id,
            started_at=datetime(2026, 9, 12, 8, 0), commit=False,
        )
        save_service_order_parts(
            self.connection,
            order_id,
            [("Neidentificiran del", "X", "", 7, "", 1)],
            commit=False,
        )
        finish_service_work_timer(
            self.connection, order_id,
            ended_at=datetime(2026, 9, 12, 8, 1), commit=False,
        )
        with self.assertRaisesRegex(ValueError, "barcode"):
            enqueue_service_technical_completion(
                self.connection,
                service_order_id=order_id,
                service_id=99,
                completed_at=datetime(2026, 9, 12, 8, 1),
                work_summary="Test",
                findings="",
                actor_id="serviser",
                device_id=self.device_id,
                data_dir=Path(self.temp.name),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM sync_outbox_v32 WHERE operation_type="
                "'CompleteServiceTechnically'"
            ).fetchone()[0],
            0,
        )

    def test_removed_part_is_journalled_offline_against_exact_synced_lot(self):
        service_order_id = str(uuid4())
        invoice_id = str(uuid4())
        invoice_line_id = str(uuid4())
        lot_id = str(uuid4())
        apply_service_part_return_updates(
            self.connection,
            central_sequence=10,
            event_type="ServiceBillingCompleted",
            payload={
                "service_order_id": service_order_id,
                "invoice_id": invoice_id,
                "invoice_number": "R-2026-99",
                "returnable_parts": [{
                    "invoice_line_id": invoice_line_id,
                    "lot_id": lot_id,
                    "article_id": str(uuid4()),
                    "barcode": "BAR-BAT-1",
                    "description": "Baterija",
                    "lot_serial": "LOT-1",
                    "allocated_quantity": "1.0000",
                    "already_removed_quantity": "0.0000",
                }],
            },
        )

        envelope = enqueue_removed_service_part(
            self.connection,
            service_order_uuid=service_order_id,
            invoice_id=invoice_id,
            invoice_line_id=invoice_line_id,
            lot_id=lot_id,
            quantity="1",
            condition_note="Odstranjeno ob reklamaciji.",
            actor_id="serviser",
            device_id=self.device_id,
        )

        row = self.connection.execute(
            "SELECT operation_type,aggregate_type,payload_json,status "
            "FROM sync_outbox_v32 WHERE operation_id=?",
            (envelope.operation_id,),
        ).fetchone()
        self.assertEqual(row[0:2], ("ReceiveRemovedServicePart", "STOCK_RETURN"))
        payload = json.loads(row[2])
        self.assertEqual(payload["invoice_id"], invoice_id)
        self.assertEqual(payload["invoice_line_id"], invoice_line_id)
        self.assertEqual(payload["lot_id"], lot_id)
        self.assertEqual(row[3], "PENDING")
        projected = service_returnable_parts(
            self.connection, service_order_uuid=service_order_id
        )[0]
        self.assertEqual(str(projected.pending_local_quantity), "1.0000")
        self.assertEqual(str(projected.removable_quantity), "0.0000")
        with self.assertRaisesRegex(ValueError, "presega"):
            enqueue_removed_service_part(
                self.connection,
                service_order_uuid=service_order_id,
                invoice_id=invoice_id,
                invoice_line_id=invoice_line_id,
                lot_id=lot_id,
                quantity="1",
                condition_note="Ponovljen klik.",
                actor_id="serviser",
                device_id=self.device_id,
            )


if __name__ == "__main__":
    unittest.main()
