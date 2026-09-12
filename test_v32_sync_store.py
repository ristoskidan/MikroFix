import sqlite3
import unittest
from uuid import uuid4

from mikrofix_sync_contracts import SyncOperationEnvelope
from mikrofix_sync_store import (
    SyncStoreError,
    acknowledge_operation,
    apply_inventory_updates,
    apply_complete_inventory_snapshot,
    advance_central_checkpoint,
    central_checkpoint,
    enqueue_sync_operation,
    ensure_sync_store_schema,
    list_dispatchable_operations,
    mark_operation_sending,
    next_device_sequence,
    outbox_operation,
    recover_stale_sending,
    record_incoming_change,
)
from mikrofix_workflows import create_service_order, ensure_workflow_schema


DEVICE_ID = "SERVICE-PC-TEST"


def order_payload(sync_uuid):
    return {
        "sync_uuid": sync_uuid,
        "origin_device_id": DEVICE_ID,
        "customer_name": "TEST Stranka",
        "customer_phone": "040000000",
        "device_model": "TEST naprava",
        "device_serial": f"SERIAL-{str(sync_uuid)[:8]}",
        "device_type": "DRUGO",
        "identity_method": "MANUAL_OTHER",
        "manual_identity_confirmed": True,
        "customer_request": "Test",
        "quote_eur": "0",
        "logistics_eur": "0",
        "contract_context": "MFD",
        "delivery_channel": "PAPER",
        "microdetect_confirmed": False,
        "microdetect_at": "2026-09-11T08:00:00+02:00",
        "operator_code": "TEST",
    }


def envelope(
    *, operation_id=None, aggregate_id=None, sequence=1, payload=None,
    actor_id="serviser",
):
    aggregate_uuid = aggregate_id or str(uuid4())
    return SyncOperationEnvelope(
        operation_id=operation_id or str(uuid4()),
        operation_type="CreateServiceOrder",
        aggregate_type="SERVICE_ORDER",
        aggregate_id=aggregate_uuid,
        expected_version=0,
        actor_id=actor_id,
        device_id=DEVICE_ID,
        device_sequence=sequence,
        occurred_at="2026-09-11T08:00:00+02:00",
        payload=payload or {"service_order_id": aggregate_uuid},
    )


class SyncStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_sync_store_schema(self.connection)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def test_device_sequence_is_transactional(self):
        self.connection.execute("BEGIN")
        self.assertEqual(next_device_sequence(self.connection, DEVICE_ID), 1)
        self.connection.rollback()
        self.connection.execute("BEGIN")
        self.assertEqual(next_device_sequence(self.connection, DEVICE_ID), 1)
        self.assertEqual(next_device_sequence(self.connection, DEVICE_ID), 2)
        self.connection.commit()

    def test_same_operation_and_payload_is_idempotent(self):
        item = envelope()
        first = enqueue_sync_operation(self.connection, item)
        second = enqueue_sync_operation(self.connection, item)
        self.assertEqual(first["local_sequence"], second["local_sequence"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_outbox_v32").fetchone()[0],
            1,
        )

    def test_changed_payload_for_same_operation_is_rejected(self):
        operation_id = str(uuid4())
        aggregate_id = str(uuid4())
        enqueue_sync_operation(
            self.connection,
            envelope(
                operation_id=operation_id,
                aggregate_id=aggregate_id,
                payload={"value": 1},
            ),
        )
        with self.assertRaisesRegex(SyncStoreError, "different sync operation"):
            enqueue_sync_operation(
                self.connection,
                envelope(
                    operation_id=operation_id,
                    aggregate_id=aggregate_id,
                    payload={"value": 2},
                ),
            )

    def test_operation_identity_cannot_be_rebound_to_another_actor(self):
        item = envelope(actor_id="serviser-a")
        enqueue_sync_operation(self.connection, item)
        with self.assertRaisesRegex(SyncStoreError, "different sync operation"):
            enqueue_sync_operation(
                self.connection,
                envelope(
                    operation_id=item.operation_id,
                    aggregate_id=item.aggregate_id,
                    sequence=item.device_sequence,
                    payload=item.payload,
                    actor_id="serviser-b",
                ),
            )

    def test_device_sequence_cannot_be_reused(self):
        enqueue_sync_operation(self.connection, envelope(sequence=1))
        with self.assertRaisesRegex(SyncStoreError, "device sequence"):
            enqueue_sync_operation(self.connection, envelope(sequence=1))

    def test_uncertain_send_is_not_treated_as_a_new_operation(self):
        item = envelope()
        enqueue_sync_operation(self.connection, item)
        mark_operation_sending(self.connection, item.operation_id)
        self.connection.execute(
            "UPDATE sync_outbox_v32 SET last_attempt_at='2000-01-01T00:00:00+00:00' "
            "WHERE operation_id=?",
            (item.operation_id,),
        )
        self.assertEqual(recover_stale_sending(self.connection), 1)
        dispatchable = list_dispatchable_operations(self.connection, DEVICE_ID)
        self.assertEqual([row["operation_id"] for row in dispatchable], [item.operation_id])
        self.assertEqual(dispatchable[0]["status"], "UNCERTAIN")

    def test_server_receipt_is_stable_and_idempotent(self):
        item = envelope()
        enqueue_sync_operation(self.connection, item)
        mark_operation_sending(self.connection, item.operation_id)
        first = acknowledge_operation(
            self.connection,
            item.operation_id,
            result_code="ACCEPTED",
            response={"aggregate_version": 1},
            central_sequence=7,
        )
        second = acknowledge_operation(
            self.connection,
            item.operation_id,
            result_code="ACCEPTED",
            response={"aggregate_version": 1},
            central_sequence=7,
        )
        self.assertEqual(first["status"], "ACKNOWLEDGED")
        self.assertEqual(second["status"], "ACKNOWLEDGED")
        with self.assertRaisesRegex(SyncStoreError, "receipt changed"):
            acknowledge_operation(
                self.connection,
                item.operation_id,
                result_code="ACCEPTED",
                response={"aggregate_version": 2},
                central_sequence=7,
            )

    def test_checkpoint_never_moves_backwards(self):
        self.assertEqual(central_checkpoint(self.connection, DEVICE_ID), 0)
        self.assertEqual(advance_central_checkpoint(self.connection, DEVICE_ID, 12), 12)
        self.assertEqual(advance_central_checkpoint(self.connection, DEVICE_ID, 8), 12)

    def test_business_row_and_outbox_rollback_together(self):
        ensure_workflow_schema(self.connection)
        self.connection.commit()
        aggregate_id = str(uuid4())
        self.connection.execute("BEGIN")
        create_service_order(
            self.connection, order_payload(aggregate_id), commit=False
        )
        sequence = next_device_sequence(self.connection, DEVICE_ID)
        enqueue_sync_operation(
            self.connection,
            envelope(aggregate_id=aggregate_id, sequence=sequence),
        )
        self.connection.rollback()

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM service_orders").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM sync_outbox_v32").fetchone()[0],
            0,
        )
        self.assertIsNone(outbox_operation(self.connection, str(uuid4())))
        self.assertEqual(central_checkpoint(self.connection, DEVICE_ID), 0)

    def test_incoming_change_replay_is_stable(self):
        change = {
            "central_sequence": 4,
            "event_id": str(uuid4()),
            "event_type": "ServiceOrderCreated",
            "aggregate_type": "SERVICE_ORDER",
            "aggregate_uuid": str(uuid4()),
            "aggregate_version": 1,
            "payload": {"name": "Test"},
        }
        self.assertTrue(record_incoming_change(self.connection, **change))
        self.assertFalse(record_incoming_change(self.connection, **change))
        changed = dict(change)
        changed["payload"] = {"name": "Changed"}
        with self.assertRaisesRegex(SyncStoreError, "different data"):
            record_incoming_change(self.connection, **changed)

    def test_inventory_projection_is_versioned_and_never_creates_stock_events(self):
        article_id = str(uuid4())
        payload = {"inventory_updates": [{
            "article_id": article_id, "main_barcode": "MAIN-1",
            "description": "Baterija", "track_mode": "QUANTITY",
            "quantity_available": "3.0000", "current_sale_price": "29.00",
            "row_version": 4, "blocked": False, "blocked_reason": "",
            "identities": [{
                "identity_id": str(uuid4()), "supplier_barcode": "SUP-1",
                "quantity_available": "3.0000",
            }],
        }]}
        self.assertEqual(apply_inventory_updates(
            self.connection, central_sequence=9, payload=payload
        ), 1)
        row = self.connection.execute(
            "SELECT quantity_available,row_version,central_sequence "
            "FROM central_inventory_snapshot_v32 WHERE article_uuid=?",
            (article_id,),
        ).fetchone()
        self.assertEqual(row, ("3.0000", 4, 9))
        older = {"inventory_updates": [{
            **payload["inventory_updates"][0],
            "quantity_available": "99.0000", "row_version": 3,
        }]}
        apply_inventory_updates(self.connection, central_sequence=10, payload=older)
        self.assertEqual(self.connection.execute(
            "SELECT quantity_available,row_version FROM central_inventory_snapshot_v32"
        ).fetchone(), ("3.0000", 4))

        # A complete snapshot can start at sequence zero, and an older event
        # with the same row version may never rewind a newer snapshot.
        apply_complete_inventory_snapshot(
            self.connection, snapshot_sequence=10,
            inventory_updates=[{
                **payload["inventory_updates"][0],
                "quantity_available": "4.0000", "row_version": 5,
            }],
        )
        replay = {"inventory_updates": [{
            **payload["inventory_updates"][0],
            "quantity_available": "88.0000", "row_version": 5,
        }]}
        apply_inventory_updates(self.connection, central_sequence=9, payload=replay)
        self.assertEqual(self.connection.execute(
            "SELECT quantity_available,row_version,central_sequence "
            "FROM central_inventory_snapshot_v32"
        ).fetchone(), ("4.0000", 5, 10))

    def test_empty_complete_bootstrap_at_sequence_zero_is_valid(self):
        self.assertEqual(apply_complete_inventory_snapshot(
            self.connection, snapshot_sequence=0, inventory_updates=[]
        ), 0)
        self.assertEqual(self.connection.execute(
            "SELECT snapshot_complete,snapshot_sequence "
            "FROM sync_inventory_state_v32"
        ).fetchone(), (1, 0))



if __name__ == "__main__":
    unittest.main()
