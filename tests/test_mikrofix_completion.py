import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "mikrofix_completion.py"
SPEC = importlib.util.spec_from_file_location("mikrofix_completion", MODULE_PATH)
completion = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(completion)

THREAD_ID = "12345678-1234-5678-9234-567812345678"


class FakeRunner:
    def __init__(self, returncode=0, error=None):
        self.calls = []
        self.returncode = returncode
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(returncode=self.returncode, stdout="secret", stderr="secret")


class CompletionOutboxTests(unittest.TestCase):
    def payload(self, **changes):
        value = {
            "job_id": "MF-2026-0043",
            "state": "AWAIT_CODEX_REVIEW",
            "summary": "review the durable result",
        }
        value.update(changes)
        return value

    def test_enqueue_is_deterministic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = completion.CompletionOutbox(directory)
            first = outbox.enqueue(self.payload())
            second = outbox.enqueue(self.payload())
            self.assertEqual(first, second)
            self.assertEqual(len(list((Path(directory) / "events").iterdir())), 1)

    def test_event_claim_and_receipt_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            outbox = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=runner
            )
            result = outbox.publish(self.payload())
            for collection in ("events", "claims", "receipts"):
                path = Path(directory) / collection / f"{result['event_id']}.json"
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_claim_allows_exactly_one_invocation_across_restart_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            first_runner = FakeRunner()
            first = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=first_runner
            )
            delivered = first.publish(self.payload())
            second_runner = FakeRunner()
            restarted = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=second_runner
            )
            replayed = restarted.publish(self.payload())
            self.assertEqual(delivered["status"], "delivered")
            self.assertEqual(replayed["status"], "delivered")
            self.assertEqual(len(first_runner.calls), 1)
            self.assertEqual(second_runner.calls, [])

    def test_no_invocation_without_configured_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            outbox = completion.CompletionOutbox(directory, runner=runner)
            result = outbox.publish(self.payload())
            self.assertEqual(result["status"], "queued_no_thread")
            self.assertEqual(runner.calls, [])
            self.assertEqual(list((Path(directory) / "claims").iterdir()), [])

    def test_desktop_notification_is_sent_once_without_configured_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = FakeRunner()
            first = completion.CompletionOutbox(
                directory, notify_runner=notifications
            )
            result = first.publish(self.payload(
                display_name="Preizkus lokalne naloge",
                state="COMPLETED",
            ))
            restarted = completion.CompletionOutbox(
                directory, notify_runner=notifications
            )
            restarted.publish(self.payload(
                display_name="Preizkus lokalne naloge",
                state="COMPLETED",
            ))

            self.assertEqual(result["status"], "queued_no_thread")
            self.assertEqual(len(notifications.calls), 1)
            argv, kwargs = notifications.calls[0]
            self.assertEqual(argv[0], "/usr/bin/notify-send")
            self.assertIn("MikroFIX: naloga je končana", argv)
            self.assertIn("Preizkus lokalne naloge\nStanje: COMPLETED", argv)
            self.assertFalse(kwargs["shell"])
            marker = (
                Path(directory) / "notifications" / f"{result['event_id']}.json"
            )
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_invalid_or_noncanonical_thread_uuid_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for invalid in ("not-a-uuid", "12345678-1234-5678-9234-56781234567A"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(completion.CompletionError):
                        completion.CompletionOutbox(directory, thread_id=invalid)

    def test_codex_invocation_uses_literal_argv_and_shell_false(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            outbox = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=runner, timeout_seconds=9
            )
            outbox.publish(self.payload())
            argv, kwargs = runner.calls[0]
            self.assertEqual(
                argv[:5],
                [
                    "/usr/lib/chatgpt/resources/codex",
                    "queue",
                    "--thread",
                    THREAD_ID,
                    "--message",
                ],
            )
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 9)
            self.assertEqual(
                kwargs["env"],
                {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            )

    def test_message_is_bounded_structured_and_excludes_sensitive_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            secret = "TOKEN=super-secret " + ("diff --git a/x b/x " * 100)
            outbox = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=runner
            )
            outbox.publish(self.payload(summary=secret))
            message = runner.calls[0][0][-1]
            parsed = json.loads(message)
            self.assertLessEqual(len(message.encode("utf-8")), completion.MAX_MESSAGE_BYTES)
            self.assertEqual(
                set(parsed),
                {"display_name", "event_id", "event_path", "job_id", "state"},
            )
            self.assertTrue(Path(parsed["event_path"]).is_file())
            self.assertNotIn("super-secret", message)
            self.assertNotIn("diff --git", message)

    def test_queue_exception_leaves_event_claim_and_failure_receipt_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = FakeRunner()
            failing = FakeRunner(error=subprocess.TimeoutExpired("codex", 5))
            outbox = completion.CompletionOutbox(
                directory,
                thread_id=THREAD_ID,
                runner=failing,
                notify_runner=notifications,
            )
            result = outbox.publish(self.payload())
            receipt_path = (
                Path(directory) / "receipts" / f"{result['event_id']}.json"
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["detail"], "exception:TimeoutExpired")
            self.assertTrue(
                (Path(directory) / "events" / f"{result['event_id']}.json").exists()
            )
            self.assertTrue(
                (Path(directory) / "claims" / f"{result['event_id']}.json").exists()
            )
            replay_runner = FakeRunner()
            replay = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=replay_runner
            ).publish(self.payload())
            self.assertEqual(replay["status"], "failed")
            self.assertEqual(replay_runner.calls, [])
            self.assertEqual(len(notifications.calls), 1)
            self.assertFalse(notifications.calls[0][1]["shell"])
            self.assertIn(
                "Obvestilo Codexu ni bilo dostavljeno",
                notifications.calls[0][0][-1],
            )

    def test_review_state_is_mapped_in_event_and_message(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            outbox = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=runner
            )
            result = outbox.publish(self.payload())
            event_path = Path(directory) / "events" / f"{result['event_id']}.json"
            event_text = event_path.read_text(encoding="utf-8")
            self.assertNotIn("AWAIT_CODEX_REVIEW", event_text)
            self.assertEqual(
                json.loads(event_text)["payload"]["state"],
                "READY_FOR_CODEX_REVIEW",
            )
            self.assertEqual(
                json.loads(runner.calls[0][0][-1])["state"],
                "READY_FOR_CODEX_REVIEW",
            )

    def test_payload_schema_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = completion.CompletionOutbox(directory)
            with self.assertRaises(completion.CompletionError):
                outbox.enqueue(self.payload(raw_model_output="do not persist"))

    def test_safe_evidence_paths_and_hashes_are_private_event_only(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = str((Path(directory) / "manifest.json").resolve())
            digest = "a" * 64
            runner = FakeRunner()
            outbox = completion.CompletionOutbox(
                directory, thread_id=THREAD_ID, runner=runner
            )
            result = outbox.publish(self.payload(
                display_name="Human friendly job",
                evidence_paths=[evidence],
                evidence_sha256={evidence: digest},
                test_result="12 tests; PASS",
                risks="not merged",
            ))
            event = json.loads(
                (Path(directory) / "events" / f"{result['event_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([evidence], event["payload"]["evidence_paths"])
            message = runner.calls[0][0][-1]
            self.assertNotIn(digest, message)
            self.assertNotIn("12 tests", message)


if __name__ == "__main__":
    unittest.main()
