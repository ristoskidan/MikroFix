"""Production launcher for one explicitly selected MikroFIX application."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from preveri_produkcijo_linux import collect


APP_DIR = Path(__file__).resolve().parent
PRODUCTION_CONFIG = APP_DIR / "mikrofix_settings.json"


def prepare_production_environment() -> None:
    """Stable must never inherit a TEST/E2E runtime override."""
    for name in (
        "MIKROFIX_E2E_TEST",
        "MIKROFIX_DATA_DIR",
        "MIKROFIX_SMOKE_TEST",
        "MIKROFIX_SKIP_FILE_PROTECTION",
    ):
        os.environ.pop(name, None)
    os.environ["MIKROFIX_CONFIG"] = str(PRODUCTION_CONFIG)


APPLICATION_TARGETS = {
    "service": ("mikrofix.py", "--service-only"),
    "servis": ("mikrofix.py", "--service-only"),
    "office": ("mikrofix_office.py",),
    "cashier": ("mikrofix_office.py", "--new-invoice"),
    "blagajna": ("mikrofix_office.py", "--new-invoice"),
}


def launch(application: str) -> int:
    """Run one application in its own process after all production gates pass."""
    prepare_production_environment()
    try:
        results = collect()
    except Exception as exc:
        results = [(False, "Predpregled", str(exc))]
    failed = [(label, detail) for ok, label, detail in results if not ok]
    if failed:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "MikroFIX - produkcija še ni pripravljena",
            "Program se zaradi zaščite ni zagnal:\n\n"
            + "\n".join(f"• {label}: {detail}" for label, detail in failed),
            parent=root,
        )
        root.destroy()
        return 1
    target = APPLICATION_TARGETS.get(str(application or "").strip().lower())
    if target is None:
        raise ValueError(f"Neznan program MikroFIX: {application}")
    python = APP_DIR / ".venv/bin/python"
    script, *arguments = target
    os.execv(
        str(python),
        [str(python), str(APP_DIR / script), *arguments],
    )
    return 0


def main() -> int:
    application = sys.argv[1] if len(sys.argv) > 1 else "service"
    return launch(application)


if __name__ == "__main__":
    raise SystemExit(main())
