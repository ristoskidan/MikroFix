import sqlite3
import unittest
from uuid import uuid4

from mikrofix_service_inventory import (
    service_inventory_by_barcode,
    service_inventory_identities,
)
from mikrofix_sync_contracts import SyncOperationEnvelope
from mikrofix_sync_store import (
    acknowledge_operation,
    apply_inventory_updates,
    enqueue_sync_operation,
    ensure_sync_store_schema,
)


class ServiceInventoryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_sync_store_schema(self.connection)
        self.article_id = str(uuid4())
        self.identity_id = str(uuid4())
        self.barcode = "SUP-BAT-1"
        self.apply_stock(quantity="5.0000", sequence=10, version=1)

    def tearDown(self):
        self.connection.close()

    def apply_stock(self, *, quantity, sequence, version):
        apply_inventory_updates(
            self.connection,
            central_sequence=sequence,
            payload={"inventory_updates": [{
                "article_id": self.article_id,
                "main_barcode": "BAT-MAIN",
                "description": "Baterija",
                "track_mode": "QUANTITY",
                "quantity_available": quantity,
                "current_sale_price": "29.90",
                "row_version": version,
                "blocked": False,
                "blocked_reason": "",
                "identities": [{
                    "identity_id": self.identity_id,
                    "supplier_barcode": self.barcode,
                    "quantity_available": quantity,
                }],
            }]},
        )

    def enqueue_completion(self, *, operation_id=None):
        operation_id = operation_id or str(uuid4())
        enqueue_sync_operation(self.connection, SyncOperationEnvelope(
            operation_id=operation_id,
            operation_type="CompleteServiceTechnically",
            aggregate_type="SERVICE_ORDER",
            aggregate_id=str(uuid4()),
            expected_version=1,
            actor_id="serviser",
            device_id="SERVICE-TEST-1",
            device_sequence=self.connection.execute(
                "SELECT COUNT(*)+1 FROM sync_outbox_v32"
            ).fetchone()[0],
            occurred_at="2026-09-12T10:00:00+02:00",
            payload={"parts": [{
                "article_id": "17",
                "barcode": self.barcode,
                "description": "Baterija",
                "quantity": "1.0000",
            }]},
        ))
        return operation_id

    def test_central_five_minus_one_offline_completion_is_four(self):
        self.enqueue_completion()
        item = service_inventory_by_barcode(self.connection, self.barcode)
        self.assertEqual(str(item.quantity_central), "5.0000")
        self.assertEqual(str(item.quantity_pending_local), "1.0000")
        self.assertEqual(str(item.quantity_available), "4.0000")

    def test_acknowledged_completion_stays_overlay_until_its_event_arrives(self):
        operation_id = self.enqueue_completion()
        acknowledge_operation(
            self.connection, operation_id, result_code="ACCEPTED",
            response={"operation_id": operation_id, "result_code": "ACCEPTED"},
            central_sequence=12,
        )
        self.assertEqual(
            str(service_inventory_by_barcode(
                self.connection, self.barcode
            ).quantity_available),
            "4.0000",
        )

        # Event 12 already contains the server-side deduction.  The overlay is
        # removed, so five-minus-one never becomes four-minus-one again.
        self.apply_stock(quantity="4.0000", sequence=12, version=2)
        item = service_inventory_by_barcode(self.connection, self.barcode)
        self.assertEqual(str(item.quantity_pending_local), "0.0000")
        self.assertEqual(str(item.quantity_available), "4.0000")

    def test_later_receipt_addition_keeps_unconfirmed_local_use_deducted(self):
        self.enqueue_completion()
        self.apply_stock(quantity="7.0000", sequence=11, version=2)
        item = service_inventory_by_barcode(self.connection, self.barcode)
        self.assertEqual(str(item.quantity_central), "7.0000")
        self.assertEqual(str(item.quantity_pending_local), "1.0000")
        self.assertEqual(str(item.quantity_available), "6.0000")

    def test_rejected_completion_never_creates_apparently_available_stock(self):
        operation_id = self.enqueue_completion()
        acknowledge_operation(
            self.connection, operation_id, result_code="REJECTED",
            response={"operation_id": operation_id, "result_code": "REJECTED"},
            central_sequence=None,
        )
        self.apply_stock(quantity="0.0000", sequence=13, version=2)
        item = service_inventory_by_barcode(self.connection, self.barcode)
        self.assertEqual(str(item.quantity_pending_local), "1.0000")
        self.assertEqual(str(item.quantity_available), "0.0000")

    def test_multiple_supplier_barcodes_are_not_mixed(self):
        second_identity = str(uuid4())
        apply_inventory_updates(
            self.connection, central_sequence=11,
            payload={"inventory_updates": [{
                "article_id": self.article_id, "main_barcode": "BAT-MAIN",
                "description": "Baterija", "track_mode": "QUANTITY",
                "quantity_available": "8.0000", "current_sale_price": "29.90",
                "row_version": 2, "blocked": False, "blocked_reason": "",
                "identities": [
                    {"identity_id": self.identity_id,
                     "supplier_barcode": self.barcode,
                     "quantity_available": "5.0000"},
                    {"identity_id": second_identity,
                     "supplier_barcode": "SUP-BAT-2",
                     "quantity_available": "3.0000"},
                ],
            }]},
        )
        self.enqueue_completion()
        rows = {item.barcode: item for item in service_inventory_identities(
            self.connection
        )}
        self.assertEqual(str(rows[self.barcode].quantity_available), "4.0000")
        self.assertEqual(str(rows["SUP-BAT-2"].quantity_available), "3.0000")


if __name__ == "__main__":
    unittest.main()
