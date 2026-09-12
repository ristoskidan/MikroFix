import sqlite3
import threading
import time
import unittest

from mikrofix_sync_client import SyncRunResult
from mikrofix_sync_runtime import BackgroundSyncWorker


class BackgroundSyncWorkerTests(unittest.TestCase):
    def test_start_wake_and_stop_never_overlap_cycles(self):
        calls = []
        statuses = []
        completed = threading.Event()
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def synchronize(connection, transport, device_id):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            calls.append(device_id)
            time.sleep(0.02)
            with lock:
                active -= 1
            if len(calls) >= 2:
                completed.set()
            return SyncRunResult(1, 0, len(calls), False)

        worker = BackgroundSyncWorker(
            connection_factory=lambda: sqlite3.connect(":memory:"),
            transport=object(),
            device_id="SERVICE-MFD-PIS",
            interval_seconds=60,
            on_status=statuses.append,
            synchronize=synchronize,
        )
        try:
            self.assertTrue(worker.start())
            self.assertFalse(worker.start())
            deadline = time.monotonic() + 1
            while len(calls) < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            worker.request_sync()
            self.assertTrue(completed.wait(1))
        finally:
            self.assertTrue(worker.stop())
        self.assertFalse(worker.running)
        self.assertEqual(maximum_active, 1)
        self.assertGreaterEqual(len(statuses), 2)

    def test_connection_failure_is_reported_and_worker_stays_alive(self):
        statuses = []
        reported = threading.Event()

        def status(value):
            statuses.append(value)
            reported.set()

        worker = BackgroundSyncWorker(
            connection_factory=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
            transport=object(),
            device_id="SERVICE-MFD-PIS",
            interval_seconds=60,
            on_status=status,
        )
        try:
            worker.start()
            self.assertTrue(reported.wait(1))
            self.assertTrue(worker.running)
        finally:
            self.assertTrue(worker.stop())
        self.assertFalse(statuses[0].ok)
        self.assertIn("offline", statuses[0].message)


if __name__ == "__main__":
    unittest.main()
