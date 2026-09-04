"""Blagajniške seje in nespremenljiv dnevnik gotovine MikroFIX v30."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


CENT = Decimal("0.01")


class CashJournalError(RuntimeError):
    pass


def money(value) -> Decimal:
    try:
        return Decimal(str(value or 0).replace(",", ".")).quantize(CENT, ROUND_HALF_UP)
    except Exception as exc:
        raise CashJournalError("Neveljaven gotovinski znesek.") from exc


def ensure_cash_schema_connection(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS cash_sessions_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_no TEXT NOT NULL UNIQUE,
            premise_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            opened_at TEXT NOT NULL,
            opened_by TEXT NOT NULL,
            opening_cash REAL NOT NULL,
            closed_at TEXT,
            closed_by TEXT,
            expected_cash REAL,
            counted_cash REAL,
            difference REAL,
            close_note TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_cash_session_v30
            ON cash_sessions_v30(premise_id,device_id)
            WHERE status='OPEN';
        CREATE TABLE IF NOT EXISTS cash_journal_entries_v30(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            entry_no INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            amount REAL NOT NULL,
            balance_after REAL NOT NULL,
            reference_type TEXT NOT NULL,
            reference_id INTEGER,
            reference_no TEXT,
            description TEXT NOT NULL,
            operator_code TEXT NOT NULL,
            reversal_of_id INTEGER,
            UNIQUE(session_id,entry_no),
            UNIQUE(reference_type,reference_id,entry_type),
            FOREIGN KEY(session_id) REFERENCES cash_sessions_v30(id)
        );
        CREATE INDEX IF NOT EXISTS idx_cash_journal_session_v30
            ON cash_journal_entries_v30(session_id,id);
        CREATE TRIGGER IF NOT EXISTS trg_cash_entry_no_update_v30
        BEFORE UPDATE ON cash_journal_entries_v30
        BEGIN SELECT RAISE(ABORT,'Blagajniškega dogodka ni dovoljeno spreminjati.'); END;
        CREATE TRIGGER IF NOT EXISTS trg_cash_entry_no_delete_v30
        BEFORE DELETE ON cash_journal_entries_v30
        BEGIN SELECT RAISE(ABORT,'Blagajniškega dogodka ni dovoljeno brisati.'); END;
        CREATE TRIGGER IF NOT EXISTS trg_cash_session_no_delete_v30
        BEFORE DELETE ON cash_sessions_v30
        BEGIN SELECT RAISE(ABORT,'Blagajniške seje ni dovoljeno brisati.'); END;
        CREATE TRIGGER IF NOT EXISTS trg_closed_cash_session_no_update_v30
        BEFORE UPDATE ON cash_sessions_v30 WHEN OLD.status='CLOSED'
        BEGIN SELECT RAISE(ABORT,'Zaključene blagajniške seje ni dovoljeno spreminjati.'); END;
        """
    )


def ensure_cash_schema(office_db: Path | str) -> None:
    c = sqlite3.connect(str(office_db))
    try:
        ensure_cash_schema_connection(c)
        c.commit()
    finally:
        c.close()


def _next_session_no(c: sqlite3.Connection) -> str:
    year = datetime.now().year
    prefix = f"BD-{year}-"
    values = []
    for (raw,) in c.execute(
        "SELECT session_no FROM cash_sessions_v30 WHERE session_no LIKE ?",(prefix+"%",)
    ):
        try:
            values.append(int(str(raw).rsplit("-",1)[-1]))
        except ValueError:
            pass
    return f"{prefix}{max(values,default=0)+1:04d}"


def _open_row(c: sqlite3.Connection, premise_id: str, device_id: str):
    return c.execute(
        "SELECT * FROM cash_sessions_v30 WHERE premise_id=? AND device_id=? "
        "AND status='OPEN' ORDER BY id DESC LIMIT 1",
        (str(premise_id),str(device_id)),
    ).fetchone()


def open_cash_session(
    office_db: Path | str, *, premise_id: str, device_id: str,
    operator_code: str, opening_cash,
) -> tuple[int,str]:
    opening = money(opening_cash)
    if opening < 0:
        raise CashJournalError("Začetno stanje ne sme biti negativno.")
    c = sqlite3.connect(str(office_db))
    try:
        ensure_cash_schema_connection(c)
        c.execute("BEGIN IMMEDIATE")
        if _open_row(c,premise_id,device_id):
            raise CashJournalError("Blagajna za ta prostor in napravo je že odprta.")
        number = _next_session_no(c)
        now = datetime.now().isoformat(timespec="seconds")
        cur = c.execute(
            "INSERT INTO cash_sessions_v30(session_no,premise_id,device_id,status,"
            "opened_at,opened_by,opening_cash) VALUES(?,?,?,'OPEN',?,?,?)",
            (number,str(premise_id),str(device_id),now,str(operator_code),float(opening)),
        )
        c.commit()
        return int(cur.lastrowid),number
    except Exception:
        c.rollback(); raise
    finally:
        c.close()


