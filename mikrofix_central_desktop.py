"""Standalone API-only desktop shell for MikroFIX Office and Cashier."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from mikrofix_central_auth_client import RequestsCentralAuthClient
from mikrofix_central_cashier_bootstrap import build_central_cashier
from mikrofix_central_identity import CentralApplicationIdentityManager
from mikrofix_central_office_bootstrap import build_central_office
from mikrofix_runtime import load_runtime


APP_DIR = Path(__file__).resolve().parent


class CentralDesktopConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CentralDesktopConfiguration:
    application_type: str
    server_url: str
    workstation_code: str
    workstation_secret: str


_APPLICATION_ENVIRONMENT = {
    "OFFICE": (
        "MIKROFIX_OFFICE_WORKSTATION_CODE",
        "MIKROFIX_OFFICE_WORKSTATION_SECRET",
    ),
    "CASHIER": (
        "MIKROFIX_CASHIER_WORKSTATION_CODE",
        "MIKROFIX_CASHIER_WORKSTATION_SECRET",
    ),
}


def central_desktop_configuration(
    settings: Mapping[str, object],
    application_type: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> CentralDesktopConfiguration:
    application = str(application_type or "").strip().upper()
    if application not in _APPLICATION_ENVIRONMENT:
        raise CentralDesktopConfigurationError(
            "Samostojni centralni ovoj podpira samo Office in Blagajno."
        )
    database = settings.get("database") or {}
    if not isinstance(database, Mapping):
        database = {}
    server_url = str(database.get("server_url") or "").strip().rstrip("/")
    if not server_url:
        raise CentralDesktopConfigurationError(
            "Centralni strežnik ni nastavljen. Program zaradi varnosti ne bo "
            "odprl stare lokalne podatkovne poti."
        )
    source = os.environ if environment is None else environment
    code_name, secret_name = _APPLICATION_ENVIRONMENT[application]
    workstation_code = str(source.get(code_name) or "").strip().upper()
    workstation_secret = str(source.get(secret_name) or "").strip()
    missing = [
        name
        for name, value in (
            (code_name, workstation_code),
            (secret_name, workstation_secret),
        )
        if not value
    ]
    if missing:
        raise CentralDesktopConfigurationError(
            "Manjka konfiguracija centralne delovne postaje: " + ", ".join(missing)
        )
    return CentralDesktopConfiguration(
        application_type=application,
        server_url=server_url,
        workstation_code=workstation_code,
        workstation_secret=workstation_secret,
    )


def _prompt_new_pin(parent, *, title: str) -> str | None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    result: dict[str, str | None] = {"pin": None}
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.grab_set()
    first = tk.StringVar()
    second = tk.StringVar()
    frame = ttk.Frame(dialog, padding=18)
    frame.grid(sticky="nsew")
    ttk.Label(
        frame,
        text="Pred nadaljevanjem moraš določiti nov PIN.",
        wraplength=360,
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
    ttk.Label(frame, text="Novi PIN").grid(row=1, column=0, sticky="w", pady=4)
    first_entry = ttk.Entry(frame, textvariable=first, show="•", width=28)
    first_entry.grid(row=1, column=1, pady=4)
    ttk.Label(frame, text="Ponovi novi PIN").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(frame, textvariable=second, show="•", width=28).grid(
        row=2, column=1, pady=4
    )

    def accept(_event=None):
        if not first.get() or first.get() != second.get():
            messagebox.showwarning(
                title, "Vpisana nova PIN-a se ne ujemata.", parent=dialog
            )
            return
        result["pin"] = first.get()
        first.set("")
        second.set("")
        dialog.destroy()

    def cancel():
        first.set("")
        second.set("")
        dialog.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))
    ttk.Button(buttons, text="Prekliči", command=cancel).pack(side="right")
    ttk.Button(buttons, text="Spremeni PIN", command=accept).pack(
        side="right", padx=(0, 8)
    )
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    dialog.bind("<Return>", accept)
    dialog.bind("<Escape>", lambda _event: cancel())
    first_entry.focus_set()
    parent.wait_window(dialog)
    return result["pin"]


def prompt_central_login(parent, manager: CentralApplicationIdentityManager):
    import tkinter as tk
    from tkinter import messagebox, ttk

    application_label = "Office" if manager.application_type == "OFFICE" else "Blagajna"
    title = f"MikroFIX {application_label} – prijava"
    result = {"identity": None}
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.grab_set()
    username = tk.StringVar()
    pin = tk.StringVar()
    frame = ttk.Frame(dialog, padding=20)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text=application_label, font=("Segoe UI", 17, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
    )
    ttk.Label(frame, text="Uporabniško ime").grid(row=1, column=0, sticky="w", pady=4)
    username_entry = ttk.Entry(frame, textvariable=username, width=30)
    username_entry.grid(row=1, column=1, pady=4)
    ttk.Label(frame, text="PIN").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(frame, textvariable=pin, show="•", width=30).grid(
        row=2, column=1, pady=4
    )
    submit_button = ttk.Button(frame, text="Prijava")
    submit_button.grid(row=3, column=1, sticky="e", pady=(14, 0))

    def submit(_event=None):
        submit_button.configure(state="disabled")
        current_pin = pin.get()
        try:
            identity = manager.login(username=username.get(), pin=current_pin)
            if manager.must_change_pin:
                new_pin = _prompt_new_pin(dialog, title=title)
                if new_pin is None:
                    manager.end_session()
                    return
                identity = manager.change_pin_and_relogin(
                    username=identity.username,
                    current_pin=current_pin,
                    new_pin=new_pin,
                )
            result["identity"] = identity
            pin.set("")
            dialog.destroy()
        except Exception as exc:
            pin.set("")
            messagebox.showerror(
                title,
                "Prijava ni uspela. Preveri uporabnika, PIN, povezavo in "
                f"registracijo postaje.\n\n{exc}",
                parent=dialog,
            )
        finally:
            if dialog.winfo_exists():
                submit_button.configure(state="normal")

    def cancel():
        pin.set("")
        manager.end_session()
        dialog.destroy()

    submit_button.configure(command=submit)
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    dialog.bind("<Return>", submit)
    dialog.bind("<Escape>", lambda _event: cancel())
    username_entry.focus_set()
    parent.wait_window(dialog)
    return result["identity"]


def run(application_type: str) -> int:
    from tkinter import messagebox

    from mikrofix_central_office_ui import open_central_office
    from mikrofix_tk_bootstrap import prepare_tcl_tk
    from mikrofix_v30_pos_ui import open_pos

    prepare_tcl_tk()
    import tkinter as tk

    runtime = load_runtime(APP_DIR)
    root = tk.Tk()
    root.withdraw()
    try:
        configuration = central_desktop_configuration(
            runtime.settings, application_type
        )
        identity_manager = CentralApplicationIdentityManager(
            RequestsCentralAuthClient(configuration.server_url),
            workstation_code=configuration.workstation_code,
            workstation_secret=configuration.workstation_secret,
            application_type=configuration.application_type,
        )
    except Exception as exc:
        messagebox.showerror("MikroFIX – zagon zavrnjen", str(exc), parent=root)
        root.destroy()
        return 1

    identity = prompt_central_login(root, identity_manager)
    if identity is None:
        root.destroy()
        return 0

    handle = None
    closing = False
    session_expiry_scheduled = False

    def close_application():
        nonlocal handle, closing
        if closing:
            return
        closing = True
        try:
            if handle is not None:
                handle.close()
                handle = None
        finally:
            try:
                identity_manager.end_session()
            finally:
                if root.winfo_exists():
                    root.destroy()

    def on_session_expired():
        nonlocal session_expiry_scheduled
        if closing or session_expiry_scheduled:
            return
        session_expiry_scheduled = True
        root.after_idle(close_application)

    try:
        if configuration.application_type == "OFFICE":
            handle = build_central_office(
                settings=runtime.settings,
                office_db=runtime.db_path("office"),
                actor_id=identity.username,
                device_id=identity.workstation_code,
                session=identity_manager.session,
                on_session_expired=on_session_expired,
            )
            open_central_office(
                root,
                workflow=handle.workflow,
                username=identity.username,
                initial_tab="inventory",
                on_close=close_application,
            )
        else:
            handle = build_central_cashier(
                settings=runtime.settings,
                office_db=runtime.db_path("office"),
                actor_id=identity.username,
                device_id=identity.workstation_code,
                session=identity_manager.session,
                on_session_expired=on_session_expired,
            )
            pdf_dir = runtime.office_data_dir / "pdf"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            open_pos(
                root,
                runtime=runtime,
                office_db=runtime.db_path("office"),
                inventory_db=runtime.db_path("inventory"),
                service_db=runtime.db_path("service"),
                pdf_dir=pdf_dir,
                operator_code=runtime.operator_code,
                authenticated_username=identity.username,
                action_permissions=identity_manager.session.permissions,
                central_cashier=handle.workflow,
                on_close=close_application,
            )
        root.mainloop()
    except Exception as exc:
        messagebox.showerror(
            "MikroFIX", f"Programa ni bilo mogoče odpreti.\n\n{exc}", parent=root
        )
        close_application()
        return 1
    finally:
        close_application()
    return 0


def main(arguments: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    try:
        index = values.index("--application")
        application = values[index + 1]
    except (ValueError, IndexError):
        raise SystemExit("Manjka --application OFFICE|CASHIER")
    return run(application)


if __name__ == "__main__":
    raise SystemExit(main())
