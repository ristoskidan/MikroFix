from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from mikrofix_auth import AuthStore
from mikrofix_runtime import load_runtime
from mikrofix_policy import (
    WARRANTY_SCOPE_INSTALLED_PARTS,
    WARRANTY_SCOPE_NONE,
    add_calendar_months,
    service_parts_warranty,
)
from mikrofix_receipt import generate_test_receipt
from mikrofix_supplier import SupplierCatalogStore, parse_fixshop_product_html
from mikrofix_weekly_report import (
    WeeklyReportError,
    calculate_entry,
    generate_pdf as generate_weekly_pdf,
    parse_fixshop_price as parse_weekly_fixshop_price,
    parse_refurbed_price,
    parse_swappie_prices,
    report_for_week,
    save_inputs as save_weekly_inputs,
)
from mikrofix_device import (
    DeviceDetectionError,
    classify_pmd_failure,
    _read_usb_iphone_async,
    extract_device_identity,
    parse_usbmux_devices,
    select_single_usb_device,
    storage_capacity_label,
)
from mikrofix_furs import (
    FursTestClient,
    FursTestError,
    FursTestStore,
    BusinessPremiseResponse,
    InvoiceResponse,
    build_one_euro_test_invoice,
    build_test_business_premise,
    calculate_zoi,
    create_jws,
    load_test_certificate,
    readiness_report,
)
from mikrofix_workflows import (
    WorkflowError,
    complete_buyback,
    complete_service_order,
    create_buyback,
    create_service_order,
    ensure_workflow_schema,
    generate_buyback_pdf,
    generate_service_order_pdf,
    get_buyback,
    get_service_order,
    latest_open_service_order,
    validate_buyback,
)
from mikrofix_backup import (
    backup_sqlite_file,
    verify_restore_roundtrip,
    verify_sqlite_file,
)
from mikrofix_invoice_bridge import (
    load_service_invoice_seed,
    mark_service_invoiced,
)


def sample_service_order() -> dict:
    return {
        "customer_name": "TEST Stranka",
        "customer_address": "Testna 1",
        "customer_phone": "040000000",
        "customer_email": "test@example.invalid",
        "device_model": "iPhone 11 Pro",
        "device_serial": "TESTSERIAL001",
        "device_imei": "000000000000001",
        "device_os": "18.0 TEST",
        "device_storage": "256 GB",
        "device_type": "APPLE_MOBILE",
        "identity_method": "MICRODETECT",
        "customer_request": "Testna menjava zaslona.",
        "intake_condition": "Testna praska na okvirju.",
        "accessories": "brez opreme",
        "quote_eur": "120,00",
        "logistics_eur": "20,00",
        "expected_due": "1-2 delovna dneva",
        "contract_context": "MFT_TEREN",
        "immediate_start_requested": True,
        "withdrawal_loss_ack": True,
        "delivery_channel": "EMAIL_PDF",
        "microdetect_confirmed": True,
        "microdetect_at": "2026-08-27 12:00:00",
        "operator_code": "DR",
        "notes": "Samo avtomatski test.",
    }


def sample_buyback() -> dict:
    return {
        "seller_name": "TEST Prodajalec",
        "seller_tax_number": "12345678",
        "seller_birth_date": "1990-01-01",
        "seller_address": "Testna 2",
        "seller_contact": "040000001",
        "seller_iban": "SI56000000000000000",
        "identity_checked": True,
        "device_model": "iPhone 11 Pro",
        "device_serial": "TESTSERIAL002",
        "device_imei": "000000000000002",
        "device_os": "18.0 TEST",
        "device_storage": "256 GB",
        "color_capacity": "testna barva",
        "accessories": "naprava",
        "condition_text": "Testna naprava deluje.",
        "acquisition_method": "KUPLJENA_RABLJENA_FIZICNA_OSEBA",
        "acquisition_details": "Testna pogodba.",
        "evidence_type": "PREJŠNJA_POGODBA",
        "imei_check_provider": "TESTNI SIMULATOR",
        "imei_check_result": "TEST_CLEAR",
        "imei_check_reference": "TEST-REF-001",
        "imei_checked_at": "2026-08-27 12:00:00",
        "account_removed": True,
        "activation_lock_removed": True,
        "mdm_clear": True,
        "ownership_declaration": True,
        "privacy_notice_received": True,
        "risk_level": "GREEN",
        "risk_reason": "Samo avtomatski test.",
        "purchase_price": "250,00",
        "payout_method": "BANK_TRANSFER",
        "operator_code": "DR",
        "microdetect_confirmed": True,
        "microdetect_at": "2026-08-27 12:00:00",
        "notes": "Ni dejanski odkup.",
    }


