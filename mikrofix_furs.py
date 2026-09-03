"""Varna integracijska osnova za FURS TEST okolje.

Ta modul namenoma ne vsebuje produkcijskih naslovov in ne zna preklopiti v
produkcijo.  Namenjen je preverjanju potrdila, dvosmerne TLS povezave (echo),
izdelavi uradno strukturiranega testnega JSON/JWS sporočila, izračunu ZOI ter
obdelavi testnega EOR oziroma napake.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


FURS_TEST_BASE = "https://blagajne-test.fu.gov.si:9002"
FURS_TEST_ECHO_URL = f"{FURS_TEST_BASE}/v1/cash_registers/echo"
FURS_TEST_INVOICE_URL = f"{FURS_TEST_BASE}/v1/cash_registers/invoices"
FURS_TEST_PREMISE_URL = f"{FURS_TEST_BASE}/v1/cash_registers/invoices/register"
FURS_TECHNICAL_VERSION = "3.2 (23. 03. 2026)"
_TEST_HOST = "blagajne-test.fu.gov.si"
_TEST_PORT = 9002
_CERT_DIR = Path(__file__).resolve().parent / "certs"
FURS_TEST_CA_BUNDLE = _CERT_DIR / "furs-test-ca-bundle.pem"
FURS_TEST_RESPONSE_CERTIFICATE = _CERT_DIR / "DavPotRacTEST.cer"


class FursTestError(RuntimeError):
    """Pričakovana, uporabniku razložljiva napaka FURS TEST poti."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CertificateBundle:
    private_key: Any
    certificate: Any
    additional_certificates: tuple[Any, ...]
    subject_name: str
    issuer_name: str
    serial_number: int
    tax_number: int
    not_valid_before: datetime
    not_valid_after: datetime

    @property
    def valid_until_text(self) -> str:
        return self.not_valid_after.astimezone().strftime("%d.%m.%Y %H:%M")


@dataclass(frozen=True)
class InvoiceResponse:
    ok: bool
    eor: str
    error_code: str
    error_message: str
    message_id: str
    response_time: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class BusinessPremiseResponse:
    ok: bool
    error_code: str
    error_message: str
    message_id: str
    response_time: str
    raw_payload: dict[str, Any]


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _certificate_time(certificate: Any, modern_name: str, legacy_name: str) -> datetime:
    value = getattr(certificate, modern_name, None)
    if value is None:
        value = getattr(certificate, legacy_name)
    return _as_utc(value)


def _extract_test_tax_number(certificate: Any) -> int:
    from cryptography.x509.oid import NameOID

    candidates = []
    for attribute in certificate.subject.get_attributes_for_oid(
        NameOID.ORGANIZATIONAL_UNIT_NAME
    ):
        candidates.append(str(attribute.value).strip())
    for value in candidates:
        if re.fullmatch(r"[1-9][0-9]{7}", value):
            return int(value)
    raise FursTestError(
        "TEST_TAX_NUMBER_MISSING",
        "V testnem potrdilu ni najdene anonimizirane 8-mestne davčne številke.",
    )


