"""Čiste pomožne funkcije za varno bralno zaznavo iPhona v MicroDETECT."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class DeviceDetectionError(RuntimeError):
    code: str
    user_message: str

    def __str__(self) -> str:
        return self.user_message


ERROR_MESSAGES = {
    "PMD_MISSING": (
        "Manjka dodatek pymobiledevice3. Zaženi namesti_v28.bat."
    ),
    "APPLE_SERVICE_MISSING": (
        "Windows ne najde Apple USB komunikacijskega ozadja. Zaženi Apple Devices "
        "oziroma iTunes; če se napaka ponovi, ponovno zaženi Windows ali popravi "
        "Apple podporo za mobilne naprave."
    ),
    "APPLE_SERVICE_STOPPED": (
        "Apple USB komunikacijsko ozadje je nameščeno, vendar ne deluje. "
        "Ponovno zaženi Apple Devices oziroma iTunes in nato Windows."
    ),
    "USBMUX_UNAVAILABLE": (
        "Apple USB povezovalna storitev ni dosegljiva. Preveri iTunes oziroma "
        "Apple Mobile Device Service."
    ),
    "NO_DEVICE": (
        "iPhone ni zaznan. Uporabi podatkovni USB kabel, odkleni telefon in ga priklopi neposredno v računalnik."
    ),
    "MULTIPLE_DEVICES": (
        "Priklopljenih je več Apple naprav. Za zanesljiv servis pusti priklopljen samo en iPhone."
    ),
    "NOT_IPHONE": (
        "Priklopljena Apple naprava ni iPhone. MicroDETECT v tej izdaji podpira samo iPhone."
    ),
    "TRUST_REQUIRED": (
        "Odkleni iPhone in na njem potrdi »Trust This Computer«, nato znova klikni MicroDETECT."
    ),
    "DEVICE_LOCKED": (
        "iPhone je zaklenjen. Odkleni ga in znova klikni MicroDETECT."
    ),
    "INVALID_RESPONSE": (
        "Apple povezava je vrnila neveljaven odgovor. Ponovno priklopi telefon in poskusi znova."
    ),
    "MISSING_SERIAL": (
        "Telefon je zaznan, vendar Apple ni vrnil serijske številke. Odkleni telefon in potrdi Trust."
    ),
    "MISSING_MODEL": (
        "Telefon je zaznan, vendar Apple ni vrnil modela naprave. Ponovno priklopi telefon in potrdi Trust."
    ),
    "TIMEOUT": (
        "Čas za povezavo je potekel. Odkleni iPhone, potrdi Trust in poskusi ponovno."
    ),
    "SESSION_ACTIVE": (
        "Apple povezavo trenutno uporablja druga seja. Zapri Apple Devices oziroma iTunes, "
        "telefon ponovno priklopi in nato znova klikni MicroDETECT."
    ),
    "UNKNOWN": (
        "MicroDETECT ni uspel prebrati naprave. Preveri kabel, Apple podporo in Trust."
    ),
}


def problem(code: str) -> DeviceDetectionError:
    return DeviceDetectionError(code, ERROR_MESSAGES.get(code, ERROR_MESSAGES["UNKNOWN"]))


def windows_apple_service_state() -> str:
    """Vrne running, stopped, missing ali not_windows; ničesar ne spreminja."""
    if os.name != "nt":
        return "not_windows"
    completed = subprocess.run(
        ["sc.exe", "query", "Apple Mobile Device Service"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = f"{completed.stdout}\n{completed.stderr}".upper()
    if "RUNNING" in output:
        return "running"
    try:
        with socket.create_connection(("127.0.0.1", 27015), timeout=0.25):
            return "running"
    except OSError:
        pass
    if completed.returncode == 0:
        return "stopped"
    return "missing"


def parse_usbmux_devices(raw: str) -> list[dict[str, Any]]:
    """Prebere trenutno JSON obliko in nekaj starejših različic izpisa."""
    raw = str(raw or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise problem("INVALID_RESPONSE") from exc

    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = None
        for key in ("devices", "Devices", "DeviceList", "device_list", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if candidates is None and any(
            key in payload for key in ("serial", "SerialNumber", "udid", "identifier")
        ):
            candidates = [payload]
        if candidates is None:
            raise problem("INVALID_RESPONSE")
    else:
        raise problem("INVALID_RESPONSE")

    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        connection_type = str(
            item.get("connection_type")
            or item.get("ConnectionType")
            or item.get("connectionType")
            or "USB"
        ).upper()
        if connection_type and "USB" not in connection_type:
            continue
        identity = str(
            item.get("serial")
            or item.get("SerialNumber")
            or item.get("udid")
            or item.get("identifier")
            or item.get("device_id")
            or item.get("DeviceID")
            or len(devices)
        )
        if identity in seen:
            continue
        seen.add(identity)
        devices.append(item)
    return devices


def _mux_serial(device: Any) -> str:
    if isinstance(device, str):
        return device.strip()
    if isinstance(device, dict):
        return str(
            device.get("serial")
            or device.get("SerialNumber")
            or device.get("udid")
            or device.get("identifier")
            or device.get("Identifier")
            or device.get("UniqueDeviceID")
            or ""
        ).strip()
    return str(getattr(device, "serial", "") or "").strip()


def _mux_connection_type(device: Any) -> str:
    if isinstance(device, dict):
        value = (
            device.get("connection_type")
            or device.get("ConnectionType")
            or device.get("connectionType")
            or "USB"
        )
    else:
        value = getattr(device, "connection_type", "USB")
    return str(value or "USB")


def select_single_usb_device(devices: Iterable[Any]) -> Any:
    """Izbere natanko eno fizično USB napravo in izloči Wi-Fi podvojitve."""
    unique: dict[str, Any] = {}
    for device in devices:
        if "USB" not in _mux_connection_type(device).upper():
            continue
        serial = _mux_serial(device)
        if not serial:
            continue
        unique.setdefault(serial.replace("-", "").upper(), device)

    if not unique:
        state = windows_apple_service_state()
        if state == "missing":
            raise problem("APPLE_SERVICE_MISSING")
        if state == "stopped":
            raise problem("APPLE_SERVICE_STOPPED")
        raise problem("NO_DEVICE")
    if len(unique) > 1:
        raise problem("MULTIPLE_DEVICES")
    return next(iter(unique.values()))


async def _read_usb_iphone_async(
    status_callback: Callable[[str], None] | None = None,
    *,
    list_devices_func: Callable[[], Awaitable[list[Any]]] | None = None,
    create_lockdown_func: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Prebere eno USB napravo prek javnega pymobiledevice3 API-ja.

    Funkcija ne uporablja datotečnih, varnostno-kopirnih, obnovitvenih ali
    razvojnih storitev naprave. Lockdown povezava se vedno zapre.
    """
    if list_devices_func is None or create_lockdown_func is None:
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.usbmux import list_devices
        except (ImportError, ModuleNotFoundError) as exc:
            raise problem("PMD_MISSING") from exc
        list_devices_func = list_devices
        create_lockdown_func = create_using_usbmux

    def report(message: str) -> None:
        if status_callback is not None:
            status_callback(message)

    try:
        mux_devices = await list_devices_func()
        selected = select_single_usb_device(mux_devices)
        selected_serial = _mux_serial(selected)
        connection_type = _mux_connection_type(selected)

        report(
            "iPhone je zaznan prek USB. Odkleni ga in po potrebi potrdi "
            "»Trust This Computer«..."
        )
        lockdown = await create_lockdown_func(
            serial=selected_serial,
            autopair=True,
            connection_type=connection_type,
            pair_timeout=60,
        )
        try:
            report("Zaupanje je potrjeno. Berem identifikacijske podatke...")
            values = dict(await lockdown.get_value())
            try:
                disk_usage = await lockdown.get_value(domain="com.apple.disk_usage")
            except Exception:
                disk_usage = None
            if isinstance(disk_usage, dict):
                values["MicroDetectDiskUsage"] = disk_usage
            values["MicroDetectUSBSerial"] = selected_serial
            marketing_model = str(getattr(lockdown, "display_name", "") or "")
            if marketing_model:
                values["MarketingModel"] = marketing_model
        finally:
            await lockdown.close()

        product_type = str(values.get("ProductType") or "")
        device_class = str(values.get("DeviceClass") or "")
        if not product_type.startswith("iPhone") and device_class.lower() != "iphone":
            raise problem("NOT_IPHONE")
        return values
    except DeviceDetectionError:
        raise
    except Exception as exc:
        raise classify_pmd_failure(
            f"{type(exc).__name__}: {exc}", stage="lockdown"
        ) from exc


