import sqlite3
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from mikrofix_sync_client import (
    CentralSessionExpired,
    RequestsSyncTransport,
    SyncTransportError,
    synchronize_once,
)
from mikrofix_sync_contracts import SyncOperationEnvelope
from mikrofix_sync_store import (
    enqueue_sync_operation,
    ensure_sync_store_schema,
    list_open_sync_conflicts,
    outbox_operation,
)
from mikrofix_workflows import create_service_order, ensure_workflow_schema, get_service_order


DEVICE_ID = "SERVICE-PC-TEST"


class RequestsSyncTransportSessionTests(unittest.TestCase):
    def test_http_401_reports_expiry_once_and_raises_specific_error(self):
        expired = Mock()
        response = Mock(status_code=401)
        transport = RequestsSyncTransport(
            "https://central.example",
            "expired-token",
            on_session_expired=expired,
        )

        with self.assertRaisesRegex(CentralSessionExpired, "seja je potekla"):
            transport._json_response(response)
        with self.assertRaises(CentralSessionExpired):
            transport._json_response(response)

        expired.assert_called_once_with()

    def test_http_403_does_not_report_session_expiry(self):
        expired = Mock()
        response = Mock(status_code=403)
        transport = RequestsSyncTransport(
            "https://central.example", "token", on_session_expired=expired
        )

        with self.assertRaisesRegex(SyncTransportError, "HTTP 403"):
            transport._json_response(response)

        expired.assert_not_called()


class FlakyTransport:
    def __init__(self):
        self.requests = []
        self.fail_first = True

    def submit_operation(self, operation):
        self.requests.append(operation)
        if self.fail_first:
            self.fail_first = False
            raise SyncTransportError("simulated lost response")
        return {
            "operation_id": operation["operation_id"],
            "result_code": "ACCEPTED",
            "aggregate_id": operation["aggregate_id"],
            "aggregate_version": 1,
            "central_sequence": 3,
            "error_code": None,
            "error_message": None,
        }

    def pull_changes(self, *, after, limit):
        return {"after": after, "next_checkpoint": after, "changes": []}

    def pull_inventory_snapshot(self):
        return {
            "complete": True, "snapshot_sequence": 0,
            "inventory_updates": [],
        }

    def pull_service_return_snapshot(self):
        return {
            "complete": True, "snapshot_sequence": 0,
            "returnable_parts": [],
        }


class MalformedChangeTransport(FlakyTransport):
    def __init__(self):
        super().__init__()
        self.fail_first = False

    def pull_changes(self, *, after, limit):
        aggregate_id = str(uuid4())
        changes = [
            {
                "central_sequence": after + 1,
                "event_id": str(uuid4()),
                "event_type": "ServiceOrderCreated",
                "aggregate_type": "SERVICE_ORDER",
                "aggregate_id": aggregate_id,
                "aggregate_version": 1,
                "payload": {"sync_uuid": aggregate_id},
            }
        ]
        return {
            "after": after,
            "next_checkpoint": after + 99,
            "changes": changes,
        }


class StockConflictTransport(FlakyTransport):
    def __init__(self):
        super().__init__()
        self.fail_first = False

    def submit_operation(self, operation):
        return {
            "operation_id": operation["operation_id"],
            "result_code": "REJECTED",
            "aggregate_id": operation["aggregate_id"],
            "aggregate_version": None,
            "central_sequence": None,
            "error_code": "OFFLINE_STOCK_CONFLICT",
            "error_message": "Insufficient stock.",
            "result": {"conflicting_barcodes": ["BAR-1"]},
        }


class RemovedPartConflictTransport(StockConflictTransport):
    def submit_operation(self, operation):
        return {
            "operation_id": operation["operation_id"],
            "result_code": "REJECTED",
            "aggregate_id": operation["aggregate_id"],
            "aggregate_version": None,
            "central_sequence": None,
            "error_code": "PHYSICAL_RETURN_EXCEEDS_SOLD_QUANTITY",
            "error_message": "The exact invoice line and LOT are no longer eligible.",
            "result": {},
        }


class AcceptedTransport(FlakyTransport):
    def __init__(self, version=1):
        super().__init__()
        self.fail_first = False
        self.version = version

    def submit_operation(self, operation):
        return {
            "operation_id": operation["operation_id"],
            "result_code": "ACCEPTED",
            "aggregate_id": operation["aggregate_id"],
            "aggregate_version": self.version,
            "central_sequence": 1,
            "error_code": None,
            "error_message": None,
            "result": {"aggregate_version": self.version},
        }