class RuntimeTests(unittest.TestCase):
    def test_usb_launcher_can_keep_live_data_on_local_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw_app, tempfile.TemporaryDirectory() as raw_data:
            with patch.dict(os.environ, {"MIKROFIX_DATA_DIR": raw_data}):
                runtime = load_runtime(Path(raw_app))
            self.assertEqual(runtime.data_dir, Path(raw_data).resolve())

    def test_default_test_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = load_runtime(Path(raw_dir))
            self.assertTrue(runtime.is_test)
            self.assertEqual(runtime.tax_mode, "NON_VAT")
            self.assertEqual(runtime.default_vat_rate, 0.0)
            self.assertEqual(runtime.active_premise, "MFD")
            self.assertEqual(runtime.electronic_device, "PIS")
            self.assertEqual(runtime.operator_code, "DR")
            self.assertEqual(
                runtime.settings["printer"]["paper_width_mm"], 80
            )
            self.assertFalse(runtime.settings["furs"]["enabled"])
            self.assertTrue(runtime.settings["furs"]["test_console_enabled"])
            self.assertFalse(runtime.settings["weekly_report"]["automatic_monday"])
            self.assertEqual(runtime.settings["weekly_report"]["source_max_age_days"], 8)
            self.assertEqual(runtime.settings["business"]["tax_number"], "84514841")
            self.assertEqual(runtime.settings["business"]["numbering_structure"], "B")
            self.assertEqual(runtime.settings["pricing"]["pickup_return_eur"], 30.0)
            self.assertEqual(
                runtime.settings["pricing"]["dedicated_pickup_return_eur"], 30.0
            )
            self.assertFalse(
                runtime.settings["pricing"]["dedicated_pickup_return_public_fixed"]
            )
            self.assertEqual(runtime.settings["warranty"]["installed_parts_months"], 3)
            self.assertEqual(runtime.settings["warranty"]["refurbished_devices_months"], 12)
            self.assertTrue(
                runtime.settings["warranty"]["statutory_rights_remain_unaffected"]
            )
            self.assertTrue(
                runtime.settings["warranty"]["replacement_part_restarts_warranty"]
            )
            self.assertEqual(
                runtime.settings["warranty"]["used_goods_conformity_shortening"],
                "SEPARATE_SIGNED_AGREEMENT_12_MONTHS",
            )
            self.assertEqual(runtime.settings["service_policy"]["diagnostic_fee_eur"], 30.0)
            self.assertEqual(
                runtime.settings["service_policy"]["personal_data_gate"],
                "MICRODETECT_FIRST",
            )
            self.assertEqual(
                runtime.settings["service_policy"]["price_overrun"],
                "EXPLICIT_APPROVAL_ONLY",
            )
            self.assertEqual(runtime.settings["service_policy"]["unclaimed_free_days"], 60)
            self.assertFalse(runtime.settings["used_device_policy"]["imei_check_required"])
            self.assertEqual(
                runtime.settings["used_device_policy"]["preferred_payout_method"],
                "BANK_TRANSFER",
            )
            self.assertEqual(
                runtime.settings["used_device_policy"]["cash_payout_max_eur"],
                420.0,
            )
            self.assertEqual(
                runtime.settings["service_policy"]["service_photo_retention_years"],
                5,
            )
            self.assertEqual(
                runtime.settings["used_device_policy"]["personal_data_gate"],
                "MICRODETECT_FIRST",
            )
            self.assertEqual(
                runtime.settings["used_device_policy"]["device_identity_fields"],
                "AUTO_READ_ONLY_FROM_MICRODETECT",
            )
            connection = runtime.connect_sqlite("service")
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            connection.close()

    def test_calendar_month_warranty_rules(self) -> None:
        self.assertEqual(
            add_calendar_months(date(2026, 11, 30), 3),
            date(2027, 2, 28),
        )
        scope, months, valid_until = service_parts_warranty(
            date(2026, 8, 27), True, 3
        )
        self.assertEqual(scope, WARRANTY_SCOPE_INSTALLED_PARTS)
        self.assertEqual(months, 3)
        self.assertEqual(valid_until, "2026-11-27")
        self.assertEqual(
            service_parts_warranty(date(2026, 8, 27), False, 3),
            (WARRANTY_SCOPE_NONE, 0, ""),
        )

    def test_production_furs_requires_explicit_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            app_dir = Path(raw_dir)
            config_path = app_dir / "production.json"
            config_path.write_text(json.dumps({
                "environment": "PRODUCTION",
                "furs": {"enabled": True, "environment": "PRODUCTION"},
            }), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"MIKROFIX_CONFIG": str(config_path)},
                clear=False,
            ):
                os.environ.pop("MIKROFIX_ALLOW_FURS_PRODUCTION", None)
                with self.assertRaisesRegex(ValueError, "varnostno zaklenjen"):
                    load_runtime(app_dir)
                os.environ["MIKROFIX_ALLOW_FURS_PRODUCTION"] = "YES"
                runtime = load_runtime(app_dir)
                self.assertFalse(runtime.is_test)


