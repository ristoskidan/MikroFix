import json
from pathlib import Path
import tempfile
import unittest

from tools.mikrofix_lead_session import SOURCES, confirm_context, ensure_lead_session


class LeadSessionTests(unittest.TestCase):
    def test_context_is_built_once_acknowledged_and_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            home = Path(temporary) / "state"
            for relative in SOURCES:
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(relative + "\n", encoding="utf-8")
            first = ensure_lead_session(root, home)
            self.assertEqual("AWAITING_CONTEXT_ACK", first["state"])
            ready = confirm_context(
                home, str(first["session_id"]), str(first["memory_sha256"])
            )
            self.assertEqual("CONTEXT_READY", ready["state"])
            acknowledgement = json.loads((home / "context-ready.json").read_text())
            self.assertEqual("CONTEXT_READY", acknowledgement["context_ack"])
            self.assertEqual(first["session_id"], acknowledgement["session_id"])
            second = ensure_lead_session(root, home)
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertEqual("CONTEXT_READY", second["state"])
            persisted = json.loads((home / "lead-session.json").read_text())
            self.assertEqual("CONTEXT_READY", persisted["state"])

    def test_changed_context_requires_a_new_ack_but_keeps_parent_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            home = Path(temporary) / "state"
            for relative in SOURCES:
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("base\n", encoding="utf-8")
            first = ensure_lead_session(root, home)
            confirm_context(home, str(first["session_id"]), str(first["memory_sha256"]))
            (root / SOURCES[0]).write_text("changed\n", encoding="utf-8")
            changed = ensure_lead_session(root, home)
            self.assertEqual(first["session_id"], changed["session_id"])
            self.assertEqual("AWAITING_CONTEXT_ACK", changed["state"])


if __name__ == "__main__":
    unittest.main()