def read_usb_iphone(
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Sinhroni vhod za delavski thread grafičnega vmesnika."""
    return asyncio.run(_read_usb_iphone_async(status_callback))


def normalize_lockdown_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise problem("INVALID_RESPONSE")
    for key in ("all_values", "AllValues", "values", "Value"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            payload = nested
            break
    return payload


def storage_capacity_label(payload: Any) -> str:
    """Vrne oglaševano zmogljivost (npr. 256 GB), kadar jo iOS razkrije."""
    data = normalize_lockdown_info(payload)
    disk_usage = data.get("MicroDetectDiskUsage")
    sources = [disk_usage, data] if isinstance(disk_usage, dict) else [data]
    raw_value: Any = None
    for source in sources:
        for key in (
            "TotalDiskCapacity",
            "TotalDataCapacity",
            "DiskCapacity",
            "StorageCapacity",
        ):
            if source.get(key) not in (None, ""):
                raw_value = source.get(key)
                break
        if raw_value is not None:
            break
    if raw_value is None:
        return ""

    try:
        numeric = float(str(raw_value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return ""
    if numeric <= 0:
        return ""

    # Lockdown praviloma vrne bajte. Pri testnih virih dovolimo tudi neposreden GB.
    decimal_gb = numeric / 1_000_000_000 if numeric > 100_000 else numeric
    standard_sizes = (8, 16, 32, 64, 128, 256, 512, 1024, 2048)
    closest = min(standard_sizes, key=lambda size: abs(size - decimal_gb))
    tolerance = max(4.0, closest * 0.12)
    if abs(closest - decimal_gb) > tolerance:
        return ""
    return f"{closest // 1024} TB" if closest >= 1024 else f"{closest} GB"


def extract_device_identity(payload: Any) -> dict[str, str]:
    data = normalize_lockdown_info(payload)
    serial = str(
        data.get("SerialNumber")
        or data.get("serial_number")
        or data.get("UniqueDeviceID")
        or ""
    ).strip()
    if not serial:
        raise problem("MISSING_SERIAL")
    model = str(
        data.get("MarketingModel")
        or data.get("ProductType")
        or data.get("ProductName")
        or data.get("HardwareModel")
        or ""
    ).strip()
    if not model:
        raise problem("MISSING_MODEL")
    return {
        "serial": serial,
        "imei": str(
            data.get("InternationalMobileEquipmentIdentity")
            or data.get("MobileEquipmentIdentifier")
            or data.get("IMEI")
            or data.get("MEID")
            or ""
        ).strip(),
        "model": model,
        "ios": str(data.get("ProductVersion") or "").strip(),
        "name": str(data.get("DeviceName") or "").strip(),
        "storage": storage_capacity_label(data),
    }


def classify_pmd_failure(raw: str, *, stage: str = "") -> DeviceDetectionError:
    text = str(raw or "").lower()
    if "no module named" in text and "pymobiledevice3" in text:
        return problem("PMD_MISSING")
    if any(marker in text for marker in (
        "timeouterror", "timed out", "timeout expired"
    )):
        return problem("TIMEOUT")
    if "sessionactive" in text or "session active" in text:
        return problem("SESSION_ACTIVE")
    if any(marker in text for marker in (
        "connectionfailedtousbmuxderror",
        "failed to connect to usbmuxd",
        "connection refused",
        "winerror 10061",
        "usbmuxd socket",
    )):
        return problem("USBMUX_UNAVAILABLE")
    if any(marker in text for marker in (
        "pairingdialogresponsepending",
        "trust this computer",
        "not paired",
        "notpairederror",
        "invalidhostid",
        "pair record",
        "please accept the trust",
        "user denied pairing",
        "userdeniedpairingerror",
        "fatalpairingerror",
    )):
        return problem("TRUST_REQUIRED")
    if any(marker in text for marker in (
        "password protected",
        "device is locked",
        "device locked",
        "passwordrequirederror",
    )):
        return problem("DEVICE_LOCKED")
    if any(marker in text for marker in (
        "nodeviceconnectederror",
        "no device connected",
        "device is not connected",
        "no device found",
    )):
        return problem("NO_DEVICE")
    if stage == "usbmux":
        return problem("USBMUX_UNAVAILABLE")
    return problem("UNKNOWN")
