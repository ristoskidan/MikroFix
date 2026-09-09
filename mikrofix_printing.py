"""Zaznava tiskalnikov in neposredni tisk PDF na Windows ali prek CUPS."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    port: str = ""
    driver: str = ""
    is_virtual: bool = False


@dataclass(frozen=True)
class PrintResult:
    printer: str
    job_id: int
    completed: bool
    detail: str = ""


def _is_windows() -> bool:
    return os.name == "nt"


def _is_virtual_printer(name: str, port: str) -> bool:
    text = f"{name} {port}".lower()
    return any(marker in text for marker in (
        "microsoft print to pdf", "xps", "fax", "onenote", "portprompt",
        "cups-pdf", "print to file", "file:/",
    ))


def _cups_command(*arguments: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    executable = shutil.which(arguments[0])
    if not executable:
        raise RuntimeError(f"Manjka CUPS ukaz {arguments[0]}. Namesti paket cups-client.")
    return subprocess.run(
        [executable, *arguments[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def list_cups_printers() -> list[PrinterInfo]:
    if _is_windows():
        return []
    try:
        completed = _cups_command("lpstat", "-v")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    printers: list[PrinterInfo] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line.lower().startswith("device for ") or ":" not in line:
            continue
        left, port = line.split(":", 1)
        name = left[len("device for "):].strip()
        port = port.strip()
        if name:
            printers.append(PrinterInfo(
                name=name,
                port=port,
                driver="CUPS",
                is_virtual=_is_virtual_printer(name, port),
            ))
    return sorted(printers, key=lambda item: (item.is_virtual, item.name.casefold()))


def list_windows_printers() -> list[PrinterInfo]:
    if not _is_windows():
        return []
    try:
        import win32print

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        records = win32print.EnumPrinters(flags, None, 2)
    except Exception:
        return []

    printers: list[PrinterInfo] = []
    seen: set[str] = set()
    for record in records:
        if isinstance(record, dict):
            name = str(record.get("pPrinterName") or "").strip()
            port = str(record.get("pPortName") or "").strip()
            driver = str(record.get("pDriverName") or "").strip()
        else:
            name = str(record[2] if len(record) > 2 else "").strip()
            port = ""
            driver = ""
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        printers.append(PrinterInfo(
            name=name,
            port=port,
            driver=driver,
            is_virtual=_is_virtual_printer(name, port),
        ))
    return sorted(printers, key=lambda item: (item.is_virtual, item.name.casefold()))


def default_windows_printer() -> str:
    if not _is_windows():
        return ""
    try:
        import win32print

        return str(win32print.GetDefaultPrinter() or "").strip()
    except Exception:
        return ""


def default_cups_printer() -> str:
    if _is_windows():
        return ""
    try:
        completed = _cups_command("lpstat", "-d")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    marker = ":"
    return completed.stdout.split(marker, 1)[1].strip() if marker in completed.stdout else ""


def list_printers() -> list[PrinterInfo]:
    return list_windows_printers() if _is_windows() else list_cups_printers()


def default_printer() -> str:
    return default_windows_printer() if _is_windows() else default_cups_printer()


def preferred_windows_printer(configured_name: str = "") -> str:
    """Izberi fizični POS tiskalnik brez poseganja uporabnika.

    Vrstni red je: veljavna ročna nastavitev, Epson TM-T20, fizični
    privzeti tiskalnik in nazadnje edini fizični tiskalnik. S tem se PDF
    nikoli samodejno ne pošlje v Microsoft Print to PDF ali XPS.
    """
    printers = list_windows_printers()
    physical = [item for item in printers if not item.is_virtual]
    configured = str(configured_name or "").strip()
    if configured:
        match = next(
            (item for item in physical if item.name.casefold() == configured.casefold()),
            None,
        )
        if match:
            return match.name

    epson = next(
        (
            item for item in physical
            if "epson" in f"{item.name} {item.driver}".casefold()
            and "tm-t20" in f"{item.name} {item.driver}".casefold()
        ),
        None,
    )
    if epson:
        return epson.name

    default_name = default_windows_printer()
    default_match = next(
        (item for item in physical if item.name.casefold() == default_name.casefold()),
        None,
    )
    if default_match:
        return default_match.name
    return physical[0].name if len(physical) == 1 else ""


def preferred_printer(configured_name: str = "") -> str:
    """Izbere nastavljen oziroma najprimernejši fizični POS tiskalnik."""
    if _is_windows():
        return preferred_windows_printer(configured_name)
    printers = list_cups_printers()
    physical = [item for item in printers if not item.is_virtual]
    configured = str(configured_name or "").strip()
    if configured:
        match = next(
            (item for item in physical if item.name.casefold() == configured.casefold()),
            None,
        )
        if match:
            return match.name
    epson = next(
        (
            item for item in physical
            if "epson" in f"{item.name} {item.port}".casefold()
            and "tm" in f"{item.name} {item.port}".casefold()
            and "t20" in f"{item.name} {item.port}".casefold()
        ),
        None,
    )
    if epson:
        return epson.name
    default_name = default_cups_printer()
    default_match = next(
        (item for item in physical if item.name.casefold() == default_name.casefold()),
        None,
    )
    if default_match:
        return default_match.name
    return physical[0].name if len(physical) == 1 else ""


def preferred_receipt_printer(configured_name: str = "") -> str:
    """Vrne termalni tiskalnik; A4 laserja ne izbere kot nadomestilo."""
    printers = [item for item in list_printers() if not item.is_virtual]
    configured = str(configured_name or "").strip()
    if configured:
        match = next(
            (item for item in printers if item.name.casefold() == configured.casefold()),
            None,
        )
        if match:
            return match.name
    thermal = next(
        (
            item for item in printers
            if any(marker in f"{item.name} {item.port} {item.driver}".casefold()
                   for marker in ("tm-t20", "receipt", "thermal", "termal"))
        ),
        None,
    )
    return thermal.name if thermal else ""


def preferred_a4_printer(configured_name: str = "") -> str:
    """Vrne A4 tiskalnik; termalne vrste ne izbere kot nadomestilo."""
    printers = [item for item in list_printers() if not item.is_virtual]
    configured = str(configured_name or "").strip()
    if configured:
        match = next(
            (item for item in printers if item.name.casefold() == configured.casefold()),
            None,
        )
        if match:
            return match.name
    a4 = next(
        (
            item for item in printers
            if any(marker in f"{item.name} {item.port} {item.driver}".casefold()
                   for marker in ("laserjet", "laser", "officejet", "deskjet"))
        ),
        None,
    )
    return a4.name if a4 else ""


def _wait_for_spool_job(printer_name: str, job_id: int, timeout: float = 20.0) -> PrintResult:
    import win32print

    error_flags = (
        win32print.JOB_STATUS_ERROR
        | win32print.JOB_STATUS_OFFLINE
        | win32print.JOB_STATUS_PAPEROUT
        | win32print.JOB_STATUS_BLOCKED_DEVQ
        | win32print.JOB_STATUS_USER_INTERVENTION
    )
    deadline = time.monotonic() + max(1.0, float(timeout))
    handle = win32print.OpenPrinter(printer_name)
    try:
        while time.monotonic() < deadline:
            try:
                info = win32print.GetJob(handle, int(job_id), 1)
            except Exception:
                # Windows praviloma odstrani uspešno dokončano opravilo iz čakalne vrste.
                return PrintResult(printer_name, int(job_id), True, "Tiskalniško opravilo je zaključeno.")
            status = int(info.get("Status") or 0)
            if status & error_flags:
                detail = str(info.get("pStatus") or "Napaka tiskalnika.")
                raise RuntimeError(detail)
            if status & win32print.JOB_STATUS_PRINTED:
                return PrintResult(printer_name, int(job_id), True, "Tiskalnik je potrdil izpis.")
            time.sleep(0.25)
    finally:
        win32print.ClosePrinter(handle)
    raise RuntimeError(
        "Tiskalniško opravilo je bilo poslano, vendar tiskalnik v 20 sekundah ni potrdil zaključka."
    )


def send_pdf_to_windows_printer(
    pdf_path: Path | str, printer_name: str, *, timeout: float = 20.0
) -> PrintResult:
    """PDF izriše prek gonilnika Windows in počaka na rezultat tiskalne vrste."""
    if not _is_windows():
        raise RuntimeError("Neposredni tisk je v tej izdaji podprt samo na Windows.")
    path = Path(pdf_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    printer_name = str(printer_name or "").strip()
    if not printer_name:
        raise RuntimeError("Tiskalnik ni izbran.")

    try:
        import fitz
        import win32con
        import win32ui
        from PIL import Image, ImageWin
    except Exception as exc:
        raise RuntimeError(
            "Za neposredni tisk manjka komponenta PyMuPDF. Zaženi namestitev dodatkov v30."
        ) from exc

    dc = win32ui.CreateDC()
    document = fitz.open(str(path))
    job_id = 0
    try:
        dc.CreatePrinterDC(printer_name)
        printable_width = int(dc.GetDeviceCaps(win32con.HORZRES))
        printable_height = int(dc.GetDeviceCaps(win32con.VERTRES))
        dpi_x = max(96, int(dc.GetDeviceCaps(win32con.LOGPIXELSX)))
        dpi_y = max(96, int(dc.GetDeviceCaps(win32con.LOGPIXELSY)))
        raw_job_id = dc.StartDoc(path.name)
        # Nekateri Windows/GDI gonilniki (tudi določene izdaje Epson APD)
        # uspešno začnejo opravilo, vendar vrnejo None namesto številke.
        # Tega ne smemo pretvoriti z int(None), ker je PDF že lahko v tisku.
        job_id = int(raw_job_id) if raw_job_id is not None else 0
        for page in document:
            zoom = max(dpi_x, dpi_y) / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            scale = min(
                printable_width / max(1, image.width),
                printable_height / max(1, image.height),
            )
            width = max(1, int(image.width * scale))
            height = max(1, int(image.height * scale))
            left = max(0, (printable_width - width) // 2)
            dc.StartPage()
            ImageWin.Dib(image).draw(dc.GetHandleOutput(), (left, 0, left + width, height))
            dc.EndPage()
        dc.EndDoc()
    except Exception:
        try:
            dc.AbortDoc()
        except Exception:
            pass
        raise
    finally:
        document.close()
        dc.DeleteDC()
    if job_id <= 0:
        return PrintResult(
            printer_name,
            0,
            False,
            "Windows je sprejel izpis, vendar gonilnik ni vrnil številke opravila.",
        )
    return _wait_for_spool_job(printer_name, job_id, timeout=timeout)


def _wait_for_cups_job(
    printer_name: str, request_id: str, job_id: int, *, timeout: float
) -> PrintResult:
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        pending = _cups_command(
            "lpstat", "-W", "not-completed", "-o", printer_name, timeout=5.0
        )
        if pending.returncode == 0 and request_id in pending.stdout:
            status = _cups_command("lpstat", "-p", printer_name, timeout=5.0)
            detail = f"{status.stdout}\n{status.stderr}".lower()
            if "disabled" in detail or "not accepting" in detail:
                raise RuntimeError(status.stdout.strip() or status.stderr.strip())
            time.sleep(0.25)
            continue
        completed = _cups_command(
            "lpstat", "-W", "completed", "-o", printer_name, timeout=5.0
        )
        if completed.returncode == 0 and request_id in completed.stdout:
            return PrintResult(
                printer_name, job_id, True, "CUPS je potrdil zaključek tiskalnega opravila."
            )
        # Med odstranitvijo iz aktivne vrste in vpisom v zgodovino je lahko
        # kratko okno, ko opravila ne vrne nobena poizvedba.
        time.sleep(0.25)
        continue
    raise RuntimeError(
        "CUPS je sprejel opravilo, vendar tiskalnik v predvidenem času ni potrdil zaključka."
    )


def _is_thermal_printer(printer_name: str) -> bool:
    value = str(printer_name or "").casefold()
    return any(marker in value for marker in ("tm-t20", "receipt", "thermal", "termal"))


def _image_to_escpos_raster(image) -> bytes:
    """Pretvori Pillow sliko v ESC/POS GS v 0 raster (MSB najprej)."""
    from PIL import Image, ImageOps

    grayscale = ImageOps.autocontrast(image.convert("L"))
    # Pillow stores white pixels as 1, while ESC/POS prints a dot for bit 1.
    # Invert before packing so white paper stays unprinted and black text prints.
    mono = ImageOps.invert(grayscale).convert("1")
    width, height = mono.size
    padded_width = ((width + 7) // 8) * 8
    if padded_width != width:
        padded = Image.new("1", (padded_width, height), 1)
        padded.paste(mono, (0, 0))
        mono = padded
        width = padded_width
    width_bytes = width // 8
    header = bytes((
        0x1D, 0x76, 0x30, 0x00,
        width_bytes & 0xFF, (width_bytes >> 8) & 0xFF,
        height & 0xFF, (height >> 8) & 0xFF,
    ))
    return header + mono.tobytes()


def _pdf_to_escpos(pdf_path: Path) -> bytes:
    """Izriše PDF na 576 pik (80 mm / 203 dpi) in izdela surov Epson ESC/POS."""
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Za termalni tisk manjka knjižnica Pillow.") from exc
    renderer = shutil.which("pdftoppm")
    if not renderer:
        raise RuntimeError("Za termalni tisk manjka Popplerjev ukaz pdftoppm.")
    with tempfile.TemporaryDirectory(prefix="mikrofix-escpos-") as raw:
        prefix = Path(raw) / "page"
        completed = subprocess.run(
            [
                renderer, "-png", "-r", "203", "-scale-to-x", "576",
                "-scale-to-y", "-1", str(pdf_path), str(prefix),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                completed.stderr.strip() or "80-mm PDF-ja ni bilo mogoče izrisati za Epson."
            )
        pages = sorted(Path(raw).glob("page-*.png"))
        if not pages:
            raise RuntimeError("Iz PDF-ja ni nastala nobena stran za termalni tisk.")
        output = bytearray(b"\x1b@\x1ba\x01")
        for page in pages:
            with Image.open(page) as image:
                output.extend(_image_to_escpos_raster(image))
            output.extend(b"\n")
        output.extend(b"\n\n\n\x1dV\x00")
        return bytes(output)


def _submit_cups_file(
    path: Path, printer_name: str, *, timeout: float, raw: bool = False,
) -> PrintResult:
    arguments = ["lp", "-d", printer_name, "-t", path.name]
    if raw:
        arguments.extend(["-o", "raw"])
    arguments.extend(["--", str(path)])
    submitted = _cups_command(*arguments, timeout=15.0)
    if submitted.returncode != 0:
        raise RuntimeError(submitted.stderr.strip() or submitted.stdout.strip())
    match = re.search(r"request id is\s+([^\s]+)-(\d+)", submitted.stdout, re.I)
    if not match:
        return PrintResult(
            printer_name, 0, False,
            "CUPS je sprejel izpis, vendar ni vrnil številke opravila.",
        )
    request_id = f"{match.group(1)}-{match.group(2)}"
    return _wait_for_cups_job(
        printer_name, request_id, int(match.group(2)), timeout=timeout
    )


def send_pdf_to_cups_printer(
    pdf_path: Path | str, printer_name: str, *, timeout: float = 20.0
) -> PrintResult:
    if _is_windows():
        raise RuntimeError("CUPS tisk ni namenjen okolju Windows.")
    path = Path(pdf_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    printer_name = str(printer_name or "").strip()
    if not printer_name:
        raise RuntimeError("Tiskalnik ni izbran.")
    available = {item.name for item in list_cups_printers() if not item.is_virtual}
    if printer_name not in available:
        raise RuntimeError(f"CUPS tiskalnik ni dosegljiv: {printer_name}")
    if _is_thermal_printer(printer_name):
        payload = _pdf_to_escpos(path)
        with tempfile.TemporaryDirectory(prefix="mikrofix-print-") as raw:
            raster_path = Path(raw) / "receipt.escpos"
            raster_path.write_bytes(payload)
            return _submit_cups_file(
                raster_path, printer_name, timeout=timeout, raw=True
            )
    return _submit_cups_file(path, printer_name, timeout=timeout)


def send_pdf_to_printer(
    pdf_path: Path | str, printer_name: str, *, timeout: float = 20.0
) -> PrintResult:
    if _is_windows():
        return send_pdf_to_windows_printer(pdf_path, printer_name, timeout=timeout)
    return send_pdf_to_cups_printer(pdf_path, printer_name, timeout=timeout)
