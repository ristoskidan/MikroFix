"""Skupne, strogo omejene funkcije operacijskega sistema za MikroFIX."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def open_local_file(path: Path | str) -> None:
    """Odpre obstoječo lokalno datoteko s privzeto namizno aplikacijo."""
    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    if os.name == "nt":
        os.startfile(str(target))
        return
    # xdg-open pogosto ostane živ toliko časa kot odprti pregledovalnik PDF-ja.
    # Zato ga ne čakamo: uspešen zagon procesa je dovolj, uporabniški vmesnik pa
    # mora takoj ostati odziven.
    subprocess.Popen(
        ["xdg-open", str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def protect_private_path(path: Path | str, *, skip: bool = False) -> None:
    """Omeji produkcijske datoteke na prijavljenega uporabnika.

    Windows ohrani obstoječi ACL. Na POSIX sistemu uporabi 0600 za datoteko
    oziroma 0700 za imenik. Napaka je namenoma posredovana klicatelju.
    """
    target = Path(path)
    if skip or not target.exists():
        return
    if os.name == "nt":
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            raise RuntimeError("Trenutnega uporabnika ni mogoče določiti.")
        completed = subprocess.run(
            [
                "icacls", str(target), "/inheritance:r", "/grant:r",
                f"{user}:(F)", "SYSTEM:(F)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "Nastavitev ACL ni uspela.")
        return
    target.chmod(0o700 if target.is_dir() else 0o600)