class FursTestIntegrationTests(unittest.TestCase):
    @staticmethod
    def _certificate_file(
        directory: str,
        *,
        test_certificate: bool = True,
        issuer_cn: str | None = None,
        ou_marker: str | None = None,
    ):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization.pkcs12 import (
            serialize_key_and_certificates,
        )
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ou_marker = ou_marker or ("DavPotRacTEST" if test_certificate else "DavPotRac")
        issuer_cn = issuer_cn or ("Tax CA Test" if test_certificate else "TaxCA")
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "SI"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "99999862"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou_marker),
            x509.NameAttribute(NameOID.COMMON_NAME, "Testna oseba 1"),
        ])
        issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "SI"),
            x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn),
        ])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        p12 = serialize_key_and_certificates(
            b"mikrofix-test",
            key,
            certificate,
            None,
            serialization.BestAvailableEncryption(b"test-geslo"),
        )
        path = Path(directory) / ("test.p12" if test_certificate else "production.p12")
        path.write_bytes(p12)
        return path, key, certificate

    def test_test_certificate_zoi_and_jws(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path, _key, _certificate = self._certificate_file(raw_dir)
            bundle = load_test_certificate(path, "test-geslo")
            self.assertEqual(bundle.tax_number, 99999862)
            issued = datetime(2026, 8, 26, 12, 34, 56)
            zoi_a = calculate_zoi(
                bundle,
                issue_datetime=issued,
                invoice_number="T1",
                business_premise_id="MFD",
                electronic_device_id="1",
                invoice_amount="1.00",
            )
            zoi_b = calculate_zoi(
                bundle,
                issue_datetime=issued,
                invoice_number="T1",
                business_premise_id="MFD",
                electronic_device_id="1",
                invoice_amount="1.00",
            )
            self.assertEqual(zoi_a, zoi_b)
            self.assertRegex(zoi_a, r"^[0-9a-f]{32}$")

            payload = build_one_euro_test_invoice(
                bundle,
                business_premise_id="MFD",
                electronic_device_id="1",
                issue_datetime=issued,
                invoice_number="T1",
            )
            invoice = payload["InvoiceRequest"]["Invoice"]
            self.assertEqual(invoice["TaxNumber"], 99999862)
            self.assertEqual(
                invoice["TaxesPerSeller"], [{"ExemptVATTaxableAmount": 1.0}]
            )
            self.assertEqual(invoice["ProtectedID"], zoi_a)
            token = create_jws(payload, bundle)
            self.assertEqual(len(token.split(".")), 3)
            header = json.loads(
                base64.urlsafe_b64decode(token.split(".")[0] + "==").decode("utf-8")
            )
            self.assertEqual(header["cty"], "application/json")
            self.assertEqual(header["typ"], "JOSE")

            premise_payload = build_test_business_premise(
                bundle,
                business_premise_id="TESTMFD",
                software_supplier_tax_number="84514841",
                validity_date="2026-08-31",
            )
            premise = premise_payload["BusinessPremiseRequest"]["BusinessPremise"]
            self.assertEqual(premise["TaxNumber"], 99999862)
            self.assertEqual(premise["BusinessPremiseID"], "TESTMFD")
            self.assertEqual(premise["BPIdentifier"], {"PremiseType": "C"})
            self.assertEqual(
                premise["SoftwareSupplier"], [{"TaxNumber": 84514841}]
            )

    def test_production_certificate_is_rejected_by_test_console(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path, _key, _certificate = self._certificate_file(
                raw_dir, test_certificate=False
            )
            with self.assertRaises(FursTestError) as caught:
                load_test_certificate(path, "test-geslo")
            self.assertEqual(caught.exception.code, "PRODUCTION_CERTIFICATE_REJECTED")

    def test_current_edb_ca_test_certificate_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path, _key, _certificate = self._certificate_file(
                raw_dir,
                issuer_cn="EDB-CA Test",
                ou_marker="FURS test uporabnik",
            )
            bundle = load_test_certificate(path, "test-geslo")
            self.assertEqual(bundle.tax_number, 99999862)

    def test_echo_is_locked_to_official_test_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path, _key, _certificate = self._certificate_file(raw_dir)
            bundle = load_test_certificate(path, "test-geslo")
            calls = []
            test_case = self

            class FakeResponse:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"EchoResponse": "mikrofix"}

            class FakeSession:
                trust_env = True

                def post(self, url, **kwargs):
                    self_cert = kwargs["cert"]
                    test_case.assertTrue(Path(self_cert[0]).is_file())
                    test_case.assertTrue(Path(self_cert[1]).is_file())
                    calls.append((url, kwargs["json"]))
                    return FakeResponse()

                def close(self):
                    return None

            client = FursTestClient(session_factory=FakeSession)
            self.assertEqual(client.echo(bundle), "mikrofix")
            self.assertEqual(
                calls,
                [("https://blagajne-test.fu.gov.si:9002/v1/cash_registers/echo",
                  {"EchoRequest": "mikrofix"})],
            )

    def test_test_outbox_keeps_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            path, _key, _certificate = self._certificate_file(raw_dir)
            bundle = load_test_certificate(path, "test-geslo")
            payload = build_one_euro_test_invoice(
                bundle,
                business_premise_id="MFD",
                electronic_device_id="1",
                invoice_number="TQUEUE1",
            )
            store = FursTestStore(Path(raw_dir) / "office.db")
            store.init()
            row_id = store.enqueue(payload)
            self.assertEqual(store.recent()[0]["status"], "PENDING")
            response = InvoiceResponse(
                ok=True,
                eor="00000000-0000-0000-0000-000000000001",
                error_code="",
                error_message="",
                message_id=payload["InvoiceRequest"]["Header"]["MessageID"],
                response_time="2026-08-26T12:00:00",
                raw_payload={},
            )
            store.mark_sent(row_id, response, {"token": "test"})
            row = store.recent()[0]
            self.assertEqual(row["status"], "SENT")
            self.assertEqual(row["attempt_count"], 1)
            self.assertEqual(row["eor"], response.eor)

            premise_response = BusinessPremiseResponse(
                ok=True,
                error_code="",
                error_message="",
                message_id="00000000-0000-0000-0000-000000000002",
                response_time="2026-08-31T12:00:00",
                raw_payload={},
            )
            store.mark_premise_registered(
                test_tax_number=bundle.tax_number,
                premise_id="TESTMFD",
                device_id="PIS",
                result=premise_response,
                raw_response={"token": "test-premise"},
            )
            self.assertTrue(
                store.is_premise_registered(bundle.tax_number, "TESTMFD")
            )
            self.assertFalse(
                store.is_premise_registered(bundle.tax_number, "DRUGTEST")
            )

    def test_readiness_never_enables_production(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = load_runtime(Path(raw_dir))
            report = readiness_report(runtime.settings, raw_dir)
            by_label = {row["label"]: row for row in report}
            self.assertEqual(by_label["Program"]["status"], "OK")
            self.assertEqual(by_label["Produkcija"]["status"], "OK")
            self.assertEqual(by_label["Testno potrdilo"]["status"], "BLOCK")


class AuthTests(unittest.TestCase):
    def test_daniel_and_future_users(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            store = AuthStore(Path(raw_dir) / "auth.db")
            store.init()
            users = store.list_users()
            self.assertEqual([user["username"] for user in users], ["daniel"])
            self.assertEqual(users[0]["role"], "ADMIN")

            store.set_pin("220067")
            self.assertTrue(store.verify_pin("220067").ok)
            self.assertFalse(store.verify_pin("220068").ok)

            user_id = store.create_user(
                "servis2", "Serviser 2", "SERVICE", pin="135790"
            )
            self.assertGreater(user_id, 0)
            self.assertTrue(store.verify_pin("135790", "servis2").ok)
            store.set_pin(
                "567804",
                "servis2",
                event_type="PIN_RESET_BY_ADMIN",
                changed_by="daniel",
            )
            self.assertTrue(store.verify_pin("567804", "servis2").ok)
            store.set_user_active("servis2", False)
            self.assertFalse(store.verify_pin("135790", "servis2").ok)
            with self.assertRaisesRegex(ValueError, "Daniel"):
                store.set_user_active("daniel", False)

            connection = store.connect()
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0]
            reset_actor = connection.execute(
                "SELECT username FROM audit_events "
                "WHERE event_type='PIN_RESET_BY_ADMIN' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            connection.close()
            self.assertGreaterEqual(audit_count, 6)
            self.assertEqual(reset_actor, "daniel")

    def test_session_handoff_is_short_lived_scoped_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            store = AuthStore(Path(raw_dir) / "auth.db")
            store.init()
            store.set_pin("220067")
            self.assertTrue(store.verify_pin("220067").ok)

            wrong_context_token = store.issue_session_handoff(
                "daniel", "OFFICE", ttl_seconds=60
            )
            self.assertIsNone(
                store.consume_session_handoff(wrong_context_token, "SERVICE")
            )
            accepted = store.consume_session_handoff(
                wrong_context_token, "OFFICE"
            )
            self.assertEqual(accepted["username"], "daniel")
            self.assertEqual(accepted["role"], "ADMIN")
            self.assertIsNone(
                store.consume_session_handoff(wrong_context_token, "OFFICE")
            )

            expired_token = store.issue_session_handoff(
                "daniel", "OFFICE", ttl_seconds=5
            )
            connection = store.connect()
            connection.execute(
                "UPDATE session_handoffs SET expires_at='2000-01-01 00:00:00' "
                "WHERE token_hash=?",
                (store._handoff_hash(expired_token),),
            )
            connection.commit()
            connection.close()
            self.assertIsNone(
                store.consume_session_handoff(expired_token, "OFFICE")
            )


class ReceiptTests(unittest.TestCase):
    def test_58mm_test_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            output = Path(raw_dir) / "receipt.pdf"
            generate_test_receipt(
                output,
                company={
                    "name": "MikroFIX",
                    "address": "Paradiž 73",
                    "phone": "+386 40 353 402",
                    "email": "mikrofixsi@gmail.com",
                },
                document={
                    "number": "IRA-TEST-1",
                    "type": "IZDANI RAČUN",
                    "date": "2026-08-25",
                    "partner": "Testna stranka",
                    "delivery": 0,
                },
                items=[{
                    "name": "Testna storitev",
                    "qty": 1,
                    "line_net": 22,
                    "line_vat": 0,
                }],
                premise="MFD",
                device="1",
                tax_note="DDV ni obračunan.",
            )
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1_000)


class MicroDetectTests(unittest.TestCase):
    def test_parses_current_and_legacy_usbmux_json(self) -> None:
        current = parse_usbmux_devices(
            '[{"serial":"UDID1","connection_type":"USB"}]'
        )
        legacy = parse_usbmux_devices(
            '{"DeviceList":[{"SerialNumber":"UDID2","ConnectionType":"USB"}]}'
        )
        self.assertEqual(len(current), 1)
        self.assertEqual(len(legacy), 1)

    def test_ignores_wifi_and_deduplicates_usb(self) -> None:
        devices = parse_usbmux_devices(json.dumps([
            {"serial": "USB1", "connection_type": "USB"},
            {"serial": "USB1", "connection_type": "USB"},
            {"serial": "WIFI1", "connection_type": "Network"},
        ]))
        self.assertEqual(len(devices), 1)

    def test_selects_exactly_one_physical_usb_device(self) -> None:
        class MuxDevice:
            def __init__(self, serial: str, connection_type: str) -> None:
                self.serial = serial
                self.connection_type = connection_type

        usb = MuxDevice("USB-1", "USB")
        selected = select_single_usb_device([
            usb,
            MuxDevice("USB-1", "USB"),
            MuxDevice("WIFI-1", "Network"),
        ])
        self.assertIs(selected, usb)

    def test_rejects_multiple_physical_usb_devices(self) -> None:
        with self.assertRaises(DeviceDetectionError) as caught:
            select_single_usb_device([
                {"serial": "USB1", "connection_type": "USB"},
                {"serial": "USB2", "connection_type": "USB"},
            ])
        self.assertEqual(caught.exception.code, "MULTIPLE_DEVICES")

    def test_extracts_required_identity(self) -> None:
        identity = extract_device_identity({
            "MarketingModel": "iPhone 14 Pro Max",
            "ProductType": "iPhone15,3",
            "ProductVersion": "18.6",
            "DeviceName": "Danielov iPhone",
            "SerialNumber": "SERIAL123",
            "InternationalMobileEquipmentIdentity": "123456789012345",
        })
        self.assertEqual(identity["model"], "iPhone 14 Pro Max")
        self.assertEqual(identity["serial"], "SERIAL123")
        self.assertEqual(identity["imei"], "123456789012345")

    def test_reads_marketed_storage_from_lockdown_disk_usage(self) -> None:
        payload = {
            "SerialNumber": "SERIAL123",
            "ProductType": "iPhone15,3",
            "MicroDetectDiskUsage": {"TotalDiskCapacity": 255_900_000_000},
        }
        self.assertEqual(storage_capacity_label(payload), "256 GB")
        self.assertEqual(extract_device_identity(payload)["storage"], "256 GB")

    def test_rejects_identity_without_model(self) -> None:
        with self.assertRaises(DeviceDetectionError) as caught:
            extract_device_identity({"SerialNumber": "SERIAL123"})
        self.assertEqual(caught.exception.code, "MISSING_MODEL")

    def test_classifies_common_windows_failures(self) -> None:
        self.assertEqual(
            classify_pmd_failure(
                "No module named pymobiledevice3", stage="usbmux"
            ).code,
            "PMD_MISSING",
        )
        self.assertEqual(
            classify_pmd_failure(
                "PairingDialogResponsePendingError", stage="lockdown"
            ).code,
            "TRUST_REQUIRED",
        )
        self.assertEqual(
            classify_pmd_failure(
                "ConnectionFailedToUsbmuxdError", stage="usbmux"
            ).code,
            "USBMUX_UNAVAILABLE",
        )
        self.assertEqual(
            classify_pmd_failure("LockdownError: SessionActive", stage="lockdown").code,
            "SESSION_ACTIVE",
        )

    def test_direct_api_targets_selected_usb_and_closes_connection(self) -> None:
        class MuxDevice:
            serial = "USB-SELECTED"
            connection_type = "USB"

        class FakeLockdown:
            display_name = "iPhone 14 Pro Max"

            def __init__(self) -> None:
                self.closed = False

            async def validate_pairing(self) -> bool:
                raise AssertionError(
                    "create_using_usbmux je že vzpostavil sejo; ponovni klic bi povzročil SessionActive"
                )

            async def get_value(self, domain=None, key=None):
                if domain == "com.apple.disk_usage":
                    return {"TotalDiskCapacity": 255_900_000_000}
                return {
                    "DeviceClass": "iPhone",
                    "ProductType": "iPhone15,3",
                    "ProductVersion": "18.6",
                    "SerialNumber": "SERIAL123",
                }

            async def close(self) -> None:
                self.closed = True

        fake = FakeLockdown()
        target = {}

        async def list_devices():
            return [MuxDevice()]

        async def create_lockdown(**kwargs):
            target.update(kwargs)
            return fake

        result = asyncio.run(_read_usb_iphone_async(
            list_devices_func=list_devices,
            create_lockdown_func=create_lockdown,
        ))

        self.assertEqual(target["serial"], "USB-SELECTED")
        self.assertEqual(target["connection_type"], "USB")
        self.assertTrue(target["autopair"])
        self.assertEqual(target["pair_timeout"], 60)
        self.assertTrue(fake.closed)
        self.assertEqual(result["MarketingModel"], "iPhone 14 Pro Max")
        self.assertEqual(
            result["MicroDetectDiskUsage"]["TotalDiskCapacity"],
            255_900_000_000,
        )


class SupplierCatalogTests(unittest.TestCase):
    def test_parses_public_fixshop_product_metadata(self) -> None:
        page = parse_fixshop_product_html(
            '<meta property="og:title" content="Zaslon Soft OLED">'
            '<meta property="product:price:amount" content="59.49">'
            '<meta property="product:price:currency" content="EUR">'
            '<meta property="product:retailer_item_id" content="PHONE13-80">'
            '<script type="application/ld+json">'
            '{"offers":{"availability":"http://schema.org/InStock"}}'
            '</script>'
        )
        self.assertEqual(page.price, 59.49)
        self.assertEqual(page.availability, "IN_STOCK")
        self.assertEqual(page.sku, "PHONE13-80")

    def test_seed_price_math_and_local_stock_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            app_dir = Path(raw_dir)
            runtime = load_runtime(app_dir)
            inventory_db = runtime.db_path("inventory")
            connection = sqlite3.connect(inventory_db)
            connection.execute("""
                CREATE TABLE inventory_items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT NOT NULL UNIQUE,
                    article_name TEXT NOT NULL,
                    internal_code TEXT,
                    stock_qty INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    lot_serial TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.commit()
            connection.close()

            seed = app_dir / "seed.json"
            seed.write_text(json.dumps({
                "schemaVersion": 1,
                "supplier": "FixShop",
                "generatedAt": "2026-08-25T00:00:00+02:00",
                "products": [
                    {
                        "model": "iPhone 13",
                        "repairType": "screen",
                        "repairLabel": "Zaslon",
                        "quality": "Soft OLED",
                        "productName": "Zaslon Soft OLED za iPhone 13",
                        "sourceUrl": "https://fixshop.si/test-iphone-13/",
                        "publicPrice": 59.49,
                        "competitorPrice": 115,
                        "autoEligible": True,
                        "inventoryTier": "A",
                        "targetVehicle": 1,
                        "targetHome": 1,
                        "reorderPoint": 1,
                    },
                    {
                        "model": "iPhone 13",
                        "repairType": "battery",
                        "repairLabel": "Baterija",
                        "quality": "Battery FixPremium",
                        "productName": "Baterija FixPremium za iPhone 13",
                        "sourceUrl": "https://fixshop.si/test-battery-iphone-13/",
                        "publicPrice": 22.0,
                        "competitorPrice": 75,
                        "autoEligible": True,
                        "inventoryTier": "A",
                        "targetVehicle": 1,
                        "targetHome": 1,
                        "reorderPoint": 1,
                    },
                ],
            }), encoding="utf-8")

            store = SupplierCatalogStore(
                inventory_db,
                runtime.settings["pricing"],
                runtime.settings["supplier"]["fixshop"],
            )
            store.init()
            self.assertEqual(store.seed_from_file(seed), 2)
            row = store.list_catalog()[0]
            self.assertEqual(row["offer_status"], "OBJAVI")
            self.assertEqual(row["pickup_price"], 150)
            self.assertEqual(row["onsite_price"], 180)
            self.assertEqual(row["local_status"], "NI V LOKALNI BAZI")
            inventory_id = store.ensure_inventory_item(row["id"])
            row = store.list_catalog()[0]
            self.assertEqual(row["local_status"], "NI NA LOKALNI ZALOGI")
            connection = sqlite3.connect(inventory_db)
            connection.execute(
                "UPDATE inventory_items SET stock_qty=1 WHERE id=?", (inventory_id,)
            )
            connection.execute(
                "UPDATE supplier_catalog SET last_success='2099-01-01T00:00:00+00:00'"
            )
            connection.commit()
            connection.close()
            self.assertEqual(store.list_catalog()[0]["local_status"], "NA ZALOGI: 1")
            self.assertEqual(store.refresh_due()["status"], "fresh")
            export_path = app_dir / "mikrofix-prices.json"
            payload = store.export_site_prices(export_path)
            self.assertEqual(len(payload["models"]), 1)
            self.assertIsNone(payload["models"][0]["combo"])
            self.assertEqual(
                payload["fulfillment"]["organizedPickupReturn"]["price"], 30
            )
            self.assertEqual(
                payload["fulfillment"]["dedicatedPickupReturn"]["price"], 30
            )
            self.assertEqual(
                payload["fulfillment"]["dedicatedPickupReturn"]["status"], "quote"
            )
            exported_text = export_path.read_text(encoding="utf-8")
            self.assertNotIn("publicPrice", exported_text)
            self.assertNotIn("sourceUrl", exported_text)

    def test_uncompetitive_screen_stays_quote(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = load_runtime(Path(raw_dir))
            store = SupplierCatalogStore(
                runtime.db_path("inventory"),
                runtime.settings["pricing"],
                runtime.settings["supplier"]["fixshop"],
            )
            result = store._calculate_offer(119, "Soft OLED", "screen", 129, True)
            self.assertEqual(result["offer_status"], "PO PONUDBI")

    def test_supplier_refresh_rejects_non_fixshop_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            runtime = load_runtime(Path(raw_dir))
            store = SupplierCatalogStore(
                runtime.db_path("inventory"),
                runtime.settings["pricing"],
                runtime.settings["supplier"]["fixshop"],
            )
            with self.assertRaisesRegex(ValueError, "dovoljenem HTTPS"):
                store._fetch_one({
                    "id": 1,
                    "source_url": "http://127.0.0.1/private",
                    "etag": None,
                    "last_modified": None,
                })


class WeeklyReportTests(unittest.TestCase):
    @staticmethod
    def _entry(observed_on="2026-08-27"):
        return {
            "model": "iPhone 14",
            "storage": "128 GB",
            "market_offers": [
                {
                    "seller": "Swappie",
                    "price_eur": 329,
                    "url": "https://swappie.com/si-en/iphone/",
                    "observed_on": observed_on,
                    "warranty_months": 12,
                },
                {
                    "seller": "Refurbed",
                    "price_eur": 360,
                    "url": "https://www.refurbed.si/p/iphone-14/",
                    "observed_on": observed_on,
                    "warranty_months": 12,
                },
            ],
            "battery": {
                "cost_eur": 18.83,
                "url": "https://fixshop.si/battery/",
                "observed_on": observed_on,
            },
            "seal_material_eur": 2.46,
        }

    def test_formula_uses_median_room_and_rounding(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            settings = load_runtime(Path(raw_dir)).settings["weekly_report"]
            row = calculate_entry(
                self._entry(), settings, as_of=date(2026, 8, 27)
            )
        self.assertEqual(row.status, "OK")
        self.assertEqual(row.reference_eur, 344.50)
        self.assertEqual(row.refurbishment_eur, 72.54)
        self.assertEqual(row.maximum_eur, 200.0)
        self.assertEqual(row.recommended_eur, 180.0)

    def test_stale_sources_make_row_na(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            settings = load_runtime(Path(raw_dir)).settings["weekly_report"]
            row = calculate_entry(
                self._entry("2026-08-18"), settings, as_of=date(2026, 8, 27)
            )
        self.assertEqual(row.status, "N/A")
        self.assertIn("Premalo svežih", row.detail)

    def test_public_source_parsers(self) -> None:
        swappie = """
        <script type="application/ld+json">{
          "@graph":[{"@type":"ItemList","itemListElement":[
            {"item":{"name":"iPhone 14","offers":{"price":"329.00","priceCurrency":"EUR"}}}
          ]}]
        }</script>
        """
        refurbed = """
        <script type="application/ld+json">[
          {"@type":"ProductGroup","hasVariant":[
            {"size":"128 GB","offers":{"price":360,"priceCurrency":"EUR","availability":"https://schema.org/InStock"}},
            {"size":"256 GB","offers":{"price":450,"priceCurrency":"EUR","availability":"https://schema.org/InStock"}}
          ]}
        ]</script>
        """
        fixshop = '<meta property="product:price:amount" content="18.83">'
        self.assertEqual(parse_swappie_prices(swappie)["iPhone 14"], 329.0)
        self.assertEqual(parse_refurbed_price(refurbed, "128 GB"), 360.0)
        self.assertEqual(parse_weekly_fixshop_price(fixshop), 18.83)

    def test_pdf_requires_data_and_keeps_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            base = Path(raw_dir)
            runtime = load_runtime(base)
            input_path = base / "weekly.json"
            save_weekly_inputs(input_path, {
                "schema_version": 1,
                "entries": [self._entry()],
            })
            report, rows = generate_weekly_pdf(
                input_path,
                base / "reports",
                runtime.settings["weekly_report"],
                as_of=date(2026, 8, 27),
                generated_at=datetime(2026, 8, 27, 12, 0),
            )
            self.assertTrue(report.is_file())
            self.assertTrue(report.with_suffix(".audit.json").is_file())
            self.assertEqual(rows[0].recommended_eur, 180.0)
            self.assertEqual(report_for_week(base / "reports", date(2026, 8, 27)), report)
            same_report, _ = generate_weekly_pdf(
                input_path,
                base / "reports",
                runtime.settings["weekly_report"],
                as_of=date(2026, 8, 27),
            )
            self.assertEqual(same_report, report)

            save_weekly_inputs(input_path, {"schema_version": 1, "entries": []})
            with self.assertRaisesRegex(WeeklyReportError, "ni niti ene"):
                generate_weekly_pdf(
                    input_path,
                    base / "empty",
                    runtime.settings["weekly_report"],
                    as_of=date(2026, 8, 27),
                )


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        ensure_workflow_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_service_order_is_numbered_persistent_and_completable(self) -> None:
        order_id, order_no = create_service_order(
            self.connection, sample_service_order()
        )
        self.assertRegex(order_no, r"^MF-SN-\d{4}-0001$")
        record = get_service_order(self.connection, order_id)
        self.assertEqual(record["device_serial"], "TESTSERIAL001")
        self.assertEqual(
            latest_open_service_order(
                self.connection, "TESTSERIAL001", "000000000000001"
            ),
            order_id,
        )
        with self.assertRaisesRegex(WorkflowError, "že obstaja odprto"):
            create_service_order(self.connection, sample_service_order())
        complete_service_order(self.connection, order_id, 77)
        self.assertEqual(get_service_order(self.connection, order_id)["status"], "COMPLETED")
        self.assertIsNone(
            latest_open_service_order(
                self.connection, "TESTSERIAL001", "000000000000001"
            )
        )

    def test_other_device_uses_confirmed_physical_identity(self) -> None:
        payload = sample_service_order()
        payload.update({
            "device_model": "Namizni PC - test",
            "device_serial": "PC-LABEL-001",
            "device_imei": "",
            "device_os": "",
            "device_storage": "",
            "device_type": "NAMIZNI_PC",
            "identity_method": "MANUAL_OTHER",
            "microdetect_confirmed": False,
            "manual_identity_confirmed": True,
        })
        order_id, _ = create_service_order(self.connection, payload)
        record = get_service_order(self.connection, order_id)
        self.assertEqual(record["identity_method"], "MANUAL_OTHER")
        self.assertEqual(record["device_type"], "NAMIZNI_PC")
        self.assertEqual(record["microdetect_confirmed"], 0)

    def test_service_invoice_bridge_carries_labor_parts_and_links_back(self) -> None:
        c = sqlite3.connect(":memory:")
        c.execute(
            """
            CREATE TABLE service(
                id INTEGER PRIMARY KEY, service_no TEXT, customer TEXT,
                customer_address TEXT, customer_phone TEXT, model TEXT,
                serial TEXT, work TEXT, invoice_no TEXT, service_order_id INTEGER
            )
            """
        )
        c.execute(
            "CREATE TABLE service_orders(id INTEGER PRIMARY KEY, customer_email TEXT)"
        )
        c.execute(
            """
            CREATE TABLE service_parts(
                id INTEGER PRIMARY KEY, service_id INTEGER, article_id INTEGER,
                barcode TEXT, part_name TEXT, part_id TEXT, unit_price REAL
            )
            """
        )
        c.execute("INSERT INTO service_orders VALUES(7, 'test@example.invalid')")
        c.execute(
            """
            INSERT INTO service(
                id, service_no, customer, customer_address, customer_phone,
                model, serial, work, invoice_no, service_order_id
            ) VALUES(1, 'MF-2026-0001', 'TEST Stranka', 'Testna 1',
                     '040000000', 'TEST PC', 'PC-001', 'Menjava procesorja', '', 7)
            """
        )
        c.execute(
            """
            INSERT INTO service_parts(
                id, service_id, article_id, barcode, part_name, part_id, unit_price
            ) VALUES(1, 1, 12, 'TEST-CPU', 'Procesor TEST', 'CPU-TEST', 100)
            """
        )
        seed = load_service_invoice_seed(c, 1, labor_floor_eur=22.0)
        self.assertEqual(seed.work, "Menjava procesorja")
        self.assertEqual(seed.labor_price, 22.0)
        self.assertEqual(seed.customer_email, "test@example.invalid")
        self.assertEqual(seed.parts[0][2], "Procesor TEST")
        mark_service_invoiced(c, 1, "IRA-2026-0001", 9)
        linked = c.execute(
            "SELECT invoice_no, office_invoice_id FROM service WHERE id=1"
        ).fetchone()
        self.assertEqual(linked, ("IRA-2026-0001", 9))
        c.close()

    def test_service_order_and_buyback_create_three_page_ready_pdfs(self) -> None:
        order_id, _ = create_service_order(self.connection, sample_service_order())
        buyback_id, buyback_no = create_buyback(
            self.connection, sample_buyback(), test_mode=True
        )
        self.assertRegex(buyback_no, r"^MF-ODK-\d{4}-0001$")
        with tempfile.TemporaryDirectory() as raw_dir:
            output_dir = Path(raw_dir)
            service_pdf = generate_service_order_pdf(
                get_service_order(self.connection, order_id),
                output_dir,
                test_mode=True,
            )
            buyback_pdf = generate_buyback_pdf(
                get_buyback(self.connection, buyback_id),
                output_dir,
                test_mode=True,
            )
            self.assertGreater(service_pdf.stat().st_size, 5_000)
            self.assertGreater(buyback_pdf.stat().st_size, 5_000)

    def test_buyback_can_roll_back_before_pdf_is_committed(self) -> None:
        create_buyback(
            self.connection, sample_buyback(), test_mode=True, commit=False
        )
        self.connection.rollback()
        count = self.connection.execute("SELECT COUNT(*) FROM buybacks").fetchone()[0]
        self.assertEqual(count, 0)

    def test_buyback_cash_limit_and_manual_risk_paths(self) -> None:
        data = sample_buyback()
        data["payout_method"] = "CASH"
        data["purchase_price"] = "420.01"
        with self.assertRaisesRegex(WorkflowError, "Gotovinski"):
            validate_buyback(data, test_mode=True)

        data["purchase_price"] = "420.00"
        data["seller_iban"] = ""
        cleaned = validate_buyback(data, test_mode=False)
        self.assertEqual(cleaned["payout_method"], "CASH")
        self.assertEqual(cleaned["imei_check_result"], "NOT_REQUIRED")

        data = sample_buyback()
        data["risk_level"] = "ORANGE"
        with self.assertRaisesRegex(WorkflowError, "zeleno"):
            validate_buyback(data, test_mode=True)

        data = sample_buyback()
        data["seller_iban"] = ""
        with self.assertRaisesRegex(WorkflowError, "IBAN"):
            validate_buyback(data, test_mode=False)

    def test_cash_buyback_creates_one_voucher_and_completes_once(self) -> None:
        data = sample_buyback()
        data.update({
            "payout_method": "CASH",
            "purchase_price": "420.00",
            "seller_iban": "",
        })
        buyback_id, buyback_no = create_buyback(
            self.connection, data, test_mode=False
        )
        prepared = get_buyback(self.connection, buyback_id)
        self.assertEqual(prepared["status"], "CONTRACT_READY")
        self.assertEqual(prepared["payment_status"], "PENDING")
        self.assertRegex(prepared["cash_disbursement_no"], r"^BI-\d{4}-0001$")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cash_disbursements WHERE buyback_id=?",
                (buyback_id,),
            ).fetchone()[0],
            1,
        )
        completed = complete_buyback(self.connection, buyback_id)
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["payment_status"], "PAID")
        complete_buyback(self.connection, buyback_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cash_disbursements WHERE buyback_id=?",
                (buyback_id,),
            ).fetchone()[0],
            1,
        )

    def test_sqlite_backup_and_restore_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            base = Path(raw_dir)
            source = base / "source.db"
            backup = base / "backup.db"
            restored = base / "restored.db"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample(value) VALUES('MikroFIX')")
            connection.commit()
            connection.close()

            backup_sqlite_file(source, backup)
            verify_sqlite_file(backup)
            verify_restore_roundtrip(backup, restored)
            check = sqlite3.connect(restored)
            try:
                self.assertEqual(
                    check.execute("SELECT value FROM sample").fetchone()[0],
                    "MikroFIX",
                )
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
