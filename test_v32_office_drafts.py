import sqlite3
import unittest
from uuid import uuid4

from mikrofix_office_drafts import (
    OfficeDraftError,
    ensure_office_draft_schema,
    list_active_office_work_drafts,
    load_office_work_draft,
    save_office_work_draft,
    set_office_work_draft_status,
)


class CentralOfficeWorkDraftTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        ensure_office_draft_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_incomplete_form_survives_reload_without_posting_business_data(self):
        draft_id = save_office_work_draft(
            self.connection, actor_id="office-a", device_id="OFFICE-PC-A",
            draft_kind="PURCHASE_RECEIPT",
            payload={"external_document_no": "DOB-1", "lines": []},
        )
        loaded = load_office_work_draft(
            self.connection, draft_id, actor_id="office-a", device_id="OFFICE-PC-A",
        )
        self.assertEqual(loaded["payload"]["external_document_no"], "DOB-1")
        self.assertEqual(loaded["status"], "ACTIVE")

        save_office_work_draft(
            self.connection, actor_id="office-a", device_id="OFFICE-PC-A",
            draft_kind="PURCHASE_RECEIPT", draft_id=draft_id,
            payload={"external_document_no": "DOB-1", "lines": [{"quantity": "2"}]},
        )
        active = list_active_office_work_drafts(
            self.connection, actor_id="office-a", device_id="OFFICE-PC-A",
        )
        self.assertEqual(active[0]["payload"]["lines"][0]["quantity"], "2")

    def test_owner_isolation_and_terminal_status_prevent_overwrite(self):
        draft_id = str(uuid4())
        save_office_work_draft(
            self.connection, actor_id="office-a", device_id="OFFICE-PC-A",
            draft_kind="ATTACH_PURCHASE_INVOICE", source_id=str(uuid4()),
            draft_id=draft_id, payload={"invoice_external_no": "PRA-1"},
        )
        self.assertIsNone(load_office_work_draft(
            self.connection, draft_id, actor_id="office-b", device_id="OFFICE-PC-A",
        ))
        set_office_work_draft_status(
            self.connection, draft_id, actor_id="office-a", device_id="OFFICE-PC-A",
            status="COMPLETED",
        )
        with self.assertRaisesRegex(OfficeDraftError, "cannot be changed"):
            save_office_work_draft(
                self.connection, actor_id="office-a", device_id="OFFICE-PC-A",
                draft_kind="ATTACH_PURCHASE_INVOICE", draft_id=draft_id,
                payload={"invoice_external_no": "PRA-2"},
            )

    def test_checksum_detects_local_corruption(self):
        draft_id = save_office_work_draft(
            self.connection, actor_id="office-a", device_id="OFFICE-PC-A",
            draft_kind="PURCHASE_RECEIPT", payload={"lines": []},
        )
        self.connection.execute(
            "UPDATE central_office_work_drafts_v32 SET payload_json='{}' WHERE draft_id=?",
            (draft_id,),
        )
        with self.assertRaisesRegex(OfficeDraftError, "corrupted"):
            load_office_work_draft(
                self.connection, draft_id,
                actor_id="office-a", device_id="OFFICE-PC-A",
            )


if __name__ == "__main__":
    unittest.main()