def load_test_certificate(path: str | Path, password: str) -> CertificateBundle:
    """Odpre .p12 in izrecno zavrne produkcijsko namensko potrdilo."""
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization.pkcs12 import (
            load_key_and_certificates,
        )
    except ImportError as exc:
        raise FursTestError(
            "CRYPTOGRAPHY_MISSING",
            "Manjka varnostna knjižnica cryptography. Znova zaženi namesti_v28.bat.",
        ) from exc

    cert_path = Path(path).expanduser().resolve()
    if not cert_path.is_file():
        raise FursTestError("CERTIFICATE_MISSING", "Izbrana datoteka potrdila ne obstaja.")
    if cert_path.suffix.lower() not in {".p12", ".pfx"}:
        raise FursTestError(
            "CERTIFICATE_FORMAT",
            "Za FURS TEST izberi potrdilo v obliki .p12 oziroma .pfx.",
        )
    if not password:
        raise FursTestError("PASSWORD_MISSING", "Vpiši geslo testnega potrdila.")

    try:
        key, certificate, additional = load_key_and_certificates(
            cert_path.read_bytes(), password.encode("utf-8")
        )
    except Exception as exc:
        raise FursTestError(
            "CERTIFICATE_OPEN_FAILED",
            "Testnega potrdila ni mogoče odpreti. Preveri datoteko in geslo.",
        ) from exc

    if certificate is None or key is None:
        raise FursTestError(
            "CERTIFICATE_INCOMPLETE",
            "Potrdilo ne vsebuje certifikata in pripadajočega zasebnega ključa.",
        )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise FursTestError(
            "CERTIFICATE_KEY_TYPE",
            "FURS potrdilo mora vsebovati zasebni ključ RSA.",
        )

    subject = certificate.subject.rfc4514_string()
    issuer = certificate.issuer.rfc4514_string()
    certificate_identity = f"{subject},{issuer}".upper()
    # FURS je skozi različice uporabljal več nazivov testnega overitelja.
    # Uradna dokumentacija v3.2 v primerih navaja tudi EDB-CA Test, zato
    # preverjanje ne sme biti vezano samo na zgodovinski zapis Tax CA Test.
    test_markers = (
        "DAVPOTRACTEST",
        "TAX CA TEST",
        "TAXCA TEST",
        "EDB-CA TEST",
        "EDB CA TEST",
    )
    production_davpotrac = (
        "DAVPOTRAC" in certificate_identity
        and "DAVPOTRACTEST" not in certificate_identity
    )
    if production_davpotrac:
        raise FursTestError(
            "PRODUCTION_CERTIFICATE_REJECTED",
            "To ni FURS testno potrdilo. Produkcijsko potrdilo je v tej konzoli zavrnjeno.",
        )
    if not any(marker in certificate_identity for marker in test_markers):
        raise FursTestError(
            "TEST_CERTIFICATE_IDENTITY",
            "Potrdilo ni prepoznano kot namensko FURS TEST potrdilo. "
            f"Nosilec: {subject}. Izdajatelj: {issuer}.",
        )

    not_before = _certificate_time(
        certificate, "not_valid_before_utc", "not_valid_before"
    )
    not_after = _certificate_time(
        certificate, "not_valid_after_utc", "not_valid_after"
    )
    now = _utc_now()
    if now < not_before:
        raise FursTestError("CERTIFICATE_NOT_YET_VALID", "Testno potrdilo še ni veljavno.")
    if now > not_after:
        raise FursTestError("CERTIFICATE_EXPIRED", "Testno potrdilo je poteklo.")

    return CertificateBundle(
        private_key=key,
        certificate=certificate,
        additional_certificates=tuple(additional or ()),
        subject_name=subject,
        issuer_name=issuer,
        serial_number=int(certificate.serial_number),
        tax_number=_extract_test_tax_number(certificate),
        not_valid_before=not_before,
        not_valid_after=not_after,
    )


