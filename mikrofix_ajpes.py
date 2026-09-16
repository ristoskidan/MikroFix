"""Majhen odjemalec uradnega AJPES restPrsInfo za vnos partnerja.

Geslo ni del MikroFix nastavitev ali baze. Produkcijski dostop se poda prek
okoljskih spremenljivk oziroma prihodnje sistemske shrambe skrivnosti.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class AjpesError(RuntimeError):
    pass


@dataclass(frozen=True)
class AjpesPartner:
    name: str
    address: str
    post_city: str
    tax_number: str
    registration_number: str


def _text(data: dict, *names: str) -> str:
    folded = {str(key).casefold(): value for key, value in data.items()}
    for name in names:
        value = folded.get(name.casefold())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _credentials() -> tuple[str, str, str]:
    username = os.environ.get("MIKROFIX_AJPES_USERNAME", "").strip()
    password = os.environ.get("MIKROFIX_AJPES_PASSWORD", "")
    schema = os.environ.get("MIKROFIX_AJPES_SCHEMA", "PRS_SN_E").strip()
    if not username or not password or not schema:
        raise AjpesError(
            "AJPES restPrsInfo še nima poverilnic. Potrebni sta registrirano "
            "uporabniško ime/geslo AJPES in naročena PRS shema; do takrat podatke "
            "vnesi ročno po preverjanju v ePRS."
        )
    return username, password, schema


def find_partner_by_tax_number(tax_number: str, *, test_mode: bool, timeout: int = 15) -> AjpesPartner:
    tax = str(tax_number or "").strip().upper()
    if tax.startswith("SI"):
        tax = tax[2:]
    if len(tax) != 8 or not tax.isdigit():
        raise AjpesError("Davčna številka za AJPES mora imeti 8 številk.")
    username, password, schema = _credentials()
    base = "https://wwwa.ajpes.si" if test_mode else "https://www.ajpes.si"
    payload = {
        "ident": {"uporabnik": username, "geslo": password, "shema": schema},
        "davcna": tax,
        "najdiPodruznice": False,
        "maxRecords": 3,
    }
    request = urllib.request.Request(
        f"{base}/restPrsInfo/find",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(3, int(timeout))) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AjpesError(f"AJPES trenutno ni dosegljiv ali odgovor ni veljaven: {exc}") from exc
    if int(result.get("status") or 0) != 2000:
        raise AjpesError(str(result.get("message") or "AJPES poizvedba ni uspela."))
    formatted = ((result.get("payload") or {}).get("formatted") or {})
    rows = formatted.get("prsData") or formatted.get("PrsData") or []
    if isinstance(rows, dict):
        rows = [rows]
    if len(rows) != 1:
        if not rows:
            raise AjpesError("AJPES za vneseno davčno številko ni vrnil aktivnega subjekta.")
        raise AjpesError("AJPES je vrnil več subjektov; izberi in preveri ga ročno v ePRS.")
    row = rows[0]
    returned_tax = _text(row, "davcna", "davcna_stevilka", "ds") or tax
    if returned_tax.upper().removeprefix("SI") != tax:
        raise AjpesError("Vrnjeni subjekt nima iskane davčne številke.")
    partner = AjpesPartner(
        name=_text(row, "popolno_ime", "Popolno_ime", "naziv"),
        address=_text(row, "ulica", "naslov", "poslovni_naslov"),
        post_city=_text(row, "posta", "postna_stevilka_in_kraj"),
        tax_number=tax,
        registration_number=_text(row, "maticna", "Maticna", "maticna_stevilka"),
    )
    if not partner.name or not partner.registration_number:
        raise AjpesError("AJPES odgovor nima obveznega naziva ali matične številke.")
    return partner
