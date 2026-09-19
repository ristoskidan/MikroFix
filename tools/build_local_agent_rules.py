#!/usr/bin/env python3
"""Build small, auditable rule modules from the canonical local-agent context."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "MIKROFIX_GPT_OSS_20B_KONTEKST.md"
OUTPUT = ROOT / "docs" / "local_agents" / "pravila"

GROUPS = {
    "core": {
        "file": "00_JEDRO.md",
        "sections": [1, 2, 3, 19, 20],
        "keywords": [],
    },
    "architecture": {
        "file": "10_ARHITEKTURA_IN_DOSTOP.md",
        "sections": [4, 5, 6],
        "keywords": [
            "arhitektura", "api", "postgresql", "sqlite", "prijava", "pin",
            "uporabnik", "dovoljen", "vloga", "seja", "operation_id",
            "idempot", "sočas", "concurr", "workstation", "delovna postaja",
        ],
    },
    "service": {
        "file": "20_SERVICE.md",
        "sections": [7, 8, 9],
        "keywords": [
            "servis", "service", "čas", "interval", "microdetect", "imei",
            "naprav", "tehnični zaključek", "obračunski paket", "fotograf",
        ],
    },
    "stock_offline": {
        "file": "30_ZALOGA_OFFLINE_IN_INVENTURA.md",
        "sections": [10, 11, 14],
        "keywords": [
            "zaloga", "artikel", "del", "stock", "inventory", "inventura",
            "offline", "sinhron", "outbox", "črtna koda", "barkod", "lot",
            "dobavnica", "donor", "sync_conflict",
        ],
    },
    "invoices": {
        "file": "40_RACUNI_BLAGAJNA_FURS.md",
        "sections": [12, 13],
        "keywords": [
            "račun", "invoice", "blagaj", "cashier", "furs", "storno",
            "plačil", "payment", "rabat", "popust", "vrstic", "f10", "zoi",
            "eor", "ničel", "0,00", "zero_balance", "trr", "gotovin",
        ],
    },
    "documents": {
        "file": "50_DOKUMENTI_ZASEBNOST_GARANCIJE.md",
        "sections": [15],
        "keywords": [
            "dokument", "zasebnost", "garanc", "reklamac", "fotograf", "pin",
            "gesl", "imei", "hramba", "retenc", "stranka",
        ],
    },
    "buyback": {
        "file": "60_ODKUP_IN_POOBLASTILO.md",
        "sections": [16],
        "keywords": [
            "odkup", "pooblast", "pooblaščen", "prodajalec", "pogodba",
            "otp", "sigen", "prevzem naprav", "lastništvo", "kratkotrajno delo",
        ],
    },
    "pricing": {
        "file": "70_CENE_IN_FORMULE.md",
        "sections": [17],
        "keywords": [
            "cena", "cenik", "formula", "l0", "t1", "t2", "marža", "rabat",
            "diagnostika", "minimum", "minimalna cena", "odkupna formula",
        ],
    },
    "implementation": {
        "file": "80_STANJE_IZVEDBE.md",
        "sections": [18],
        "keywords": [
            "implement", "izvedb", "koda", "test", "produkc", "napredek",
            "stanje", "manjka", "runtime", "strežnik",
        ],
    },
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
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def split_sections(text: str) -> tuple[str, dict[int, str]]:
    matches = list(re.finditer(r"(?m)^## (\d+)\. .+$", text))
    if not matches:
        raise ValueError("Kanon nima oštevilčenih razdelkov.")
    preamble = text[: matches[0].start()].rstrip()
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[int(match.group(1))] = text[match.start() : end].rstrip()
    return preamble, sections


def main() -> int:
    source_text = SOURCE.read_text(encoding="utf-8")
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    preamble, sections = split_sections(source_text)
    expected = set(range(1, 21))
    if set(sections) != expected:
        raise ValueError(f"Pričakovani razdelki 1–20, prejeti: {sorted(sections)}")

    generated_header = (
        "<!-- GENERIRANO iz docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md; "
        "ne urejaj ročno. -->\n\n"
    )
    for name, definition in GROUPS.items():
        parts = [sections[number] for number in definition["sections"]]
        if name == "core":
            parts.insert(0, preamble)
        content = generated_header + "\n\n---\n\n".join(parts).rstrip() + "\n"
        atomic_write(OUTPUT / definition["file"], content)

    index = {
        "version": 1,
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": source_hash,
        "always": [GROUPS["core"]["file"]],
        "scopes": {
            name: {
                "file": definition["file"],
                "keywords": definition["keywords"],
            }
            for name, definition in GROUPS.items()
            if name != "core"
        },
    }
    atomic_write(
        OUTPUT / "INDEX.json",
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(f"Izdelanih {len(GROUPS)} modulov v {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