def calculate_zoi(
    bundle: CertificateBundle,
    *,
    issue_datetime: datetime,
    invoice_number: str,
    business_premise_id: str,
    electronic_device_id: str,
    invoice_amount: Decimal | float | str,
) -> str:
    """Izračuna ZOI natančno iz niza, ki ga določa specifikacija FURS."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    amount = Decimal(str(invoice_amount)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    issue_for_zoi = issue_datetime.strftime("%d.%m.%Y %H:%M:%S")
    source = (
        f"{bundle.tax_number}{issue_for_zoi}{invoice_number}"
        f"{business_premise_id}{electronic_device_id}{amount:.2f}"
    ).encode("utf-8")
    signature = bundle.private_key.sign(
        source,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    # MD5 je tukaj izključno zakonsko predpisan zaključni korak ZOI, ne
    # splošna varnostna zgoščevalna funkcija programa.
    return hashlib.md5(signature).hexdigest()


def create_jws(payload: dict[str, Any], bundle: CertificateBundle) -> str:
    """Podpiše JSON po JWS compact serialization (RS256)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header = {
        "alg": "RS256",
        "subject_name": bundle.subject_name,
        "issuer_name": bundle.issuer_name,
        "cty": "application/json",
        "typ": "JOSE",
        "serial": bundle.serial_number,
    }
    header_bytes = json.dumps(
        header, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    signing_input = (
        f"{_b64url_encode(header_bytes)}.{_b64url_encode(payload_bytes)}"
    ).encode("ascii")
    signature = bundle.private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{signing_input.decode('ascii')}.{_b64url_encode(signature)}"


def _load_x509_certificate(path: str | Path) -> Any:
    from cryptography import x509

    cert_path = Path(path)
    if not cert_path.is_file():
        raise FursTestError(
            "TRUSTED_CERTIFICATE_MISSING",
            f"Manjka uradno FURS potrdilo: {cert_path.name}.",
        )
    raw = cert_path.read_bytes()
    try:
        if b"-----BEGIN CERTIFICATE-----" in raw:
            return x509.load_pem_x509_certificate(raw)
        return x509.load_der_x509_certificate(raw)
    except Exception as exc:
        raise FursTestError(
            "TRUSTED_CERTIFICATE_INVALID",
            f"Uradno FURS potrdilo {cert_path.name} ni berljivo.",
        ) from exc


def decode_and_verify_test_response(
    token: str,
    *,
    trusted_certificate_path: str | Path = FURS_TEST_RESPONSE_CERTIFICATE,
) -> dict[str, Any]:
    """Preveri podpis testnega FURS JWS odgovora in vrne njegovo vsebino."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        raise FursTestError("CRYPTOGRAPHY_MISSING", "Manjka knjižnica cryptography.") from exc

    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise FursTestError("RESPONSE_FORMAT", "FURS odgovor nima veljavne oblike JWS.")
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        signature = _b64url_decode(parts[2])
    except Exception as exc:
        raise FursTestError("RESPONSE_DECODE", "FURS odgovora ni mogoče dekodirati.") from exc

    if header.get("alg") != "RS256":
        raise FursTestError("RESPONSE_ALGORITHM", "FURS odgovor ni podpisan z RS256.")
    chain = header.get("x5c") or []
    if not chain:
        raise FursTestError("RESPONSE_CERTIFICATE", "FURS odgovor ne vsebuje podpisnega potrdila.")
    try:
        leaf = x509.load_der_x509_certificate(base64.b64decode(chain[0]))
    except Exception as exc:
        raise FursTestError("RESPONSE_CERTIFICATE", "Podpisno potrdilo odgovora ni berljivo.") from exc

    trusted = _load_x509_certificate(trusted_certificate_path)
    if leaf.fingerprint(hashes.SHA256()) != trusted.fingerprint(hashes.SHA256()):
        raise FursTestError(
            "RESPONSE_CERTIFICATE_UNTRUSTED",
            "Podpisno potrdilo odgovora se ne ujema z uradnim potrdilom FURS TEST.",
        )

    response_identity = f"{leaf.subject.rfc4514_string()},{leaf.issuer.rfc4514_string()}".upper()
    if "DAVPOTRACTEST" not in response_identity:
        raise FursTestError(
            "NON_TEST_RESPONSE",
            "Odgovor ni podpisan s potrdilom FURS testnega okolja.",
        )
    now = _utc_now()
    if not (
        _certificate_time(leaf, "not_valid_before_utc", "not_valid_before")
        <= now
        <= _certificate_time(leaf, "not_valid_after_utc", "not_valid_after")
    ):
        raise FursTestError("RESPONSE_CERTIFICATE_TIME", "Podpisno potrdilo odgovora ni veljavno.")
    public_key = leaf.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise FursTestError("RESPONSE_KEY_TYPE", "FURS odgovor nima ključa RSA.")
    try:
        public_key.verify(
            signature,
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise FursTestError("RESPONSE_SIGNATURE", "Podpis FURS odgovora ni veljaven.") from exc
    return payload


def _validate_identifier(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z]{1,20}", value):
        raise FursTestError(
            "IDENTIFIER_INVALID",
            f"{label} mora vsebovati 1–20 črk ali številk brez presledkov.",
        )
    return value


def build_one_euro_test_invoice(
    bundle: CertificateBundle,
    *,
    business_premise_id: str,
    electronic_device_id: str,
    issue_datetime: datetime | None = None,
    invoice_number: str | None = None,
) -> dict[str, Any]:
    """Pripravi anonimiziran račun 1,00 EUR samo za FURS TEST okolje."""
    premise = _validate_identifier(business_premise_id, "Oznaka poslovnega prostora")
    device = _validate_identifier(electronic_device_id, "Oznaka elektronske naprave")
    issued = issue_datetime or datetime.now().astimezone()
    number = _validate_identifier(
        invoice_number or f"T{issued.strftime('%y%m%d%H%M%S')}",
        "Testna številka računa",
    )
    amount = Decimal("1.00")
    message_id = str(uuid.uuid4())
    issued_text = issued.strftime("%Y-%m-%dT%H:%M:%S")
    zoi = calculate_zoi(
        bundle,
        issue_datetime=issued,
        invoice_number=number,
        business_premise_id=premise,
        electronic_device_id=device,
        invoice_amount=amount,
    )
    return {
        "InvoiceRequest": {
            "Header": {
                "MessageID": message_id,
                "DateTime": issued_text,
            },
            "Invoice": {
                "TaxNumber": bundle.tax_number,
                "IssueDateTime": issued_text,
                "NumberingStructure": "B",
                "InvoiceIdentifier": {
                    "BusinessPremiseID": premise,
                    "ElectronicDeviceID": device,
                    "InvoiceNumber": number,
                },
                "InvoiceAmount": 1.00,
                "PaymentAmount": 1.00,
                "TaxesPerSeller": [{"ExemptVATTaxableAmount": 1.00}],
                # V anonimnem testu uporabimo testno davčno številko. To polje
                # se nikoli ne prenese v produkcijske nastavitve operaterja.
                "OperatorTaxNumber": bundle.tax_number,
                "ProtectedID": zoi,
                "SpecialNotes": (
                    "MikroFIX avtomatski FURS TEST; brez pravnega in "
                    "računovodskega učinka."
                ),
            },
        }
    }


def build_test_business_premise(
    bundle: CertificateBundle,
    *,
    business_premise_id: str,
    software_supplier_tax_number: int | str,
    validity_date: str | None = None,
) -> dict[str, Any]:
    """Pripravi prijavo ločenega premičnega prostora samo za FURS TEST.

    Tip C je po uradni shemi posamezna elektronska naprava, kadar testni
    zavezanec nima drugega poslovnega prostora. Tako test ne spreminja
    dejanskih produkcijskih prijav MFD/MFT.
    """
    premise = _validate_identifier(business_premise_id, "Testni poslovni prostor")
    try:
        supplier_tax = int(str(software_supplier_tax_number).strip())
    except (TypeError, ValueError) as exc:
        raise FursTestError(
            "SOFTWARE_SUPPLIER_TAX_NUMBER",
            "Davčna številka dobavitelja programske opreme mora imeti 8 številk.",
        ) from exc
    if supplier_tax < 10_000_000 or supplier_tax > 99_999_999:
        raise FursTestError(
            "SOFTWARE_SUPPLIER_TAX_NUMBER",
            "Davčna številka dobavitelja programske opreme mora imeti 8 številk.",
        )
    issued = datetime.now().astimezone()
    active_from = validity_date or issued.date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", active_from):
        raise FursTestError(
            "VALIDITY_DATE",
            "Datum veljavnosti testnega prostora mora biti v obliki LLLL-MM-DD.",
        )
    return {
        "BusinessPremiseRequest": {
            "Header": {
                "MessageID": str(uuid.uuid4()),
                "DateTime": issued.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            "BusinessPremise": {
                "TaxNumber": bundle.tax_number,
                "BusinessPremiseID": premise,
                "BPIdentifier": {"PremiseType": "C"},
                "ValidityDate": active_from,
                "SoftwareSupplier": [{"TaxNumber": supplier_tax}],
                "SpecialNotes": (
                    "MikroFIX avtomatska prijava ločenega prostora samo v "
                    "FURS TEST; brez pravnega in računovodskega učinka."
                ),
            },
        }
    }


def parse_invoice_response(
    payload: dict[str, Any], *, expected_message_id: str | None = None
) -> InvoiceResponse:
    response = payload.get("InvoiceResponse")
    if not isinstance(response, dict):
        raise FursTestError("RESPONSE_PAYLOAD", "FURS odgovor ne vsebuje InvoiceResponse.")
    header = response.get("Header") or {}
    message_id = str(header.get("MessageID") or "")
    if expected_message_id and message_id.lower() != expected_message_id.lower():
        raise FursTestError(
            "RESPONSE_MESSAGE_ID",
            "Identifikator FURS odgovora se ne ujema s poslanim sporočilom.",
        )
    eor = str(response.get("UniqueInvoiceID") or "")
    error = response.get("Error") or {}
    error_code = str(error.get("ErrorCode") or "")
    error_message = str(error.get("ErrorMessage") or "")
    if not eor and not error_code:
        raise FursTestError("RESPONSE_EMPTY", "FURS ni vrnil niti EOR niti opisane napake.")
    return InvoiceResponse(
        ok=bool(eor),
        eor=eor,
        error_code=error_code,
        error_message=error_message,
        message_id=message_id,
        response_time=str(header.get("DateTime") or ""),
        raw_payload=payload,
    )


def parse_business_premise_response(
    payload: dict[str, Any], *, expected_message_id: str | None = None
) -> BusinessPremiseResponse:
    response = payload.get("BusinessPremiseResponse")
    if not isinstance(response, dict):
        raise FursTestError(
            "PREMISE_RESPONSE_PAYLOAD",
            "FURS odgovor ne vsebuje BusinessPremiseResponse.",
        )
    header = response.get("Header") or {}
    message_id = str(header.get("MessageID") or "")
    if expected_message_id and message_id.lower() != expected_message_id.lower():
        raise FursTestError(
            "RESPONSE_MESSAGE_ID",
            "Identifikator odgovora prijave prostora se ne ujema s poslanim sporočilom.",
        )
    error = response.get("Error") or {}
    error_code = str(error.get("ErrorCode") or "")
    error_message = str(error.get("ErrorMessage") or "")
    return BusinessPremiseResponse(
        ok=not bool(error_code),
        error_code=error_code,
        error_message=error_message,
        message_id=message_id,
        response_time=str(header.get("DateTime") or ""),
        raw_payload=payload,
    )


def _assert_test_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _TEST_HOST
        or parsed.port != _TEST_PORT
        or not parsed.path.startswith("/v1/cash_registers")
    ):
        raise FursTestError(
            "ENDPOINT_BLOCKED",
            "FURS TEST modul sme komunicirati samo z uradnim testnim naslovom.",
        )


class FursTestClient:
    """HTTP odjemalec, ki je konstrukcijsko omejen samo na FURS TEST."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 15,
        ca_bundle_path: str | Path | None = None,
        protect_file: Callable[[Path], None] | None = None,
        session_factory: Callable[[], Any] | None = None,
    ):
        self.timeout_seconds = max(3, min(60, int(timeout_seconds)))
        self.ca_bundle_path = (
            Path(ca_bundle_path).resolve()
            if ca_bundle_path
            else FURS_TEST_CA_BUNDLE.resolve()
        )
        self.protect_file = protect_file
        self.session_factory = session_factory

    def _request(self, url: str, body: dict[str, Any], bundle: CertificateBundle) -> dict[str, Any]:
        _assert_test_url(url)
        try:
            import requests
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
            )
        except ImportError as exc:
            raise FursTestError(
                "HTTP_DEPENDENCY_MISSING",
                "Manjka varnostna spletna knjižnica. Znova zaženi namesti_v28.bat.",
            ) from exc

        verify: bool | str = True
        if self.ca_bundle_path:
            if not self.ca_bundle_path.is_file():
                raise FursTestError("CA_BUNDLE_MISSING", "Nastavljena CA datoteka ne obstaja.")
            verify = str(self.ca_bundle_path)

        with tempfile.TemporaryDirectory(prefix="mikrofix_furs_test_") as raw_temp:
            temp_dir = Path(raw_temp)
            cert_path = temp_dir / "client-chain.pem"
            key_path = temp_dir / "client-key-call.pem"
            cert_pem = bundle.certificate.public_bytes(Encoding.PEM)
            for extra in bundle.additional_certificates:
                cert_pem += extra.public_bytes(Encoding.PEM)
            cert_path.write_bytes(cert_pem)
            key_path.write_bytes(bundle.private_key.private_bytes(
                Encoding.PEM,
                PrivateFormat.PKCS8,
                NoEncryption(),
            ))
            if self.protect_file:
                self.protect_file(cert_path)
                self.protect_file(key_path)

            factory = self.session_factory or requests.Session
            session = factory()
            if hasattr(session, "trust_env"):
                session.trust_env = False
            try:
                # requests/OpenSSL potrebuje začasni PEM ključ. Datoteka je
                # omejena na trenutni Windows račun in obstaja samo med tem
                # klicem; TemporaryDirectory jo nato odstrani.
                response = session.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json; charset=UTF-8"},
                    cert=(str(cert_path), str(key_path)),
                    verify=verify,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("response is not an object")
                return data
            except FursTestError:
                raise
            except Exception as exc:
                raise FursTestError(
                    "TEST_CONNECTION_FAILED",
                    f"Povezava s FURS TEST ni uspela: {exc}",
                ) from exc
            finally:
                try:
                    session.close()
                except Exception:
                    pass

    def echo(self, bundle: CertificateBundle, text: str = "mikrofix") -> str:
        data = self._request(FURS_TEST_ECHO_URL, {"EchoRequest": text}, bundle)
        response = str(data.get("EchoResponse") or "")
        if response != text:
            raise FursTestError("ECHO_MISMATCH", "FURS TEST ni vrnil pričakovanega odgovora.")
        return response

    def submit_invoice(
        self, bundle: CertificateBundle, payload: dict[str, Any]
    ) -> tuple[InvoiceResponse, str, dict[str, Any]]:
        request_message_id = str(
            payload.get("InvoiceRequest", {}).get("Header", {}).get("MessageID") or ""
        )
        token = create_jws(payload, bundle)
        raw_response = self._request(
            FURS_TEST_INVOICE_URL, {"token": token}, bundle
        )
        response_token = str(raw_response.get("token") or "")
        verified_payload = decode_and_verify_test_response(response_token)
        parsed = parse_invoice_response(
            verified_payload, expected_message_id=request_message_id
        )
        return parsed, token, raw_response

    def register_business_premise(
        self, bundle: CertificateBundle, payload: dict[str, Any]
    ) -> tuple[BusinessPremiseResponse, str, dict[str, Any]]:
        request_message_id = str(
            payload.get("BusinessPremiseRequest", {})
            .get("Header", {})
            .get("MessageID")
            or ""
        )
        token = create_jws(payload, bundle)
        raw_response = self._request(
            FURS_TEST_PREMISE_URL, {"token": token}, bundle
        )
        response_token = str(raw_response.get("token") or "")
        verified_payload = decode_and_verify_test_response(response_token)
        parsed = parse_business_premise_response(
            verified_payload, expected_message_id=request_message_id
        )
        return parsed, token, raw_response


