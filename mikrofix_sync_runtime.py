"""Non-blocking lifecycle for periodic Service outbox synchronization."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from mikrofix_sync_client import SyncRunResult, SyncTransport, synchronize_once


@dataclass(frozen=True, slots=True)
class BackgroundSyncStatus:
    ok: bool
    message: str
    result: SyncRunResult | None = None


class BackgroundSyncWorker:
    """Run one sync cycle at a time without ever blocking the Tk event loop."""

    def __init__(
        self,
        *,
        connection_factory: Callable,
        transport: SyncTransport,
        device_id: str,
        interval_seconds: float = 15.0,
        on_status: Callable[[BackgroundSyncStatus], None] | None = None,
        synchronize: Callable = synchronize_once,
    ):
        self.connection_factory = connection_factory
        self.transport = transport
        self.device_id = str(device_id)
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.on_status = on_status
        self.synchronize = synchronize
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self._wake.set()
        self._thread = threading.Thread(
            target=self._run,
            name=f"mikrofix-sync-{self.device_id}",
            daemon=True,
        )
        self._thread.start()
        return True

    def request_sync(self) -> None:
        """Wake the worker after a new local operation; safe while offline."""
        self._wake.set()

    def stop(self, *, join_timeout: float = 2.0) -> bool:
        """Request shutdown and report whether the worker is fully stopped."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(join_timeout)))
        return not self.running

    def _notify(self, status: BackgroundSyncStatus) -> None:
        if self.on_status is not None:
            try:
                self.on_status(status)
            except Exception:
                # A UI callback must never kill synchronization.
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            connection = None
            try:
                connection = self.connection_factory()
                result = self.synchronize(
                    connection, self.transport, self.device_id
                )
                if result.interrupted:
                    self._notify(BackgroundSyncStatus(
                        False, "Sinhronizacija je prekinjena; poskus se bo ponovil.",
                        result,
                    ))
                else:
                    self._notify(BackgroundSyncStatus(
                        True,
                        f"Sinhronizirano: poslano {result.sent}, prejeto {result.received}.",
                        result,
                    ))
            except Exception as exc:
                self._notify(BackgroundSyncStatus(
                    False, f"Brez povezave s strežnikom: {exc}", None
                ))
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            self._wake.wait(self.interval_seconds)
