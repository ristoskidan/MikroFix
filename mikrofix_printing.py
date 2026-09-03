"""Windows pomočniki za zaznavo tiskalnikov in tiskanje že izdelanega PDF-ja."""

from __future__ import annotations

import os
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


def _is_virtual_printer(name: str, port: str) -> bool:
    text = f"{name} {port}".lower()
    return any(marker in text for marker in (
        "microsoft print to pdf", "xps", "fax", "onenote", "portprompt",
    ))


def list_windows_printers() -> list[PrinterInfo]:
    if os.name != "nt":
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
    if os.name != "nt":
        return ""
    try:
        import win32print

        return str(win32print.GetDefaultPrinter() or "").strip()
    except Exception:
        return ""


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
    if os.name != "nt":
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