class FursTestStore:
    """Lokalna sled in čakalna vrsta izključno za anonimne testne račune."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        connection.execute("""
            CREATE TABLE IF NOT EXISTS furs_test_outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL UNIQUE,
                invoice_number TEXT NOT NULL,
                premise_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                zoi TEXT NOT NULL,
                eor TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(status IN ('PENDING','SENT','FAILED')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                response_json TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_furs_test_status "
            "ON furs_test_outbox(status, id)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS furs_test_premises(
                test_tax_number INTEGER NOT NULL,
                premise_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                response_message_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                PRIMARY KEY(test_tax_number, premise_id)
            )
        """)
        connection.commit()
        connection.close()

    def enqueue(self, payload: dict[str, Any]) -> int:
        request = payload["InvoiceRequest"]
        invoice = request["Invoice"]
        identifier = invoice["InvoiceIdentifier"]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection = self.connect()
        cursor = connection.execute("""
            INSERT INTO furs_test_outbox(
                message_id, invoice_number, premise_id, device_id, zoi,
                payload_json, status, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,'PENDING',?,?)
        """, (
            request["Header"]["MessageID"],
            identifier["InvoiceNumber"],
            identifier["BusinessPremiseID"],
            identifier["ElectronicDeviceID"],
            invoice["ProtectedID"],
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
        ))
        connection.commit()
        row_id = int(cursor.lastrowid)
        connection.close()
        return row_id

    def mark_sent(self, row_id: int, result: InvoiceResponse, raw_response: dict[str, Any]) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection = self.connect()
        connection.execute("""
            UPDATE furs_test_outbox
            SET status='SENT', attempt_count=attempt_count+1, eor=?,
                response_json=?, error_code=NULL, error_message=NULL,
                updated_at=?, sent_at=?
            WHERE id=?
        """, (
            result.eor,
            json.dumps(raw_response, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
            row_id,
        ))
        connection.commit()
        connection.close()

    def mark_failed(self, row_id: int, code: str, message: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection = self.connect()
        connection.execute("""
            UPDATE furs_test_outbox
            SET status='FAILED', attempt_count=attempt_count+1,
                error_code=?, error_message=?, updated_at=?
            WHERE id=?
        """, (str(code), str(message)[:1000], now, row_id))
        connection.commit()
        connection.close()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        connection = self.connect()
        rows = connection.execute("""
            SELECT id, created_at, invoice_number, premise_id, device_id,
                   status, attempt_count, zoi, eor, error_code, error_message
            FROM furs_test_outbox
            ORDER BY id DESC LIMIT ?
        """, (max(1, min(500, int(limit))),)).fetchall()
        connection.close()
        keys = (
            "id", "created_at", "invoice_number", "premise_id", "device_id",
            "status", "attempt_count", "zoi", "eor", "error_code", "error_message",
        )
        return [dict(zip(keys, row)) for row in rows]

    def mark_premise_registered(
        self,
        *,
        test_tax_number: int,
        premise_id: str,
        device_id: str,
        result: BusinessPremiseResponse,
        raw_response: dict[str, Any],
    ) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        connection = self.connect()
        connection.execute("""
            INSERT INTO furs_test_premises(
                test_tax_number, premise_id, device_id, registered_at,
                response_message_id, response_json
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(test_tax_number, premise_id) DO UPDATE SET
                device_id=excluded.device_id,
                registered_at=excluded.registered_at,
                response_message_id=excluded.response_message_id,
                response_json=excluded.response_json
        """, (
            int(test_tax_number), premise_id, device_id, now,
            result.message_id,
            json.dumps(raw_response, ensure_ascii=False, separators=(",", ":")),
        ))
        connection.commit()
        connection.close()

    def is_premise_registered(self, test_tax_number: int, premise_id: str) -> bool:
        connection = self.connect()
        row = connection.execute(
            "SELECT 1 FROM furs_test_premises "
            "WHERE test_tax_number=? AND premise_id=?",
            (int(test_tax_number), str(premise_id)),
        ).fetchone()
        connection.close()
        return row is not None


def readiness_report(settings: dict[str, Any], app_dir: str | Path) -> list[dict[str, str]]:
    """Preveri varnostne pogoje brez odpiranja potrdila in brez omrežja."""
    app_dir = Path(app_dir)
    furs = settings.get("furs", {})
    items: list[dict[str, str]] = []

    def add(status: str, label: str, detail: str) -> None:
        items.append({"status": status, "label": label, "detail": detail})

    if str(settings.get("environment", "")).upper() == "TEST":
        add("OK", "Program", "MikroFIX deluje v načinu TEST.")
    else:
        add("BLOCK", "Program", "FURS TEST konzola je dovoljena samo v TEST izdaji.")
    if str(furs.get("environment", "")).upper() == "TEST":
        add("OK", "FURS naslov", f"Zaklenjen na {_TEST_HOST}:{_TEST_PORT}.")
    else:
        add("BLOCK", "FURS naslov", "Nastavitve niso omejene na TEST.")
    if not bool(furs.get("enabled", False)):
        add("OK", "Produkcija", "Produkcijsko potrjevanje ostaja izključeno.")
    else:
        add("BLOCK", "Produkcija", "V testni izdaji enabled ne sme biti vključen.")

    raw_cert = str(furs.get("test_certificate_path") or "").strip()
    if raw_cert:
        cert_path = Path(raw_cert)
        if not cert_path.is_absolute():
            cert_path = app_dir / cert_path
        if cert_path.is_file() and cert_path.suffix.lower() in {".p12", ".pfx"}:
            add("OK", "Testno potrdilo", f"Najdeno: {cert_path.name}")
        else:
            add("BLOCK", "Testno potrdilo", "Nastavljena .p12/.pfx datoteka ni najdena.")
    else:
        add("BLOCK", "Testno potrdilo", "Še ni izbrano; produkcijsko potrdilo ni dovoljeno.")

    if FURS_TEST_CA_BUNDLE.is_file():
        add("OK", "TLS zaupanje", "Uradna SI-TRUST korenska veriga je vključena.")
    else:
        add("BLOCK", "TLS zaupanje", "Manjka uradni SI-TRUST CA paket.")
    if FURS_TEST_RESPONSE_CERTIFICATE.is_file():
        add("OK", "Podpis odgovora", "Uradno potrdilo DavPotRacTEST je vključeno.")
    else:
        add("BLOCK", "Podpis odgovora", "Manjka uradno potrdilo DavPotRacTEST.")

    try:
        import cryptography  # noqa: F401
        import requests  # noqa: F401
    except ImportError:
        add("BLOCK", "Varnostne knjižnice", "Znova zaženi namesti_v28.bat.")
    else:
        add("OK", "Varnostne knjižnice", "JWS, RSA in dvosmerni TLS so na voljo.")
    add("INFO", "Specifikacija", f"FURS tehnična dokumentacija {FURS_TECHNICAL_VERSION}.")
    return items
