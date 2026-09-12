import sqlite3
import unittest
from uuid import uuid4

from mikrofix_service_inventory import service_inventory_by_barcode
from mikrofix_service_stock import consume_service_parts
from mikrofix_sync_store import apply_complete_inventory_snapshot, ensure_sync_store_schema


class ServiceStockConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("ATTACH DATABASE ':memory:' AS inventorydb")
        self.connection.executescript(
            """
            CREATE TABLE service_parts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                part_name TEXT, part_id TEXT, barcode TEXT, article_id INTEGER,
                lot_serial TEXT, unit_price REAL
            );
            CREATE TABLE inventorydb.inventory_items(
                id INTEGER PRIMARY KEY, barcode TEXT UNIQUE, article_name TEXT,
                internal_code TEXT, stock_qty REAL, price REAL,
                acquisition_cost REAL, reserved_qty REAL DEFAULT 0,
                active INTEGER DEFAULT 1, lot_serial TEXT, location TEXT,
                updated_at TEXT
            );
            CREATE TABLE inventorydb.stock_movements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, movement_no TEXT UNIQUE,
                created_at TEXT, article_id INTEGER, quantity_delta REAL,
                balance_after REAL, movement_type TEXT, source_type TEXT,
                source_id INTEGER, source_line_id INTEGER, lot_serial TEXT,
                location_from TEXT, location_to TEXT, reason TEXT,
                operator_code TEXT, reversal_of_id INTEGER
            );
            INSERT INTO inventorydb.inventory_items VALUES(
                1,'TEST-1','Testni del','DEL',3,25,10,0,1,'LOT-1','GLAVNA',''
            );
            """
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def part(self):
        return ("Testni del", "DEL", "TEST-1", 1, "LOT-1", 25)

    def test_technical_completion_consumes_each_part_once(self):
        line_ids = consume_service_parts(
            self.connection,
            inventory_schema="inventorydb",
            service_id=7,
            service_no="MF-2026-0007",
            parts=[self.part(), self.part()],
            operator_code="DR",
            consumed_at="2026-09-12 08:00:00",
        )
        self.connection.commit()

        self.assertEqual(len(line_ids), 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT stock_qty,reserved_qty FROM inventorydb.inventory_items WHERE id=1"
            ).fetchone(),
            (1.0, 0.0),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*),SUM(quantity_delta) "
                "FROM inventorydb.stock_movements WHERE movement_type='SERVICE_USE_OUT'"
            ).fetchone(),
            (2, -2.0),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*),SUM(quantity) "
                "FROM inventorydb.inventory_lot_allocations_v30 "
                "WHERE allocation_type='SERVICE_CONSUMPTION'"
            ).fetchone(),
            (2, 2.0),
        )

    def test_insufficient_stock_rolls_back_the_whole_caller_transaction(self):
        self.connection.execute(
            "UPDATE inventorydb.inventory_items SET stock_qty=1 WHERE id=1"
        )
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "Premalo zaloge"):
            consume_service_parts(
                self.connection,
                inventory_schema="inventorydb",
                service_id=8,
                service_no="MF-2026-0008",
                parts=[self.part(), self.part()],
                operator_code="DR",
                consumed_at="2026-09-12 09:00:00",
            )
        self.connection.rollback()

        self.assertEqual(
            self.connection.execute(
                "SELECT stock_qty FROM inventorydb.inventory_items WHERE id=1"
            ).fetchone()[0],
            1.0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM service_parts").fetchone()[0],
            0,
        )

    def test_central_snapshot_part_uses_overlay_not_legacy_stock_movement(self):
        ensure_sync_store_schema(self.connection)
        article_id = str(uuid4())
        identity_id = str(uuid4())
        apply_complete_inventory_snapshot(
            self.connection, snapshot_sequence=4,
            inventory_updates=[{
                "article_id": article_id, "main_barcode": "MAIN-CENTRAL",
                "description": "Centralni del", "track_mode": "QUANTITY",
                "quantity_available": "1.0000", "current_sale_price": "20.00",
                "row_version": 1, "blocked": False, "blocked_reason": "",
                "identities": [{
                    "identity_id": identity_id,
                    "supplier_barcode": "SUP-CENTRAL-1",
                    "quantity_available": "1.0000",
                }],
            }],
        )
        projected = service_inventory_by_barcode(
            self.connection, "SUP-CENTRAL-1"
        )
        line_ids = consume_service_parts(
            self.connection, inventory_schema="inventorydb", service_id=9,
            service_no="MF-2026-0009",
            parts=[(
                "Centralni del", article_id, "SUP-CENTRAL-1",
                projected.local_article_id, "", 20,
            )],
            operator_code="DR", consumed_at="2026-09-12 10:00:00",
        )
        self.assertEqual(len(line_ids), 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM inventorydb.stock_movements"
        ).fetchone()[0], 0)

    def test_central_snapshot_never_allows_two_uses_of_last_unit(self):
        ensure_sync_store_schema(self.connection)
        article_id = str(uuid4())
        identity_id = str(uuid4())
        apply_complete_inventory_snapshot(
            self.connection, snapshot_sequence=4,
            inventory_updates=[{
                "article_id": article_id, "main_barcode": "MAIN-LAST",
                "description": "Zadnji del", "track_mode": "QUANTITY",
                "quantity_available": "1.0000", "current_sale_price": "20.00",
                "row_version": 1, "blocked": False, "blocked_reason": "",
                "identities": [{
                    "identity_id": identity_id,
                    "supplier_barcode": "SUP-LAST-1",
                    "quantity_available": "1.0000",
                }],
            }],
        )
        projected = service_inventory_by_barcode(self.connection, "SUP-LAST-1")
        part = (
            "Zadnji del", article_id, "SUP-LAST-1",
            projected.local_article_id, "", 20,
        )
        with self.assertRaisesRegex(RuntimeError, "Premalo centralnega posnetka"):
            consume_service_parts(
                self.connection, inventory_schema="inventorydb", service_id=10,
                service_no="MF-2026-0010", parts=[part, part],
                operator_code="DR", consumed_at="2026-09-12 10:30:00",
            )


if __name__ == "__main__":
    unittest.main()