class InventoryChangeTransport(AcceptedTransport):
    def __init__(self, article_id):
        super().__init__()
        self.article_id = article_id

    def pull_changes(self, *, after, limit):
        return {
            "after": after, "next_checkpoint": after + 1,
            "changes": [{
                "central_sequence": after + 1, "event_id": str(uuid4()),
                "event_type": "PurchaseReceiptConfirmed",
                "aggregate_type": "PURCHASE_RECEIPT",
                "aggregate_id": str(uuid4()), "aggregate_version": 1,
                "occurred_at": "2026-09-12T12:00:00+02:00",
                "payload": {"inventory_updates": [{
                    "article_id": self.article_id, "main_barcode": "MAIN-1",
                    "description": "Baterija", "track_mode": "QUANTITY",
                    "quantity_available": "2.0000",
                    "current_sale_price": "15.00", "row_version": 2,
                    "blocked": False, "blocked_reason": "",
                    "identities": [{
                        "identity_id": str(uuid4()),
                        "supplier_barcode": "SUP-BARCODE-1",
                        "quantity_available": "2.0000",
                    }],
                }]},
            }],
        }

    def pull_inventory_snapshot(self):
        return {
            "complete": True, "snapshot_sequence": 1,
            "inventory_updates": [{
                "article_id": self.article_id, "main_barcode": "MAIN-1",
                "description": "Baterija", "track_mode": "QUANTITY",
                "quantity_available": "2.0000",
                "current_sale_price": "15.00", "row_version": 2,
                "blocked": False, "blocked_reason": "",
                "identities": [{
                    "identity_id": str(uuid4()),
                    "supplier_barcode": "SUP-BARCODE-1",
                    "quantity_available": "2.0000",
                }],
            }],
        }


class SyncClientTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_sync_store_schema(self.connection)
        self.aggregate_id = str(uuid4())
        self.operation_id = str(uuid4())
        envelope = SyncOperationEnvelope(
            operation_id=self.operation_id,
            operation_type="CreateServiceOrder",
            aggregate_type="SERVICE_ORDER",
            aggregate_id=self.aggregate_id,
            expected_version=0,
            actor_id="serviser",
            device_id=DEVICE_ID,
            device_sequence=1,
            occurred_at="2026-09-11T08:00:00+02:00",
            payload={"sync_uuid": self.aggregate_id},
        )
        enqueue_sync_operation(self.connection, envelope)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    @patch("requests.get")
    def test_requests_transport_pulls_changes_from_central_api(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "after": 7,
            "next_checkpoint": 7,
            "changes": [],
        }
        get.return_value = response
        transport = RequestsSyncTransport(
            "https://central.example/", "test-token", timeout_seconds=9
        )

        page = transport.pull_changes(after=7, limit=25)

        self.assertEqual(page["next_checkpoint"], 7)
        get.assert_called_once_with(
            "https://central.example/v1/sync/changes",
            params={"after": 7, "limit": 25},
            headers={"Authorization": "Bearer test-token"},
            timeout=9.0,
        )

    @patch("requests.get")
    def test_requests_transport_pulls_complete_inventory_bootstrap(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "complete": True, "snapshot_sequence": 0,
            "inventory_updates": [],
        }
        get.return_value = response
        transport = RequestsSyncTransport(
            "https://central.example/", "test-token", timeout_seconds=9
        )
        snapshot = transport.pull_inventory_snapshot()
        self.assertIs(snapshot["complete"], True)
        get.assert_called_once_with(
            "https://central.example/v1/service/inventory-snapshot",
            headers={"Authorization": "Bearer test-token"},
            timeout=9.0,
        )

    @patch("requests.get")
    def test_requests_transport_pulls_complete_service_return_bootstrap(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "complete": True, "snapshot_sequence": 4,
            "returnable_parts": [],
        }
        get.return_value = response
        transport = RequestsSyncTransport(
            "https://central.example/", "test-token", timeout_seconds=9
        )
        snapshot = transport.pull_service_return_snapshot()
        self.assertIs(snapshot["complete"], True)
        get.assert_called_once_with(
            "https://central.example/v1/service/returnable-parts-snapshot",
            headers={"Authorization": "Bearer test-token"},
            timeout=9.0,
        )

    def test_lost_response_retries_the_same_operation_identity(self):
        transport = FlakyTransport()
        first = synchronize_once(self.connection, transport, DEVICE_ID)
        self.assertTrue(first.interrupted)
        self.assertEqual(outbox_operation(self.connection, self.operation_id)["status"], "UNCERTAIN")

        second = synchronize_once(self.connection, transport, DEVICE_ID)
        self.assertFalse(second.interrupted)
        self.assertEqual(second.sent, 1)
        self.assertEqual(outbox_operation(self.connection, self.operation_id)["status"], "ACKNOWLEDGED")
        self.assertEqual(
            [item["operation_id"] for item in transport.requests],
            [self.operation_id, self.operation_id],
        )

    def test_bad_change_checkpoint_rolls_back_inbox_page(self):
        result = synchronize_once(
            self.connection, MalformedChangeTransport(), DEVICE_ID
        )
        self.assertTrue(result.interrupted)
        self.assertEqual(result.received, 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_inbox_v32").fetchone()[0],
            0,
        )

    def test_stock_rejection_marks_service_and_preserves_problem_article(self):
        connection = sqlite3.connect(":memory:")
        try:
            ensure_workflow_schema(connection)
            ensure_sync_store_schema(connection)
            aggregate_id = str(uuid4())
            order_id, _ = create_service_order(
                connection,
                {
                    "sync_uuid": aggregate_id,
                    "customer_name": "Test",
                    "customer_phone": "040000000",
                    "device_model": "Naprava",
                    "device_serial": "CONFLICT-1",
                    "device_type": "DRUGO",
                    "identity_method": "MANUAL_OTHER",
                    "manual_identity_confirmed": True,
                    "customer_request": "Test",
                    "quote_eur": 0,
                    "logistics_eur": 0,
                    "contract_context": "MFD",
                    "delivery_channel": "PAPER",
                },
            )
            envelope = SyncOperationEnvelope(
                operation_id=str(uuid4()),
                operation_type="CompleteServiceTechnically",
                aggregate_type="SERVICE_ORDER",
                aggregate_id=aggregate_id,
                expected_version=1,
                actor_id="serviser",
                device_id=DEVICE_ID,
                device_sequence=1,
                occurred_at="2026-09-12T08:00:00+02:00",
                payload={
                    "parts": [{
                        "barcode": "BAR-1", "article_id": "7",
                        "description": "Baterija", "quantity": "1",
                    }]
                },
            )
            enqueue_sync_operation(connection, envelope)
            connection.commit()
            result = synchronize_once(connection, StockConflictTransport(), DEVICE_ID)
            self.assertFalse(result.interrupted)
            conflicts = list_open_sync_conflicts(connection)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["order_no"], get_service_order(
                connection, order_id
            )["order_no"])
            self.assertEqual(conflicts[0]["details"][0]["barcode"], "BAR-1")
            self.assertEqual(conflicts[0]["details"][0]["description"], "Baterija")
            self.assertEqual(
                get_service_order(connection, order_id)["sync_state"],
                "SYNC_CONFLICT",
            )
        finally:
            connection.close()

    def test_accepted_receipt_updates_only_central_version_and_sync_state(self):
        connection = sqlite3.connect(":memory:")
        try:
            ensure_workflow_schema(connection)
            ensure_sync_store_schema(connection)
            aggregate_id = str(uuid4())
            order_id, _ = create_service_order(
                connection,
                {
                    "sync_uuid": aggregate_id,
                    "customer_name": "Test",
                    "customer_phone": "040000000",
                    "device_model": "Naprava",
                    "device_serial": "ACCEPTED-1",
                    "device_type": "DRUGO",
                    "identity_method": "MANUAL_OTHER",
                    "manual_identity_confirmed": True,
                    "customer_request": "Test",
                    "quote_eur": 0,
                    "logistics_eur": 0,
                    "contract_context": "MFD",
                    "delivery_channel": "PAPER",
                },
            )
            connection.execute(
                "UPDATE service_orders SET row_version=8,sync_state='PENDING' WHERE id=?",
                (order_id,),
            )
            item = SyncOperationEnvelope(
                operation_id=str(uuid4()), operation_type="CreateServiceOrder",
                aggregate_type="SERVICE_ORDER", aggregate_id=aggregate_id,
                expected_version=0, actor_id="serviser", device_id=DEVICE_ID,
                device_sequence=1, occurred_at="2026-09-12T08:00:00+02:00",
                payload={"sync_uuid": aggregate_id},
            )
            enqueue_sync_operation(connection, item)
            connection.commit()
            result = synchronize_once(connection, AcceptedTransport(1), DEVICE_ID)
            self.assertFalse(result.interrupted)
            order = get_service_order(connection, order_id)
            self.assertEqual(order["row_version"], 8)
            self.assertEqual(order["central_version"], 1)
            self.assertEqual(order["sync_state"], "SYNCED")
        finally:
            connection.close()

    def test_rejected_removed_part_creates_a_visible_exact_lot_conflict(self):
        connection = sqlite3.connect(":memory:")
        try:
            ensure_sync_store_schema(connection)
            invoice_id = str(uuid4())
            invoice_line_id = str(uuid4())
            lot_id = str(uuid4())
            service_order_id = str(uuid4())
            connection.execute(
                "INSERT INTO central_service_part_returns_v32("
                "invoice_uuid,invoice_line_uuid,lot_uuid,service_order_uuid,"
                "invoice_number,article_uuid,barcode,description,lot_serial,"
                "allocated_quantity,returned_quantity,central_sequence,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    invoice_id, invoice_line_id, lot_id, service_order_id,
                    "R-2026-44", str(uuid4()), "BAR-44", "Baterija",
                    "LOT-44", "1.0000", "0.0000", 4,
                    "2026-09-12T08:00:00+02:00",
                ),
            )
            connection.execute(
                "INSERT INTO sync_service_return_state_v32("
                "singleton_id,snapshot_complete,snapshot_sequence,updated_at) "
                "VALUES(1,1,4,'2026-09-12T08:00:00+02:00')"
            )
            item = SyncOperationEnvelope(
                operation_id=str(uuid4()),
                operation_type="ReceiveRemovedServicePart",
                aggregate_type="STOCK_RETURN",
                aggregate_id=str(uuid4()),
                expected_version=0,
                actor_id="serviser",
                device_id=DEVICE_ID,
                device_sequence=1,
                occurred_at="2026-09-12T08:00:00+02:00",
                payload={
                    "return_id": str(uuid4()),
                    "invoice_id": invoice_id,
                    "invoice_line_id": invoice_line_id,
                    "lot_id": lot_id,
                    "quantity": "1.0000",
                    "condition_note": "Odstranjeno",
                    "confirmed": True,
                },
            )
            enqueue_sync_operation(connection, item)
            connection.commit()

            result = synchronize_once(
                connection, RemovedPartConflictTransport(), DEVICE_ID
            )

            self.assertFalse(result.interrupted)
            conflicts = list_open_sync_conflicts(connection)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(
                conflicts[0]["conflict_type"], "SERVICE_PART_RETURN_CONFLICT"
            )
            detail = conflicts[0]["details"][0]
            self.assertEqual(detail["invoice_id"], invoice_id)
            self.assertEqual(detail["invoice_line_id"], invoice_line_id)
            self.assertEqual(detail["lot_id"], lot_id)
            self.assertEqual(detail["service_order_id"], service_order_id)
            self.assertEqual(detail["invoice_number"], "R-2026-44")
            self.assertEqual(detail["description"], "Baterija")
            self.assertEqual(detail["barcode"], "BAR-44")
            self.assertEqual(detail["lot_serial"], "LOT-44")
            self.assertEqual(
                detail["error_code"],
                "PHYSICAL_RETURN_EXCEEDS_SOLD_QUANTITY",
            )
        finally:
            connection.close()

    def test_pull_applies_released_inventory_to_local_central_snapshot(self):
        article_id = str(uuid4())
        result = synchronize_once(
            self.connection, InventoryChangeTransport(article_id), DEVICE_ID
        )
        self.assertFalse(result.interrupted)
        self.assertEqual(result.received, 1)
        row = self.connection.execute(
            "SELECT quantity_available,current_sale_price,row_version "
            "FROM central_inventory_snapshot_v32 WHERE article_uuid=?",
            (article_id,),
        ).fetchone()
        self.assertEqual(row, ("2.0000", "15.00", 2))
        self.assertIsNotNone(self.connection.execute(
            "SELECT applied_at FROM sync_inbox_v32 WHERE central_sequence=1"
        ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
