"""Preprost pregled pripravljenosti računalnika za MikroFIX v28."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

from mikrofix_auth import AuthStore
from mikrofix_device import windows_apple_service_state
from mikrofix_furs import readiness_report
from mikrofix_runtime import APP_VERSION, load_runtime
from mikrofix_tk_bootstrap import prepare_tcl_tk


APP_DIR = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def result(ok: bool, name: str, detail: str) -> None:
    marker = "OK" if ok else "POZOR"
    print(f"[{marker}] {name}: {detail}")


def package_check(import_name: str, package_name: str) -> bool:
    try:
        importlib.import_module(import_name)
        version = importlib.metadata.version(package_name)
    except Exception as exc:
        result(False, package_name, f"manjka ali se ne naloži ({exc})")
        return False
    result(True, package_name, f"različica {version}")
    return True


def apple_service_status() -> tuple[bool, str]:
    state = windows_apple_service_state()
    if state == "running":
        return True, "Apple USB komunikacijsko ozadje deluje"
    if state == "stopped":
        return False, "Apple Mobile Device Service je nameščen, vendar ne deluje"
    if state == "not_windows":
        return False, "preverjanje je namenjeno Windows"
    return False, "AMDS oziroma AppleMobileDeviceProcess ni najden"


def apple_usb_driver_status() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "preverjanje je namenjeno Windows"
    completed = subprocess.run(
        ["pnputil.exe", "/enum-drivers"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    found = any(name in output for name in (
        "appleusb.inf", "usbaapl.inf", "usbaapl64.inf"
    ))
    if found:
        return True, "Apple USB gonilnik je nameščen"
    return False, "Apple USB gonilnik ni najden"


def cpp_build_tools_status() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "preverjanje je namenjeno Windows"
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return False, "Visual Studio Installer oziroma Build Tools ni najden"
    completed = subprocess.run(
        [
            str(vswhere), "-latest", "-products", "*", "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property", "installationPath",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    install_path = completed.stdout.strip()
    if completed.returncode == 0 and install_path:
        return True, f"MSVC x64/x86 je nameščen ({install_path})"
    return False, "manjka delovna obremenitev Desktop development with C++"


def printer_status(configured_name: str) -> tuple[bool, str]:
    if not configured_name:
        return False, "Epson TM-T20II (80 mm USB) še ni nameščen kot Windows tiskalnik"
    if os.name != "nt":
        return False, "preverjanje je namenjeno Windows"
    safe_name = configured_name.replace("'", "''")
    command = (
        "$p=Get-Printer -Name '" + safe_name + "' -ErrorAction SilentlyContinue; "
        "if($p){$p.Name}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    found = configured_name.lower() in completed.stdout.lower()
    return (
        (True, f"najden {configured_name}")
        if found else (False, f"Windows ne najde tiskalnika {configured_name}")
    )


def main() -> int:
    if sys.platform.startswith("linux"):
        from preveri_produkcijo_linux import main as linux_production_main

        return linux_production_main()
    print(f"MikroFIX v{APP_VERSION} - pregled Windows okolja")
    print("=" * 51)

    is_64bit = sys.maxsize > 2**32
    recommended_python = (
        sys.version_info.major == 3 and sys.version_info.minor == 13 and is_64bit
    )
    result(
        recommended_python,
        "Python",
        f"{platform.python_version()} ({'64-bit' if is_64bit else '32-bit'}); "
        "priporočeno 3.13 64-bit",
    )

    try:
        prepare_tcl_tk()
        import tkinter
        tk_root = tkinter.Tk()
        tk_root.withdraw()
        tk_detail = f"Tcl/Tk {tk_root.tk.call('info', 'patchlevel')}"
        tk_root.destroy()
        tk_ok = True
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        tk_detail = f"Python je najden, Tcl/Tk pa ni uporaben ({first_line})"
        tk_ok = False
    result(tk_ok, "Okna programa", tk_detail)

    packages_ok = all((
        package_check("reportlab", "reportlab"),
        package_check("PIL", "Pillow"),
        package_check("fitz", "PyMuPDF"),
        package_check("segno", "segno"),
        package_check("pymobiledevice3", "pymobiledevice3"),
        package_check("cryptography", "cryptography"),
        package_check("requests", "requests"),
    ))

    cpp_ok, cpp_detail = cpp_build_tools_status()
    result(cpp_ok, "C++ Build Tools", cpp_detail)

    runtime = load_runtime(APP_DIR)
    result(
        runtime.is_test,
        "Način",
        f"{runtime.settings['environment']}; FURS "
        f"{'vključen' if runtime.settings['furs']['enabled'] else 'izključen'}",
    )
    result(
        runtime.tax_mode == "NON_VAT" and runtime.default_vat_rate == 0,
        "DDV",
        f"{runtime.tax_mode}, privzeta stopnja {runtime.default_vat_rate:.2f} %",
    )
    result(
        True,
        "Oznake",
        f"prostori {', '.join(runtime.settings['business']['premises'])}; "
        f"aktivno {runtime.active_premise}/{runtime.electronic_device}; "
        f"operater {runtime.operator_code}",
    )
    for item in readiness_report(runtime.settings, APP_DIR):
        if item["label"] in {"Testno potrdilo", "Varnostne knjižnice"}:
            result(
                item["status"] != "BLOCK",
                f"FURS TEST – {item['label']}",
                item["detail"],
            )

    seed_ok = False
    seed_detail = "mikrofix_supplier_seed.json manjka"
    seed_path = APP_DIR / "mikrofix_supplier_seed.json"
    try:
        seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
        seed_products = seed_payload.get("products", [])
        seed_ok = (
            seed_payload.get("schemaVersion") == 1
            and len(seed_products) > 0
            and all(
                str(item.get("sourceUrl", "")).startswith("https://fixshop.si/")
                and float(item.get("publicPrice", 0)) > 0
                for item in seed_products
            )
        )
        seed_detail = (
            f"{len(seed_products)} javnih FixShop artiklov; osveževanje na "
            f"{runtime.settings['supplier']['fixshop']['refresh_hours']} ur"
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        seed_detail = str(exc)
    result(seed_ok, "Dobaviteljski cenik", seed_detail)

    database_ok = False
    try:
        connection = runtime.connect_sqlite("service")
        connection.execute("SELECT 1").fetchone()
        connection.close()
        AuthStore(runtime.db_path("auth")).init()
        database_ok = True
        db_detail = str(runtime.db_dir)
    except (OSError, sqlite3.Error, ValueError) as exc:
        db_detail = str(exc)
    result(database_ok, "Testne baze", db_detail)

    apple_driver_ok, apple_driver_detail = apple_usb_driver_status()
    result(apple_driver_ok, "Apple USB gonilnik", apple_driver_detail)

    apple_ok, apple_detail = apple_service_status()
    result(apple_ok, "Apple USB", apple_detail)

    printer_ok, printer_detail = printer_status(
        str(runtime.settings["printer"].get("windows_printer_name", "")).strip()
    )
    result(printer_ok, "Termični tiskalnik", printer_detail)

    print()
    if not apple_ok:
        print("Zaženi Apple Devices oziroma iTunes in ponovno preveri. Starejša")
        print("namestitev uporablja AMDS, novejša Apple Devices pa proces")
        print("AppleMobileDeviceProcess. Če ne deluje, ponovno zaženi Windows.")
    if not printer_ok:
        print("Namesti uradni Epson gonilnik za TM-T20II in nato izvedi fizični test 80-mm računa ter QR-kode.")
    print("Produkcijski FURS v tej razvojni izdaji ostaja varnostno zaklenjen.")
    print("Manjkajoče FURS TEST potrdilo ne ovira servisa ali MicroDETECT-a.")

    required_ok = (
        recommended_python and tk_ok and packages_ok and cpp_ok and database_ok
        and seed_ok and apple_driver_ok and apple_ok
    )
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
