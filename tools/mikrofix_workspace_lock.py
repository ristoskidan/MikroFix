#!/usr/bin/env python3
"""Process-wide shared/exclusive lock for one MikroFIX agent workspace."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading
from typing import Iterator


_STATE = threading.local()


def lock_path(workspace: Path) -> Path:
    workspace = workspace.expanduser().absolute()
    return workspace.parent / f".{workspace.name}.rotation.lock"


@contextmanager
def workspace_lock(
    workspace: Path, *, exclusive: bool = False, blocking: bool = True,
) -> Iterator[Path]:
    """Hold the stable sibling lock; nested locks in one thread are reentrant."""
    path = lock_path(workspace)
    key = (str(path), exclusive)
    held = getattr(_STATE, "held", {})
    if held:
        # A process already admitted to this workspace may safely nest calls.
        existing_path = next(iter(held))[0]
        if existing_path != str(path):
            raise RuntimeError("nested access to a different workspace is forbidden")
        if exclusive and not any(existing_exclusive for _, existing_exclusive in held):
            raise RuntimeError("cannot upgrade a shared workspace lock in place")
        held[key] = held.get(key, 0) + 1
        try:
            yield path
        finally:
            held[key] -= 1
            if not held[key]:
                del held[key]
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f"workspace is busy: {workspace}") from None
    _STATE.held = {key: 1}
    try:
        yield path
    finally:
        _STATE.held = {}
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
