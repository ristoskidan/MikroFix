import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mikrofix_v30_cash import (
    CashJournalError, cash_session_snapshot, close_cash_session,
    ensure_cash_schema, open_cash_session, record_cash_entry,
    require_open_cash_session,
)


class CashJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=Path(self.tmp.name)/"office.db"
        ensure_cash_schema(self.db)

    def tearDown(self): self.tmp.cleanup()

    def test_open_entries_totals_and_exact_close_difference(self):
        sid,no=open_cash_session(
            self.db,premise_id="MFD",device_id="PIS",operator_code="DR",opening_cash=50
        )
        c=sqlite3.connect(self.db)
        record_cash_entry(c,session_id=sid,entry_type="CASH_SALE",amount=100,
            reference_type="POS_INVOICE",reference_id=1,reference_no="MFD-PIS-1",
            description="Prodaja",operator_code="DR")
        record_cash_entry(c,session_id=sid,entry_type="CASH_EXPENSE",amount=-20,
            reference_type="CASH_VOUCHER",reference_id=2,reference_no="BI-1",
            description="Izdatek",operator_code="DR")
        c.commit();c.close()
        session,entries=cash_session_snapshot(self.db,premise_id="MFD",device_id="PIS")
        self.assertEqual(session["expected_cash"],130.0)
        self.assertEqual(session["cash_receipts"],100.0)
        self.assertEqual(session["cash_expenses"],20.0)
        self.assertEqual([x["entry_no"] for x in entries],[1,2])
        closed=close_cash_session(self.db,premise_id="MFD",device_id="PIS",
            operator_code="DR",counted_cash=129.50,note="Popis")
        self.assertEqual(closed["difference"],Decimal("-0.50"))
        c=sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE cash_sessions_v30 SET difference=0 WHERE id=?",(sid,))
        c.rollback();c.close()
        with self.assertRaises(CashJournalError):
            c=sqlite3.connect(self.db)
            try: require_open_cash_session(c,premise_id="MFD",device_id="PIS")
            finally:c.close()

    def test_second_open_session_is_blocked(self):
        open_cash_session(self.db,premise_id="MFD",device_id="PIS",operator_code="DR",opening_cash=0)
        with self.assertRaises(CashJournalError):
            open_cash_session(self.db,premise_id="MFD",device_id="PIS",operator_code="DR",opening_cash=0)


if __name__=="__main__": unittest.main()