def require_open_cash_session(
    c: sqlite3.Connection, *, premise_id: str, device_id: str,
) -> int:
    ensure_cash_schema_connection(c)
    row = _open_row(c,premise_id,device_id)
    if not row:
        raise CashJournalError(
            "Gotovinsko plačilo ni dovoljeno, dokler blagajna za izbrani prostor in napravo ni odprta."
        )
    return int(row[0])


def record_cash_entry(
    c: sqlite3.Connection, *, session_id: int, entry_type: str, amount,
    reference_type: str, reference_id: int | None, reference_no: str,
    description: str, operator_code: str, reversal_of_id: int | None = None,
) -> int:
    ensure_cash_schema_connection(c)
    session = c.execute(
        "SELECT status,opening_cash FROM cash_sessions_v30 WHERE id=?",(int(session_id),)
    ).fetchone()
    if not session or session[0] != "OPEN":
        raise CashJournalError("Blagajniška seja ni odprta.")
    value = money(amount)
    if value == 0:
        raise CashJournalError("Blagajniški dogodek ne sme biti 0,00 EUR.")
    last = c.execute(
        "SELECT COALESCE(MAX(entry_no),0),COALESCE(SUM(amount),0) "
        "FROM cash_journal_entries_v30 WHERE session_id=?",(int(session_id),)
    ).fetchone()
    entry_no = int(last[0])+1
    balance = (money(session[1])+money(last[1])+value).quantize(CENT)
    if balance < 0:
        raise CashJournalError("Blagajniški izdatek bi ustvaril negativno stanje gotovine.")
    cur = c.execute(
        """
        INSERT INTO cash_journal_entries_v30(
            session_id,entry_no,occurred_at,entry_type,amount,balance_after,
            reference_type,reference_id,reference_no,description,operator_code,reversal_of_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (int(session_id),entry_no,datetime.now().isoformat(timespec="seconds"),
         str(entry_type),float(value),float(balance),str(reference_type),reference_id,
         str(reference_no or ""),str(description),str(operator_code),reversal_of_id),
    )
    return int(cur.lastrowid)


def _ensure_disbursement_link_columns(
    c: sqlite3.Connection, schema: str = "svc"
) -> None:
    columns = {
        str(row[1])
        for row in c.execute(f"PRAGMA {schema}.table_info(cash_disbursements)")
    }
    if "cash_session_id" not in columns:
        c.execute(
            f"ALTER TABLE {schema}.cash_disbursements ADD COLUMN cash_session_id INTEGER"
        )
    if "cash_entry_id" not in columns:
        c.execute(
            f"ALTER TABLE {schema}.cash_disbursements ADD COLUMN cash_entry_id INTEGER"
        )
    if "booked_at" not in columns:
        c.execute(
            f"ALTER TABLE {schema}.cash_disbursements ADD COLUMN booked_at TEXT"
        )


def pending_cash_disbursements(
    *, office_db: Path | str, service_db: Path | str
) -> list[dict]:
    """Vrne še neknjižene izdatke in varno poveže stare že knjižene odkupe."""
    c = sqlite3.connect(str(office_db), timeout=20)
    c.row_factory = sqlite3.Row
    try:
        ensure_cash_schema_connection(c)
        c.execute("ATTACH DATABASE ? AS svc", (str(service_db),))
        _ensure_disbursement_link_columns(c, "svc")
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        candidates = c.execute(
            """
            SELECT * FROM svc.cash_disbursements
            WHERE status='CONFIRMED' AND cash_entry_id IS NULL
            ORDER BY confirmed_at,id
            """
        ).fetchall()
        pending: list[dict] = []
        now = datetime.now().isoformat(timespec="seconds")
        for voucher in candidates:
            legacy = c.execute(
                """
                SELECT id,session_id,amount,reference_no
                FROM cash_journal_entries_v30
                WHERE reference_type='BUYBACK' AND reference_id=?
                  AND entry_type='CASH_BUYBACK'
                ORDER BY id LIMIT 1
                """,
                (int(voucher["buyback_id"]),),
            ).fetchone()
            expected_references = {
                str(voucher["voucher_no"] or "").strip(),
                str(voucher["buyback_no"] or "").strip(),
            }
            if (
                legacy
                and money(legacy["amount"]) == -money(voucher["amount"])
                and str(legacy["reference_no"] or "").strip() in expected_references
            ):
                c.execute(
                    """
                    UPDATE svc.cash_disbursements
                    SET status='BOOKED',cash_session_id=?,cash_entry_id=?,booked_at=?
                    WHERE id=? AND status='CONFIRMED' AND cash_entry_id IS NULL
                    """,
                    (
                        int(legacy["session_id"]), int(legacy["id"]), now,
                        int(voucher["id"]),
                    ),
                )
            else:
                pending.append(dict(voucher))
        c.commit()
        return pending
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def book_cash_disbursement(
    *,
    office_db: Path | str,
    service_db: Path | str,
    voucher_id: int,
    premise_id: str,
    device_id: str,
    operator_code: str,
) -> dict:
    """Atomsko knjiži potrjeni odkupni izdatek v odprto blagajno natanko enkrat."""
    c = sqlite3.connect(str(office_db), timeout=20)
    c.row_factory = sqlite3.Row
    try:
        ensure_cash_schema_connection(c)
        c.execute("ATTACH DATABASE ? AS svc", (str(service_db),))
        _ensure_disbursement_link_columns(c, "svc")
        c.commit()
        c.execute("BEGIN IMMEDIATE")
        voucher = c.execute(
            "SELECT * FROM svc.cash_disbursements WHERE id=?",
            (int(voucher_id),),
        ).fetchone()
        if not voucher:
            raise CashJournalError("Blagajniški izdatek ni najden.")
        if str(voucher["status"]) == "BOOKED":
            if not voucher["cash_entry_id"] or not voucher["cash_session_id"]:
                raise CashJournalError(
                    "Knjiženi izdatek nima popolne povezave z blagajniškim dnevnikom."
                )
            entry = c.execute(
                """
                SELECT session_id,entry_no,entry_type,amount,reference_type,
                       reference_id,reference_no
                FROM cash_journal_entries_v30 WHERE id=?
                """,
                (int(voucher["cash_entry_id"]),),
            ).fetchone()
            expected_reference_numbers = {
                str(voucher["voucher_no"] or "").strip(),
                str(voucher["buyback_no"] or "").strip(),
            }
            valid_legacy = bool(
                entry
                and str(entry[2]) == "CASH_BUYBACK"
                and str(entry[4]) == "BUYBACK"
                and int(entry[5] or 0) == int(voucher["buyback_id"])
            )
            valid_import = bool(
                entry
                and str(entry[2]) == "CASH_EXPENSE"
                and str(entry[4]) == "BUYBACK_CASH_DISBURSEMENT"
                and int(entry[5] or 0) == int(voucher["id"])
            )
            if (
                not entry
                or int(entry[0]) != int(voucher["cash_session_id"])
                or money(entry[3]) != -money(voucher["amount"])
                or str(entry[6] or "").strip() not in expected_reference_numbers
                or not (valid_legacy or valid_import)
            ):
                raise CashJournalError(
                    "Knjiženi izdatek ni veljavno povezan s pripadajočim gotovinskim dogodkom."
                )
            c.rollback()
            return {
                "voucher_id": int(voucher["id"]),
                "voucher_no": str(voucher["voucher_no"]),
                "cash_entry_id": int(voucher["cash_entry_id"]),
                "cash_session_id": int(entry[0]),
                "entry_no": int(entry[1]),
                "already_booked": True,
            }
        if str(voucher["status"]) != "CONFIRMED":
            raise CashJournalError(
                "Izdatek je mogoče knjižiti šele po podpisu in potrditvi odkupa."
            )
        amount = money(voucher["amount"])
        if amount <= 0:
            raise CashJournalError("Blagajniški izdatek nima veljavnega zneska.")
        legacy = c.execute(
            """
            SELECT id,session_id,entry_no,amount,reference_no
            FROM cash_journal_entries_v30
            WHERE reference_type='BUYBACK' AND reference_id=?
              AND entry_type='CASH_BUYBACK'
            ORDER BY id LIMIT 1
            """,
            (int(voucher["buyback_id"]),),
        ).fetchone()
        if legacy:
            expected_references = {
                str(voucher["voucher_no"] or "").strip(),
                str(voucher["buyback_no"] or "").strip(),
            }
            if (
                money(legacy["amount"]) != -amount
                or str(legacy["reference_no"] or "").strip() not in expected_references
            ):
                raise CashJournalError(
                    "Obstoječi blagajniški izdatek odkupa ima drugačen znesek ali referenco; "
                    "samodejna povezava ni dovoljena."
                )
            now = datetime.now().isoformat(timespec="seconds")
            updated = c.execute(
                """
                UPDATE svc.cash_disbursements
                SET status='BOOKED',cash_session_id=?,cash_entry_id=?,booked_at=?
                WHERE id=? AND status='CONFIRMED' AND cash_entry_id IS NULL
                """,
                (
                    int(legacy["session_id"]), int(legacy["id"]), now,
                    int(voucher_id),
                ),
            )
            if updated.rowcount != 1:
                raise CashJournalError("Izdatka ni bilo mogoče enolično povezati.")
            c.commit()
            return {
                "voucher_id": int(voucher["id"]),
                "voucher_no": str(voucher["voucher_no"]),
                "cash_entry_id": int(legacy["id"]),
                "cash_session_id": int(legacy["session_id"]),
                "entry_no": int(legacy["entry_no"]),
                "already_booked": True,
            }
        session_id = require_open_cash_session(
            c, premise_id=str(premise_id), device_id=str(device_id)
        )
        entry_id = record_cash_entry(
            c,
            session_id=session_id,
            entry_type="CASH_EXPENSE",
            amount=-amount,
            reference_type="BUYBACK_CASH_DISBURSEMENT",
            reference_id=int(voucher["id"]),
            reference_no=str(voucher["voucher_no"]),
            description=str(voucher["purpose"]),
            operator_code=str(operator_code),
        )
        now = datetime.now().isoformat(timespec="seconds")
        updated = c.execute(
            """
            UPDATE svc.cash_disbursements
            SET status='BOOKED',cash_session_id=?,cash_entry_id=?,booked_at=?
            WHERE id=? AND status='CONFIRMED' AND cash_entry_id IS NULL
            """,
            (session_id, entry_id, now, int(voucher_id)),
        )
        if updated.rowcount != 1:
            raise CashJournalError("Izdatka ni bilo mogoče enolično knjižiti.")
        entry_no = c.execute(
            "SELECT entry_no FROM cash_journal_entries_v30 WHERE id=?", (entry_id,)
        ).fetchone()[0]
        c.commit()
        return {
            "voucher_id": int(voucher["id"]),
            "voucher_no": str(voucher["voucher_no"]),
            "cash_entry_id": entry_id,
            "cash_session_id": session_id,
            "entry_no": int(entry_no),
            "already_booked": False,
        }
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def cash_session_snapshot(
    office_db: Path | str, *, premise_id: str, device_id: str,
) -> tuple[dict|None,list[dict]]:
    ensure_cash_schema(office_db)
    c = sqlite3.connect(str(office_db)); c.row_factory=sqlite3.Row
    try:
        row=_open_row(c,premise_id,device_id)
        if not row:
            return None,[]
        entries=c.execute(
            "SELECT * FROM cash_journal_entries_v30 WHERE session_id=? ORDER BY entry_no",
            (int(row["id"]),),
        ).fetchall()
        result=dict(row)
        result["expected_cash"]=float(
            money(row["opening_cash"])+sum((money(x["amount"]) for x in entries),Decimal("0"))
        )
        result["cash_receipts"]=float(sum((money(x["amount"]) for x in entries if money(x["amount"])>0),Decimal("0")))
        result["cash_expenses"]=float(-sum((money(x["amount"]) for x in entries if money(x["amount"])<0),Decimal("0")))
        return result,[dict(x) for x in entries]
    finally:c.close()


def close_cash_session(
    office_db: Path | str, *, premise_id: str, device_id: str,
    operator_code: str, counted_cash, note: str="",
) -> dict:
    counted=money(counted_cash)
    if counted<0: raise CashJournalError("Prešteta gotovina ne sme biti negativna.")
    c=sqlite3.connect(str(office_db)); c.row_factory=sqlite3.Row
    try:
        ensure_cash_schema_connection(c); c.execute("BEGIN IMMEDIATE")
        row=_open_row(c,premise_id,device_id)
        if not row: raise CashJournalError("Blagajna ni odprta.")
        total=money(c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM cash_journal_entries_v30 WHERE session_id=?",
            (int(row["id"]),),
        ).fetchone()[0])
        expected=(money(row["opening_cash"])+total).quantize(CENT)
        difference=(counted-expected).quantize(CENT)
        now=datetime.now().isoformat(timespec="seconds")
        c.execute(
            "UPDATE cash_sessions_v30 SET status='CLOSED',closed_at=?,closed_by=?,"
            "expected_cash=?,counted_cash=?,difference=?,close_note=? WHERE id=? AND status='OPEN'",
            (now,str(operator_code),float(expected),float(counted),float(difference),str(note),int(row["id"])),
        )
        c.commit()
        return {"id":int(row["id"]),"session_no":row["session_no"],"expected":expected,"counted":counted,"difference":difference,"closed_at":now}
    except Exception:c.rollback();raise
    finally:c.close()


def generate_cash_journal_pdf(
    office_db: Path | str, *, session_id: int, output_dir: Path | str,
    company: dict,
) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph,SimpleDocTemplate,Spacer,Table,TableStyle

    ensure_cash_schema(office_db)
    c=sqlite3.connect(str(office_db));c.row_factory=sqlite3.Row
    try:
        session=c.execute("SELECT * FROM cash_sessions_v30 WHERE id=?",(int(session_id),)).fetchone()
        if not session: raise CashJournalError("Blagajniški dnevnik ni najden.")
        entries=c.execute(
            "SELECT * FROM cash_journal_entries_v30 WHERE session_id=? ORDER BY entry_no",
            (int(session_id),),
        ).fetchall()
    finally:c.close()
    opening=money(session["opening_cash"])
    receipts=sum((money(x["amount"]) for x in entries if money(x["amount"])>0),Decimal("0"))
    expenses=-sum((money(x["amount"]) for x in entries if money(x["amount"])<0),Decimal("0"))
    expected=opening+receipts-expenses
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    path=output/f"{session['session_no']}_blagajniski_dnevnik.pdf"
    styles=getSampleStyleSheet(); body=styles["BodyText"]
    story=[
        Paragraph(str(company.get("legal_name") or "MikroFix"),styles["Heading2"]),
        Paragraph(str(company.get("address") or "Paradiž 73, 2282 Cirkulane"),body),
        Spacer(1,5*mm),Paragraph(f"BLAGAJNIŠKI DNEVNIK {session['session_no']}",styles["Title"]),
        Paragraph(
            f"Prostor/naprava: {session['premise_id']}/{session['device_id']}<br/>"
            f"Odprto: {str(session['opened_at']).replace('T',' ')} – {session['opened_by']}<br/>"
            f"Zaprto: {str(session['closed_at'] or '-').replace('T',' ')} – {session['closed_by'] or '-'}",
            body,
        ),Spacer(1,4*mm),
    ]
    rows=[["Št.","Čas","Vrsta","Listina","Opis","Prejemek","Izdatek","Saldo"]]
    for x in entries:
        amount=money(x["amount"])
        rows.append([
            str(x["entry_no"]),str(x["occurred_at"])[11:19],str(x["entry_type"]),
            str(x["reference_no"] or "-"),Paragraph(str(x["description"]),body),
            f"{amount:.2f}" if amount>0 else "",f"{-amount:.2f}" if amount<0 else "",
            f"{money(x['balance_after']):.2f}",
        ])
    table=Table(rows,colWidths=[9*mm,18*mm,25*mm,25*mm,48*mm,20*mm,20*mm,20*mm],repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E8EDF2")),
        ("GRID",(0,0),(-1,-1),.3,colors.grey),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),7),("VALIGN",(0,0),(-1,-1),"TOP"),
        ("ALIGN",(5,1),(-1,-1),"RIGHT"),
    ]))
    story.extend([table,Spacer(1,5*mm),Paragraph(
        f"Začetno stanje: {opening:.2f} EUR<br/>Prejemki skupaj: {receipts:.2f} EUR<br/>"
        f"Izdatki skupaj: {expenses:.2f} EUR<br/><b>Pričakovano stanje: {expected:.2f} EUR</b><br/>"
        f"Prešteto stanje: {money(session['counted_cash']):.2f} EUR<br/>"
        f"Razlika: {money(session['difference']):.2f} EUR<br/>Opomba: {session['close_note'] or '-'}",
        body,
    ),Spacer(1,12*mm),Paragraph("Blagajnik: ____________________    Odgovorna oseba: ____________________",body)])
    SimpleDocTemplate(str(path),pagesize=A4,leftMargin=10*mm,rightMargin=10*mm,topMargin=12*mm,bottomMargin=12*mm).build(story)
    return path

