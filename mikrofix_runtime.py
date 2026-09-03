"""Skupne nastavitve in podatkovne poti za MikroFIX v30-dev.

Privzeti način je vedno TEST. Produkcijski FURS ostane izključen, dokler ni
izrecno omogočen v nadzorovanem postopku.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APP_VERSION = "30.0-dev5"
SETTINGS_SCHEMA_VERSION = 1

DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": SETTINGS_SCHEMA_VERSION,
    "app_version": APP_VERSION,
    "environment": "TEST",
    "database": {
        "backend": "sqlite",
        "data_dir": "MikroFixData_V30_DEV",
        "server_url": "",
    },
    "business": {
        "legal_name": "MikroFix, popravilo elektronskih in telekomunikacijskih naprav, Ristoski Daniel s.p.",
        "tax_number": "84514841",
        "registration_number": "7565178000",
        "iban": "SI56040000283154872",
        "street": "Paradiž 73",
        "city": "2282 Cirkulane",
        "premises": ["MFD", "MFT"],
        "active_premise": "MFD",
        "electronic_device": "PIS",
        "operator_code": "DR",
        "operator_tax_number": "84514841",
        "numbering_structure": "B",
    },
    "tax": {
        "mode": "NON_VAT",
        "default_vat_rate": 0.0,
        "effective_from": "2026-08-19",
        "invoice_note": "Oproščeno po prvem odstavku 94. člena ZDDV-1.",
    },
    "furs": {
        "enabled": False,
        "environment": "TEST",
        "certificate_path": "",
        "test_console_enabled": True,
        "test_certificate_path": "",
        "test_ca_bundle_path": "certs/furs-test-ca-bundle.pem",
        "test_business_premise_id": "TESTMFD",
        "test_electronic_device_id": "PIS",
        "test_timeout_seconds": 15,
        "production_requires_explicit_confirmation": True,
    },
    "printer": {
        "model": "Epson TM-T20II (M267D)",
        "paper_width_mm": 80,
        "connection": "USB",
        "dpi": 203,
        "windows_printer_name": "",
    },
    "backup": {
        "enabled": False,
        "target_dir": "",
        "retention_days": 30,
        "max_age_hours": 24,
        "require_distinct_volume": True,
        "passphrase_configured": False,
    },
    "warranty": {
        "installed_parts_months": 3,
        "refurbished_devices_months": 12,
        "replacement_part_restarts_warranty": True,
        "refurbished_battery_defects_months": 12,
        "normal_battery_capacity_wear_excluded": True,
        "customer_supplied_part_warranty": "NONE_PART_ONLY",
        "statutory_rights_remain_unaffected": True,
        "used_goods_conformity_shortening": "SEPARATE_SIGNED_AGREEMENT_12_MONTHS",
    },
    "service_policy": {
        "personal_data_gate": "MICRODETECT_FIRST",
        "diagnostic_fee_eur": 30.0,
        "diagnostic_fee_credited_if_repaired": True,
        "price_overrun": "EXPLICIT_APPROVAL_ONLY",
        "special_order_deposit": "NONE_AT_LAUNCH_REVIEW_HIGH_RISK",
        "payment_methods": ["CASH", "BANK_TRANSFER"],
        "replace_seal_on_every_opening": True,
        "water_resistance": "NO_GUARANTEE_WITHOUT_PRESSURE_TEST",
        "microdetect_unavailable": "DOCUMENTED_EXCEPTION",
        "start_within_withdrawal_period": "EXPLICIT_REQUEST",
        "customer_document_delivery": "ONE_SIGNATURE_PAGE_PLUS_PDF",
        "signature_device": "A4_PAPER_WET_SIGNATURE",
        "service_photo_retention_years": 5,
        "no_email_delivery": "SMS_IMMUTABLE_DOWNLOADABLE_PDF",
        "unclaimed_free_days": 60,
        "unclaimed_storage_eur_per_day": 1.0,
        "unclaimed_never_transfers_ownership_automatically": True,
    },
    "used_device_policy": {
        "personal_data_gate": "MICRODETECT_FIRST",
        "device_identity_fields": "AUTO_READ_ONLY_FROM_MICRODETECT",
        "seller_identification": "OFFICIAL_ID",
        "ownership_basis": "SIGNED_DECLARATION",
        "imei_check_required": False,
        "additional_acquisition_evidence": "RECOMMENDED_WHEN_AVAILABLE",
        "preferred_payout_method": "BANK_TRANSFER",
        "cash_payout": "ALLOWED_UP_TO_LIMIT",
        "cash_payout_max_eur": 420.0,
        "cash_voucher_required": True,
        "cash_split_prohibited": True,
        "bank_transfer_payout": True,
        "customer_facing_condition": "FREE_TEXT",
        "internal_test_checklist_required": True,
        "statutory_conformity_months_if_separately_signed": 12,
    },
    "pricing": {
        "mode": "MANUAL_REVIEW",
        "l0_eur": 22.0,
        "legacy_flat_margin_enabled": False,
        "t1_hour_eur": 30.0,
        "pickup_return_eur": 30.0,
        "pickup_return_mode": "ORGANIZED_ROUTE",
        "pickup_return_max_extra_km_total": 12,
        "dedicated_pickup_return_eur": 30.0,
        "dedicated_pickup_return_public_fixed": False,
        "t1_review_rule": "FIRST_FULL_QUARTER_AFTER_PRODUCTION_START",
        "onsite_first_hour_eur": 90.0,
        "bundle_discount_eur": 5.0,
        "competitive_premiums": {
            "screen_pickup_eur": 35.0,
            "screen_onsite_eur": 80.0,
            "battery_pickup_eur": 15.0,
            "battery_onsite_eur": 60.0,
        },
        "categories": {
            "LCD / In-Cell": {"target_margin": 0.35, "hard_margin": 0.15, "min_contribution_eur": 15.0, "consumables_eur": 5.0, "warranty_reserve": 0.05},
            "Soft OLED": {"target_margin": 0.25, "hard_margin": 0.15, "min_contribution_eur": 15.0, "consumables_eur": 5.0, "warranty_reserve": 0.05},
            "Refurbished original": {"target_margin": 0.25, "hard_margin": 0.15, "min_contribution_eur": 20.0, "consumables_eur": 5.0, "warranty_reserve": 0.05},
            "Battery DIAGNOSTIC": {"target_margin": 0.25, "hard_margin": 0.15, "min_contribution_eur": 10.0, "consumables_eur": 5.0, "warranty_reserve": 0.08},
            "Battery FixPremium": {"target_margin": 0.30, "hard_margin": 0.15, "min_contribution_eur": 10.0, "consumables_eur": 5.0, "warranty_reserve": 0.08},
        },
    },
    "supplier": {
        "fixshop": {
            "enabled": True,
            "auto_refresh": True,
            "refresh_hours": 24,
            "timeout_seconds": 12,
            "max_workers": 2,
            "allowed_hosts": ["fixshop.si"],
            "user_agent": "MikroFIX/30 DEV supplier-price-check",
        },
    },
    "weekly_report": {
        "enabled": False,
        "automatic_monday": False,
        "source_max_age_days": 8,
        "min_market_sources": 2,
        "room_pct": 0.20,
        "room_min_eur": 40.0,
        "room_max_eur": 150.0,
        "internal_refurb_labor_eur": 20.0,
        "general_material_eur": 1.25,
        "package_eur": 30.0,
        "haggle_pct": 0.10,
        "haggle_min_eur": 10.0,
        "haggle_max_eur": 50.0,
        "round_step_eur": 10.0,
        "timeout_seconds": 15,
        "allowed_hosts": [
            "swappie.com",
            "www.swappie.com",
            "refurbed.si",
            "www.refurbed.si",
            "fixshop.si",
            "www.fixshop.si"
        ],
        "user_agent": "MikroFIX/30 DEV weekly-price-check"
    },
}

DB_FILENAMES = {
    "service": "mikrofix.db",
    "office": "mikrofix_office.db",
    "inventory": "mikrofix_inventory.db",
    "auth": "mikrofix_auth.db",
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_settings(settings: dict[str, Any]) -> None:
    if settings.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        raise ValueError("Nepodprta različica nastavitev MikroFIX.")

    environment = str(settings.get("environment", "")).upper()
    if environment not in {"TEST", "PRODUCTION"}:
        raise ValueError("Okolje mora biti TEST ali PRODUCTION.")

    database = settings.get("database", {})
    if database.get("backend") not in {"sqlite", "postgresql"}:
        raise ValueError("Podprta podatkovna načina sta sqlite in postgresql.")

    business = settings.get("business", {})
    premises = business.get("premises") or []
    if business.get("active_premise") not in premises:
        raise ValueError("Aktivni poslovni prostor mora biti naveden med prostori.")
    if not business.get("electronic_device"):
        raise ValueError("Manjka oznaka elektronske naprave.")
    if not business.get("operator_code"):
        raise ValueError("Manjka oznaka operaterja.")
    operator_tax_number = str(business.get("operator_tax_number") or "")
    if len(operator_tax_number) != 8 or not operator_tax_number.isdigit():
        raise ValueError("Davčna številka operaterja mora imeti 8 številk.")
    if business.get("numbering_structure") not in {"B", "C"}:
        raise ValueError("Struktura številčenja mora biti B ali C.")
    tax_number = str(business.get("tax_number") or "")
    if tax_number and (len(tax_number) != 8 or not tax_number.isdigit()):
        raise ValueError("Davčna številka podjetja mora imeti 8 številk.")

    tax = settings.get("tax", {})
    if tax.get("mode") not in {"NON_VAT", "VAT"}:
        raise ValueError("Davčni način mora biti NON_VAT ali VAT.")

    warranty = settings.get("warranty", {})
    if int(warranty.get("installed_parts_months", 0)) not in range(1, 61):
        raise ValueError("Garancija na vgrajene dele mora biti določena v mesecih.")
    if int(warranty.get("refurbished_devices_months", 0)) not in range(1, 61):
        raise ValueError("Garancija na obnovljene naprave mora biti določena v mesecih.")
    if not warranty.get("replacement_part_restarts_warranty"):
        raise ValueError("Za zamenjani del mora začeti teči nov garancijski rok.")
    if not warranty.get("statutory_rights_remain_unaffected"):
        raise ValueError("Tržna garancija ne sme omejiti zakonskih pravic potrošnika.")
    if warranty.get("used_goods_conformity_shortening") != (
        "SEPARATE_SIGNED_AGREEMENT_12_MONTHS"
    ):
        raise ValueError(
            "Skrajšanje zakonskega roka za rabljeno blago velja samo z ločenim podpisanim dogovorom."
        )

    service_policy = settings.get("service_policy", {})
    if service_policy.get("personal_data_gate") != "MICRODETECT_FIRST":
        raise ValueError("Osebni podatki se smejo vnesti šele po uspešnem MicroDETECT.")
    if float(service_policy.get("diagnostic_fee_eur", 0)) < 0:
        raise ValueError("Cena diagnostike ne sme biti negativna.")
    if service_policy.get("price_overrun") != "EXPLICIT_APPROVAL_ONLY":
        raise ValueError("Preseganje ponudbe zahteva izrecno odobritev stranke.")
    if service_policy.get("water_resistance") != "NO_GUARANTEE_WITHOUT_PRESSURE_TEST":
        raise ValueError("Vodoodpornosti brez tlačnega testa ni dovoljeno zagotavljati.")
    if int(service_policy.get("unclaimed_free_days", 0)) < 1:
        raise ValueError("Brezplačna hramba neprevzete naprave mora biti določena.")
    if not service_policy.get("unclaimed_never_transfers_ownership_automatically"):
        raise ValueError("Neprevzeta naprava ne sme samodejno postati last MikroFIX-a.")
    if service_policy.get("signature_device") != "A4_PAPER_WET_SIGNATURE":
        raise ValueError("Pogodbe se natisnejo na A4 in podpišejo lastnoročno.")
    if int(service_policy.get("service_photo_retention_years", 0)) != 5:
        raise ValueError("Servisne fotografije imajo potrjen petletni rok hrambe.")

    used_device_policy = settings.get("used_device_policy", {})
    if used_device_policy.get("personal_data_gate") != "MICRODETECT_FIRST":
        raise ValueError("Odkup se mora začeti z uspešnim MicroDETECT, pred podatki prodajalca.")
    if used_device_policy.get("device_identity_fields") != (
        "AUTO_READ_ONLY_FROM_MICRODETECT"
    ):
        raise ValueError("IMEI in serijska številka odkupa morata priti neposredno iz MicroDETECT.")
    if used_device_policy.get("imei_check_required"):
        raise ValueError("Zunanje komercialno IMEI preverjanje pri začetnem odkupu ni obvezno.")
    if used_device_policy.get("preferred_payout_method") != "BANK_TRANSFER":
        raise ValueError("Privzeti način izplačila odkupa mora biti bančno nakazilo.")
    if used_device_policy.get("cash_payout") != "ALLOWED_UP_TO_LIMIT":
        raise ValueError("Gotovinsko izplačilo odkupa mora uporabljati zaklenjeno mejo.")
    if float(used_device_policy.get("cash_payout_max_eur", 0)) != 420.0:
        raise ValueError("Najvišji gotovinski odkup mora biti 420,00 EUR.")
    if not used_device_policy.get("cash_voucher_required"):
        raise ValueError("Gotovinski odkup mora ustvariti blagajniški izdatek.")
    if not used_device_policy.get("cash_split_prohibited"):
        raise ValueError("Enega odkupa ni dovoljeno razdeliti na več gotovinskih izplačil.")
    if int(used_device_policy.get("statutory_conformity_months_if_separately_signed", 0)) != 12:
        raise ValueError("Dogovorjeni rok za neskladnost rabljenega blaga mora biti 12 mesecev.")

    pricing = settings.get("pricing", {})
    organized_pickup = float(pricing.get("pickup_return_eur", 0))
    dedicated_pickup = float(pricing.get("dedicated_pickup_return_eur", 0))
    if organized_pickup < 0 or dedicated_pickup < organized_pickup:
        raise ValueError(
            "Namenski prevzem ne sme biti cenejši od organiziranega prevzema."
        )
    if pricing.get("pickup_return_mode") != "ORGANIZED_ROUTE":
        raise ValueError("Samodejna logistična cena mora veljati za organizirano traso.")
    if int(pricing.get("pickup_return_max_extra_km_total", 0)) not in range(1, 101):
        raise ValueError("Meja dodatnih kilometrov organizirane trase ni veljavna.")
    if pricing.get("t1_review_rule") != "FIRST_FULL_QUARTER_AFTER_PRODUCTION_START":
        raise ValueError("T1 mora biti pregledan po prvem polnem produkcijskem četrtletju.")

    furs = settings.get("furs", {})
    if furs.get("environment") not in {"TEST", "PRODUCTION"}:
        raise ValueError("FURS okolje mora biti TEST ali PRODUCTION.")
    if int(furs.get("test_timeout_seconds", 15)) not in range(3, 61):
        raise ValueError("Čas FURS TEST povezave mora biti med 3 in 60 sekund.")
    for key, label in (
        ("test_business_premise_id", "Testni poslovni prostor"),
        ("test_electronic_device_id", "Testna elektronska naprava"),
    ):
        identifier = str(furs.get(key) or "")
        if not (1 <= len(identifier) <= 20 and identifier.isascii() and identifier.isalnum()):
            raise ValueError(f"{label} mora vsebovati 1–20 črk ali številk brez presledkov.")
    if environment == "TEST" and furs.get("environment") == "PRODUCTION":
        raise ValueError("TEST aplikacija ne sme uporabljati produkcijskega FURS okolja.")
    if (
        furs.get("enabled")
        and furs.get("environment") == "PRODUCTION"
        and os.environ.get("MIKROFIX_ALLOW_FURS_PRODUCTION") != "YES"
    ):
        raise ValueError(
            "Produkcijski FURS je varnostno zaklenjen. Aktivacija zahteva "
            "poseben potrjen produkcijski postopek."
        )

    supplier = settings.get("supplier", {}).get("fixshop", {})
    if int(supplier.get("refresh_hours", 0)) < 1:
        raise ValueError("Osveževanje dobavitelja mora biti najmanj 1 ura.")
    if int(supplier.get("max_workers", 0)) not in {1, 2, 3, 4}:
        raise ValueError("Število sočasnih dobaviteljskih zahtev mora biti med 1 in 4.")
    if not supplier.get("allowed_hosts"):
        raise ValueError("Manjka dovoljeni gostitelj dobavitelja.")

    weekly_report = settings.get("weekly_report", {})
    if int(weekly_report.get("source_max_age_days", 0)) not in range(1, 32):
        raise ValueError("Starost virov tedenskega poročila mora biti med 1 in 31 dni.")
    if int(weekly_report.get("min_market_sources", 0)) not in range(2, 6):
        raise ValueError("Tedensko poročilo zahteva od 2 do 5 tržnih virov.")
    if float(weekly_report.get("room_min_eur", 0)) > float(weekly_report.get("room_max_eur", 0)):
        raise ValueError("Napačen razpon varnostnega prostora tedenskega poročila.")
    if float(weekly_report.get("haggle_min_eur", 0)) > float(weekly_report.get("haggle_max_eur", 0)):
        raise ValueError("Napačen razpon pogajalske rezerve tedenskega poročila.")
    if int(weekly_report.get("timeout_seconds", 0)) not in range(3, 61):
        raise ValueError("Čas osveževanja tedenskega poročila mora biti med 3 in 60 sekund.")


@dataclass(frozen=True)
class RuntimeContext:
    app_dir: Path
    settings_path: Path
    settings: dict[str, Any]
    data_dir: Path
    db_dir: Path
    service_data_dir: Path
    office_data_dir: Path

    @property
    def is_test(self) -> bool:
        return self.settings["environment"] == "TEST"

    @property
    def version_label(self) -> str:
        suffix = " TEST" if self.is_test else ""
        return f"v{APP_VERSION}{suffix}"

    @property
    def tax_mode(self) -> str:
        return self.settings["tax"]["mode"]

    @property
    def default_vat_rate(self) -> float:
        if self.tax_mode == "NON_VAT":
            return 0.0
        return float(self.settings["tax"].get("default_vat_rate", 22.0))

    @property
    def tax_invoice_note(self) -> str:
        return str(self.settings["tax"].get("invoice_note", "")).strip()

    @property
    def active_premise(self) -> str:
        return self.settings["business"]["active_premise"]

    @property
    def electronic_device(self) -> str:
        return self.settings["business"]["electronic_device"]

    @property
    def operator_code(self) -> str:
        return self.settings["business"]["operator_code"]

    def db_path(self, name: str) -> Path:
        try:
            filename = DB_FILENAMES[name]
        except KeyError as exc:
            raise KeyError(f"Neznana MikroFIX baza: {name}") from exc
        return self.db_dir / filename

    def connect_sqlite(
        self,
        name: str,
        *,
        foreign_keys: bool = False,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        backend = self.settings["database"]["backend"]
        if backend != "sqlite":
            raise RuntimeError(
                "Ta razvojna izdaja ima pripravljen podatkovni vmesnik, "
                "PostgreSQL povezava za Raspberry Pi 5 pa še ni vključena."
            )

        path = self.db_path(name)
        if read_only:
            if not path.exists():
                raise FileNotFoundError(path)
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(path)

        if foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def load_runtime(app_dir: Path) -> RuntimeContext:
    app_dir = Path(app_dir).resolve()
    explicit_path = os.environ.get("MIKROFIX_CONFIG")
    settings_path = (
        Path(explicit_path).expanduser().resolve()
        if explicit_path
        else app_dir / "mikrofix_settings.json"
    )

    overrides: dict[str, Any] = {}
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("mikrofix_settings.json mora vsebovati JSON objekt.")
        overrides = loaded

    settings = _deep_merge(DEFAULT_SETTINGS, overrides)
    _validate_settings(settings)

    raw_data_dir = Path(
        os.path.expandvars(
            os.environ.get("MIKROFIX_DATA_DIR")
            or str(settings["database"]["data_dir"])
        )
    ).expanduser()
    data_dir = raw_data_dir if raw_data_dir.is_absolute() else app_dir / raw_data_dir
    data_dir = data_dir.resolve()
    db_dir = data_dir / "db"
    service_data_dir = data_dir / "service"
    office_data_dir = data_dir / "office"

    for directory in (
        data_dir,
        db_dir,
        service_data_dir / "photos",
        service_data_dir / "pdf",
        service_data_dir / "backups",
        office_data_dir / "pdf",
        office_data_dir / "weekly_report",
        office_data_dir / "weekly_reports",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    return RuntimeContext(
        app_dir=app_dir,
        settings_path=settings_path,
        settings=settings,
        data_dir=data_dir,
        db_dir=db_dir,
        service_data_dir=service_data_dir,
        office_data_dir=office_data_dir,
    )
