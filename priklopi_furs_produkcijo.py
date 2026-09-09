#!/usr/bin/env python3
"""Enkratni, nadzorovani priklop MikroFIX na produkcijski FURS."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

from mikrofix_furs import FursProductionClient, load_production_certificate


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "mikrofix_settings.json"


def main() -> int:
    raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    furs = raw.setdefault("furs", {})
    business = raw.setdefault("business", {})
    # Poslovni podatki so običajno v privzetih nastavitvah in zato niso nujno
    # ponovljeni v lokalni datoteki.
    from mikrofix_runtime import DEFAULT_SETTINGS
    expected_tax = str(business.get("tax_number") or DEFAULT_SETTINGS["business"]["tax_number"])
    candidate = str(
        furs.get("certificate_path")
        or furs.get("production_certificate_candidate")
        or "/home/mikrofix/.local/share/mikrofix/certs/davpotrac.pfx"
    )
    cert_path = Path(candidate).expanduser().resolve()
    print("MikroFIX – nadzorovani priklop FURS PRODUKCIJA")
    print(f"Potrdilo: {cert_path}")
    print(f"Pričakovana davčna številka: {expected_tax}")
    password = getpass.getpass("Geslo namenskega potrdila (ne bo shranjeno): ")
    bundle = load_production_certificate(
        cert_path, password, expected_tax_number=expected_tax
    )
    ca_path = APP_DIR / str(
        furs.get("production_ca_bundle_path") or "certs/furs-test-ca-bundle.pem"
    )
    client = FursProductionClient(
        timeout_seconds=int(furs.get("production_timeout_seconds", 15)),
        ca_bundle_path=ca_path,
    )
    echoed = client.echo(bundle, "mikrofix-production-preflight")
    if echoed != "mikrofix-production-preflight":
        raise RuntimeError("FURS produkcijski ECHO se ni ujemal.")
    furs.update(
        {
            "enabled": True,
            "environment": "PRODUCTION",
            "certificate_path": str(cert_path),
            "production_activation_confirmed": True,
        }
    )
    raw["environment"] = "PRODUCTION"
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)
    print("USPEH: produkcijski ECHO je potrjen in FURS produkcija je vključena.")
    print(f"Potrdilo velja do: {bundle.valid_until_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
