import sqlite3
import tempfile
import unittest
from pathlib import Path

from mikrofix_v29_core import ensure_inventory_schema
from mikrofix_v30_inventory import (
    InventoryExceptionLine,
    V29Error,
    create_inventory_exception,
    storno_inventory_exception,
)


class V30InventoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "inventory.db"
        ensure_inventory_schema(self.db, {"categories": {}})
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            INSERT INTO inventory_items(
                barcode, article_name, stock_qty, price, created_at, updated_at
            ) VALUES('A-1','Artikel',5,10,'2026-08-30','2026-08-30')
            """
        )
        self.article_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_inventory_and_positive_manual_correction_are_blocked(self):
        with self.assertRaisesRegex(V29Error, "Neposredni popravek"):
            create_inventory_exception(
                inventory_db=self.db, document_type="INVENTURNI_ZAPISNIK",
                reason="Ni dovoljeno",
                lines=[InventoryExceptionLine(self.article_id, 1)],
            )
        with self.assertRaisesRegex(V29Error, "samo zmanjšata"):
            create_inventory_exception(
                inventory_db=self.db, document_type="ODPIS", reason="Test",
                lines=[InventoryExceptionLine(self.article_id, 1)],
            )

    def test_documented_writeoff_is_allowed(self):
        document_id, number = create_inventory_exception(
            inventory_db=self.db, document_type="ODPIS", reason="Poškodovan del",
            lines=[InventoryExceptionLine(self.article_id, -1)],
        )
        connection = sqlite3.connect(self.db)
        stock = connection.execute(
            "SELECT stock_qty FROM inventory_items WHERE id=?", (self.article_id,)
        ).fetchone()[0]
        lot_available = connection.execute(
            "SELECT quantity_available FROM inventory_lots_v30 WHERE article_id=?",
            (self.article_id,),
        ).fetchone()[0]
        allocation = connection.execute(
            "SELECT quantity FROM inventory_lot_allocations_v30 WHERE allocation_type='INVENTORY_EXCEPTION'"
        ).fetchone()[0]
        connection.close()
        self.assertTrue(number.startswith("ODP-"))
        self.assertEqual(stock, 4)
        self.assertEqual(lot_available, 4)
        self.assertEqual(allocation, 1)

        storno_inventory_exception(
            inventory_db=self.db,
            document_id=document_id,
            reason="Napačen odpis",
        )
        connection = sqlite3.connect(self.db)
        stock, lot_available, reversed_at = connection.execute(
            """
            SELECT i.stock_qty, l.quantity_available, a.reversed_at
            FROM inventory_items i
            JOIN inventory_lots_v30 l ON l.article_id=i.id
            JOIN inventory_lot_allocations_v30 a ON a.lot_id=l.id
            WHERE i.id=?
            """,
            (self.article_id,),
        ).fetchone()
        status = connection.execute(
            "SELECT status FROM inventory_exception_documents WHERE id=?",
            (document_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(stock, 5)
        self.assertEqual(lot_available, 5)
        self.assertTrue(reversed_at)
        self.assertEqual(status, "STORNIRANO")


if __name__ == "__main__":
    unittest.main()
