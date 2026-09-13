import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from mikrofix_v30_cash import (
    CashJournalError, book_cash_disbursement, cash_session_snapshot,
    close_cash_session, ensure_cash_schema, open_cash_session,
    generate_cash_journal_pdf, generate_central_cash_journal_pdf,
    list_cash_sessions,
    pending_cash_disbursements, record_cash_entry,
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

    def test_cash_journal_pdf_preserves_slovenian_characters(self):
        sid, _ = open_cash_session(
            self.db, premise_id="MFD", device_id="PIS",
            operator_code="DR", opening_cash=10,
        )
        close_cash_session(
            self.db, premise_id="MFD", device_id="PIS",
            operator_code="DR", counted_cash=10, note="Šumniki: čšž ČŠŽ",
        )
        path = generate_cash_journal_pdf(
            self.db, session_id=sid, output_dir=Path(self.tmp.name),
            company={"legal_name": "MikroFIX", "address": "Paradiž 73"},
        )
        from pypdf import PdfReader
        pages = PdfReader(str(path)).pages
        text = "\n".join(page.extract_text() or "" for page in pages)
        self.assertGreaterEqual(len(pages), 2)
        self.assertIn("ZAKLJUČNI POVZETEK", pages[0].extract_text() or "")
        self.assertIn("ARHIVSKA PRILOGA", text)
        self.assertIn("Začetno stanje", text)
        self.assertIn("Pričakovano stanje", text)
        self.assertIn("Šumniki: čšž ČŠŽ", text)
        sessions = list_cash_sessions(self.db, premise_id="MFD", device_id="PIS")
        self.assertEqual(sessions[0]["id"], sid)
        self.assertEqual(sessions[0]["status"], "CLOSED")

    def test_central_cash_journal_pdf_validates_and_renders_snapshot(self):
        session_id = str(uuid4())
        snapshot = {
            "session_id": session_id, "session_no": "BD-2026-0042",
            "premise_id": "MFD", "electronic_device_id": "PIS",
            "status": "CLOSED", "opened_at": "2026-09-13T08:00:00+02:00",
            "opened_by": "daniel", "opening_cash": "50.00",
            "expected_cash": "65.00", "closed_at": "2026-09-13T18:00:00+02:00",
            "closed_by": "daniel", "counted_cash": "64.50",
            "difference": "-0.50", "close_note": "Šumniki čšž",
            "entries": [{
                "entry_id": str(uuid4()), "entry_no": 1,
                "occurred_at": "2026-09-13T09:00:00+02:00",
                "entry_type": "CASH_SALE", "amount": "15.00",
                "balance_after": "65.00", "reference_type": "INVOICE",
                "reference_no": "MFD-PIS-1", "description": "Gotovinski račun",
                "actor_id": "daniel",
            }],
        }
        path = generate_central_cash_journal_pdf(
            snapshot, output_dir=self.tmp.name,
            company={"legal_name": "MikroFix", "address": "Paradiž 73"},
        )
        self.assertTrue(path.is_file())
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        self.assertIn("BLAGAJNIŠKI DNEVNIK BD-2026-0042", text)
        self.assertIn("Šumniki čšž", text)
        broken = {**snapshot, "expected_cash": "64.99"}
        with self.assertRaisesRegex(CashJournalError, "pričakovano stanje"):
            generate_central_cash_journal_pdf(
                broken, output_dir=self.tmp.name,
                company={"legal_name": "MikroFix"},
            )
    def test_confirmed_buyback_disbursement_is_booked_exactly_once(self):
        service_db = Path(self.tmp.name) / "service.db"
        c = sqlite3.connect(service_db)
        c.executescript(
            """
            CREATE TABLE cash_disbursements(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_no TEXT NOT NULL UNIQUE,
                buyback_id INTEGER NOT NULL UNIQUE,
                buyback_no TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                status TEXT NOT NULL,
                recipient_name TEXT NOT NULL,
                recipient_address TEXT NOT NULL,
                purpose TEXT NOT NULL,
                amount REAL NOT NULL,
                operator_code TEXT NOT NULL
            );
            INSERT INTO cash_disbursements(
                voucher_no,buyback_id,buyback_no,created_at,confirmed_at,status,
                recipient_name,recipient_address,purpose,amount,operator_code
            ) VALUES(
                'BI-2026-0001',1,'MF-ODK-2026-0001','2026-09-03 10:00:00',
                '2026-09-03 10:05:00','CONFIRMED','Prodajalec','Naslov',
                'Gotovinski odkup',75,'DR'
            );
            """
        )
        c.commit(); c.close()
        session_id, _ = open_cash_session(
            self.db,premise_id="MFD",device_id="PIS",operator_code="DR",opening_cash=100
        )
        self.assertEqual(
            [
                row["voucher_no"] for row in pending_cash_disbursements(
                    office_db=self.db, service_db=service_db
                )
            ],
            ["BI-2026-0001"],
        )
        first = book_cash_disbursement(
            office_db=self.db,service_db=service_db,voucher_id=1,
            premise_id="MFD",device_id="PIS",operator_code="DR",
        )
        second = book_cash_disbursement(
            office_db=self.db,service_db=service_db,voucher_id=1,
            premise_id="MFD",device_id="PIS",operator_code="DR",
        )
        self.assertFalse(first["already_booked"])
        self.assertTrue(second["already_booked"])
        self.assertEqual(first["cash_entry_id"], second["cash_entry_id"])
        self.assertEqual(first["cash_session_id"], session_id)
        self.assertEqual(
            pending_cash_disbursements(office_db=self.db, service_db=service_db),
            [],
        )
        c = sqlite3.connect(self.db)
        rows = c.execute(
            "SELECT amount,reference_no FROM cash_journal_entries_v30"
        ).fetchall()
        c.close()
        self.assertEqual(rows, [(-75.0, "BI-2026-0001")])
        c = sqlite3.connect(service_db)
        status = c.execute(
            "SELECT status,cash_session_id,cash_entry_id FROM cash_disbursements WHERE id=1"
        ).fetchone()
        c.close()
        self.assertEqual(status, ("BOOKED", session_id, first["cash_entry_id"]))

    def test_legacy_atomic_buyback_entry_is_linked_without_second_debit(self):
        service_db = Path(self.tmp.name) / "legacy-service.db"
        c = sqlite3.connect(service_db)
        c.executescript(
            """
            CREATE TABLE cash_disbursements(
                id INTEGER PRIMARY KEY, voucher_no TEXT, buyback_id INTEGER UNIQUE,
                buyback_no TEXT, created_at TEXT, confirmed_at TEXT, status TEXT,
                recipient_name TEXT, recipient_address TEXT, purpose TEXT,
                amount REAL, operator_code TEXT
            );
            INSERT INTO cash_disbursements VALUES(
                1,'BI-LEGACY',41,'ODK-41','2026-09-01','2026-09-01','CONFIRMED',
                'Prodajalec','Naslov','Gotovinski odkup',75,'DR'
            );
            """
        )
        c.commit(); c.close()
        session_id, _ = open_cash_session(
            self.db, premise_id="MFD", device_id="PIS",
            operator_code="DR", opening_cash=100,
        )
        c = sqlite3.connect(self.db)
        c.execute("BEGIN IMMEDIATE")
        entry_id = record_cash_entry(
            c, session_id=session_id, entry_type="CASH_BUYBACK", amount=-75,
            reference_type="BUYBACK", reference_id=41,
            reference_no="BI-LEGACY", description="Stari atomski odkup",
            operator_code="DR",
        )
        c.commit(); c.close()
        self.assertEqual(
            pending_cash_disbursements(office_db=self.db, service_db=service_db),
            [],
        )
        c = sqlite3.connect(self.db)
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM cash_journal_entries_v30").fetchone()[0],
            1,
        )
        c.close()
        c = sqlite3.connect(service_db)
        self.assertEqual(
            c.execute(
                "SELECT status,cash_session_id,cash_entry_id FROM cash_disbursements WHERE id=1"
            ).fetchone(),
            ("BOOKED", session_id, entry_id),
        )
        c.close()

    def test_legacy_buyback_with_wrong_amount_is_not_silently_linked(self):
        service_db = Path(self.tmp.name) / "legacy-mismatch.db"
        c = sqlite3.connect(service_db)
        c.executescript(
            """
            CREATE TABLE cash_disbursements(
                id INTEGER PRIMARY KEY, voucher_no TEXT, buyback_id INTEGER UNIQUE,
                buyback_no TEXT, created_at TEXT, confirmed_at TEXT, status TEXT,
                recipient_name TEXT, recipient_address TEXT, purpose TEXT,
                amount REAL, operator_code TEXT
            );
            INSERT INTO cash_disbursements VALUES(
                1,'BI-MISMATCH',51,'ODK-51','2026-09-01','2026-09-01','CONFIRMED',
                'Prodajalec','Naslov','Gotovinski odkup',75,'DR'
            );
            """
        )
        c.commit(); c.close()
        session_id, _ = open_cash_session(
            self.db, premise_id="MFD", device_id="PIS",
            operator_code="DR", opening_cash=100,
        )
        c = sqlite3.connect(self.db)
        c.execute("BEGIN IMMEDIATE")
        record_cash_entry(
            c, session_id=session_id, entry_type="CASH_BUYBACK", amount=-70,
            reference_type="BUYBACK", reference_id=51,
            reference_no="BI-MISMATCH", description="Napačen stari znesek",
            operator_code="DR",
        )
        c.commit(); c.close()
        self.assertEqual(
            len(pending_cash_disbursements(office_db=self.db, service_db=service_db)),
            1,
        )
        with self.assertRaisesRegex(CashJournalError, "drugačen znesek"):
            book_cash_disbursement(
                office_db=self.db, service_db=service_db, voucher_id=1,
                premise_id="MFD", device_id="PIS", operator_code="DR",
            )
        c = sqlite3.connect(self.db)
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM cash_journal_entries_v30").fetchone()[0],
            1,
        )
        c.close()

    def test_booked_voucher_with_broken_cash_link_fails_closed(self):
        service_db = Path(self.tmp.name) / "broken-booked-link.db"
        c = sqlite3.connect(service_db)
        c.executescript(
            """
            CREATE TABLE cash_disbursements(
                id INTEGER PRIMARY KEY, voucher_no TEXT, buyback_id INTEGER UNIQUE,
                buyback_no TEXT, created_at TEXT, confirmed_at TEXT, status TEXT,
                recipient_name TEXT, recipient_address TEXT, purpose TEXT,
                amount REAL, operator_code TEXT, cash_session_id INTEGER,
                cash_entry_id INTEGER, booked_at TEXT
            );
            INSERT INTO cash_disbursements VALUES(
                1,'BI-BROKEN',61,'ODK-61','2026-09-01','2026-09-01','BOOKED',
                'Prodajalec','Naslov','Gotovinski odkup',75,'DR',123,999,'2026-09-01'
            );
            """
        )
        c.commit(); c.close()
        with self.assertRaisesRegex(CashJournalError, "ni veljavno povezan"):
            book_cash_disbursement(
                office_db=self.db, service_db=service_db, voucher_id=1,
                premise_id="MFD", device_id="PIS", operator_code="DR",
            )

    def test_unconfirmed_or_unfunded_disbursement_is_not_booked(self):
        service_db = Path(self.tmp.name) / "service-errors.db"
        c = sqlite3.connect(service_db)
        c.executescript(
            """
            CREATE TABLE cash_disbursements(
                id INTEGER PRIMARY KEY, voucher_no TEXT, buyback_id INTEGER,
                buyback_no TEXT, created_at TEXT, confirmed_at TEXT, status TEXT,
                recipient_name TEXT, recipient_address TEXT, purpose TEXT,
                amount REAL, operator_code TEXT
            );
            INSERT INTO cash_disbursements VALUES(
                1,'BI-PREP',1,'ODK-1','2026-09-03',NULL,'PREPARED',
                'A','N','Pripravljen',10,'DR'
            );
            INSERT INTO cash_disbursements VALUES(
                2,'BI-HIGH',2,'ODK-2','2026-09-03','2026-09-03','CONFIRMED',
                'B','N','Previsok',150,'DR'
            );
            """
        )
        c.commit(); c.close()
        open_cash_session(
            self.db,premise_id="MFD",device_id="PIS",operator_code="DR",opening_cash=100
        )
        with self.assertRaisesRegex(CashJournalError, "podpisu in potrditvi"):
            book_cash_disbursement(
                office_db=self.db,service_db=service_db,voucher_id=1,
                premise_id="MFD",device_id="PIS",operator_code="DR",
            )
        with self.assertRaisesRegex(CashJournalError, "negativno stanje"):
            book_cash_disbursement(
                office_db=self.db,service_db=service_db,voucher_id=2,
                premise_id="MFD",device_id="PIS",operator_code="DR",
            )
        c = sqlite3.connect(self.db)
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM cash_journal_entries_v30").fetchone()[0], 0
        )
        c.close()
        c = sqlite3.connect(service_db)
        self.assertEqual(
            c.execute("SELECT status,cash_entry_id FROM cash_disbursements WHERE id=2").fetchone(),
            ("CONFIRMED", None),
        )
        c.close()


if __name__=="__main__": unittest.main()
