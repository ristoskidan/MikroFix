import sqlite3
import unittest

from mikrofix_operations import (
    OperationError,
    complete_operation,
    ensure_operation_schema,
    list_recovery_operations,
    mark_stale_operations,
    prepare_operation,
    start_operation,
    verify_outbox_chain,
)


class OperationJournalTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_operation_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_same_id_and_same_payload_is_idempotent(self):
        first = prepare_operation(
            self.connection,
            operation_type="BUYBACK_COMPLETE",
            aggregate_type="buyback",
            aggregate_id=17,
            payload={"buyback_id": 17, "amount": 90.0},
            operator_code="DR",
            operation_id="OP-17",
        )
        second = prepare_operation(
            self.connection,
            operation_type="BUYBACK_COMPLETE",
            aggregate_type="buyback",
            aggregate_id=17,
            payload={"amount": 90.0, "buyback_id": 17},
            operator_code="DR",
            operation_id="OP-17",
        )
        self.assertEqual(first["id"], second["id"])

    def test_same_id_with_changed_payload_is_rejected(self):
        prepare_operation(
            self.connection,
            operation_type="BUYBACK_COMPLETE",
            aggregate_type="buyback",
            payload={"amount": 90.0},
            operator_code="DR",
            operation_id="OP-17",
        )
        with self.assertRaises(OperationError):
            prepare_operation(
                self.connection,
                operation_type="BUYBACK_COMPLETE",
                aggregate_type="buyback",
                payload={"amount": 91.0},
                operator_code="DR",
                operation_id="OP-17",
            )

    def test_completed_operation_has_one_verifiable_event(self):
        prepare_operation(
            self.connection,
            operation_type="BUYBACK_COMPLETE",
            aggregate_type="buyback",
            payload={"buyback_id": 17},
            operator_code="DR",
            operation_id="OP-17",
        )
        start_operation(self.connection, "OP-17")
        complete_operation(
            self.connection,
            "OP-17",
            result={"inventory_code": "NAP-2026-0001"},
        )
        complete_operation(
            self.connection,
            "OP-17",
            result={"inventory_code": "NAP-2026-0001"},
        )
        count = self.connection.execute(
            "SELECT COUNT(*) FROM operation_outbox_v31"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(verify_outbox_chain(self.connection), [])

    def test_interrupted_operation_enters_recovery_list(self):
        prepare_operation(
            self.connection,
            operation_type="SERVICE_TO_BUYBACK",
            aggregate_type="service_order",
            payload={"order_id": 4},
            operator_code="DR",
            operation_id="OP-4",
        )
        start_operation(self.connection, "OP-4")
        self.connection.execute(
            "UPDATE operation_journal_v31 SET updated_at='2000-01-01T00:00:00+00:00'"
        )
        self.assertEqual(mark_stale_operations(self.connection), 1)
        pending = list_recovery_operations(self.connection)
        self.assertEqual(pending[0]["status"], "NEEDS_RECOVERY")


if __name__ == "__main__":
    unittest.main()
