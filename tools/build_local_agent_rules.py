#!/usr/bin/env python3
"""Build auditable task-scoped modules from the current MikroFIX canon."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "local_agents" / "pravila"

MODULES = {
    "business": {
        "file": "10_POSLOVNA_PRAVILA.md",
        "source": "docs/kanon/01_POSLOVNA_PRAVILA.md",
        "keywords": [
            "office", "servis", "service", "blagaj", "račun", "invoice",
            "zaloga", "artikel", "furs", "storno", "plačil", "rabat",
            "znesek", "decimal", "cena", "odkup", "garanc", "reklamac",
            "inventura", "offline", "microdetect", "imei", "dokument",
        ],
    },
    "architecture": {
        "file": "20_CILJNA_ARHITEKTURA.md",
        "source": "docs/kanon/02_CILJNA_ARHITEKTURA.md",
        "keywords": [
            "arhitektur", "api", "postgres", "sqlite", "outbox", "sync",
            "idempot", "operation_id", "sočas", "concurr", "stanje",
            "transakc", "backup", "migracij",
        ],
    },
    "acceptance": {
        "file": "30_SPREJEMNI_TESTI.md",
        "source": "docs/kanon/03_SPREJEMNI_TESTI.md",
        "keywords": [
            "test", "prever", "sprejem", "regres", "edge", "robna",
            "pričakov", "scenarij", "pass", "fail",
        ],
    },
    "implementation": {
        "file": "40_STANJE_IZVEDBE.md",
        "source": "docs/kanon/04_STANJE_IZVEDBE.md",
        "keywords": [
            "implement", "izvedb", "trenutno", "obstoječ", "koda",
            "narejeno", "manjka", "produkc", "runtime",
        ],
    },
    "history": {
        "file": "50_ZGODOVINA_ODLOCITEV.md",
        "source": "docs/kanon/05_ZGODOVINA_ODLOCITEV.md",
        "keywords": [
            "zgodovin", "odločitev", "nadomešč", "odprto", "blokad",
            "zakaj", "izvor", "prejšnj",
        ],
    },
}

ALWAYS = {
    "file": "00_AVTORITETA.md",
    "source": "docs/kanon/00_KAZALO.md",
}


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o644)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def source_text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def generated_module(relative: str) -> str:
    return (
        f"<!-- GENERIRANO iz {relative}; ne urejaj ročno. -->\n\n"
        + source_text(relative).rstrip()
        + "\n"
    )


def main() -> int:
    definitions = {"authority": ALWAYS, **MODULES}
    sources: dict[str, str] = {}
    expected_files: set[str] = set()
    for definition in definitions.values():
        relative = str(definition["source"])
        file_name = str(definition["file"])
        content = source_text(relative)
        sources[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        atomic_write(OUTPUT / file_name, generated_module(relative))
        expected_files.add(file_name)

    for existing in OUTPUT.glob("*.md"):
        if existing.name not in expected_files:
            existing.unlink()

    index = {
        "version": 2,
        "sources": dict(sorted(sources.items())),
        "always": [ALWAYS["file"]],
        "scopes": {
            name: {
                "file": definition["file"],
                "source": definition["source"],
                "keywords": definition["keywords"],
            }
            for name, definition in MODULES.items()
        },
    }
    atomic_write(
        OUTPUT / "INDEX.json",
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(f"Izdelanih {len(definitions)} modulov v {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
