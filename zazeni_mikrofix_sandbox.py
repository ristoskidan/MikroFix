"""Launch the permanently isolated MikroFix update-planning sandbox."""

from __future__ import annotations

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG = APP_DIR / "mikrofix_settings_sandbox.json"


def main() -> int:
    for name in (
        "MIKROFIX_E2E_TEST",
        "MIKROFIX_DATA_DIR",
        "MIKROFIX_SMOKE_TEST",
        "MIKROFIX_SKIP_FILE_PROTECTION",
        "MIKROFIX_ALLOW_FURS_PRODUCTION",
    ):
        os.environ.pop(name, None)
    os.environ["MIKROFIX_CONFIG"] = str(CONFIG)
    python = APP_DIR / ".venv/bin/python"
    os.execv(str(python), [str(python), str(APP_DIR / "mikrofix.py")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
