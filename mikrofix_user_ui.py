"""Skupni prijavni in uporabniški vmesnik za MikroFIX Service in Office."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from mikrofix_auth import (
    ALL_PERMISSIONS,
    AuthStore,
    DEFAULT_DISPLAY_NAME,
    DEFAULT_ROLE,
    DEFAULT_USERNAME,
    PERMISSION_BACKUP_RESTORE,
    PERMISSION_BUYBACK,
    PERMISSION_CASHIER,
    PERMISSION_CONFIRM_DOCUMENT,
    PERMISSION_FURS_PRODUCTION,
    PERMISSION_MANAGE_INVENTORY,
    PERMISSION_MANAGE_PRICES,
    PERMISSION_MANAGE_USERS,
    PERMISSION_OFFICE,
    PERMISSION_SERVICE,
    PERMISSION_STORNO_DOCUMENT,
    validate_pin_format,
)


ROLE_LABELS = {
    "ADMIN": "Skrbnik",
    "SERVICE": "Servis",
    "OFFICE": "Pisarna",
    "BUYBACK": "Odkup in obnova",
    "CASHIER": "Blagajna",
}
ROLE_BY_LABEL = {label: role for role, label in ROLE_LABELS.items()}

PERMISSION_LABELS = {
    PERMISSION_SERVICE: "Modul Servis",
    PERMISSION_CASHIER: "Modul Prodaja / Blagajna",
    PERMISSION_OFFICE: "Modul Office",
    PERMISSION_BUYBACK: "Modul Odkup in obnova",
    PERMISSION_CONFIRM_DOCUMENT: "Potrjevanje dokumentov",
    PERMISSION_STORNO_DOCUMENT: "Storno potrjenih dokumentov",
    PERMISSION_MANAGE_PRICES: "Cene in cenovni profili",
    PERMISSION_MANAGE_INVENTORY: "Inventura in zalogovni dokumenti",
    PERMISSION_FURS_PRODUCTION: "Produkcijske funkcije FURS",
    PERMISSION_BACKUP_RESTORE: "Varnostne kopije in obnova",
    PERMISSION_MANAGE_USERS: "Uporabniki in dovoljenja",
}


class SessionAuthUI:
    """Hrani trenutno sejo in nudi enak uporabniški vmesnik v obeh programih."""

    def __init__(
        self,
        store_factory: Callable[[], AuthStore],
        *,
        version_label: str,
        login_context: str,
        bg: str,
        panel: str,
        blue: str,
        gray: str,
        dark: str,
        light_gray: str,
        danger: str,
    ) -> None:
        self.store_factory = store_factory
        self.version_label = version_label
        self.login_context = login_context
        self.bg = bg
        self.panel = panel
        self.blue = blue
        self.gray = gray
        self.dark = dark
        self.light_gray = light_gray
        self.danger = danger
        self.current = {
            "username": DEFAULT_USERNAME,
            "display_name": DEFAULT_DISPLAY_NAME,
            "role": DEFAULT_ROLE,
            "is_active": 1,
            "permissions": set(),
        }
        self._caption_vars: list[tk.StringVar] = []
        self._admin_widgets: list[tk.Widget] = []

    @property
    def username(self) -> str:
        return str(self.current["username"])

    @property
    def display_name(self) -> str:
        return str(self.current["display_name"])

    @property
    def role(self) -> str:
        return str(self.current["role"])

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    @property
    def permissions(self) -> set[str]:
        return set(self.current.get("permissions") or set())

    def has_permission(self, permission: str) -> bool:
        return str(permission or "").strip().upper() in self.permissions

    def caption(self) -> str:
        return f"{self.display_name} • {ROLE_LABELS.get(self.role, self.role)}"

    def bind_caption_variable(self, variable: tk.StringVar) -> None:
        self._caption_vars.append(variable)
        variable.set(self.caption())

    def bind_admin_widget(self, widget: tk.Widget) -> None:
        self._admin_widgets.append(widget)
        self._refresh_bound_widgets()

    def _set_current(self, user: dict) -> None:
        current = dict(user)
        current["permissions"] = self.store_factory().permissions_for(
            str(current["username"])
        )
        self.current = current
        self._refresh_bound_widgets()

    def adopt_authenticated_user(self, user: dict) -> None:
        """Prevzame uporabnika iz preverjene enkratne predaje lokalne seje."""
        required = {"username", "display_name", "role", "is_active"}
        if not required.issubset(user) or not bool(user["is_active"]):
            raise ValueError("Predana uporabniška seja ni veljavna.")
        self._set_current(user)

    def _refresh_bound_widgets(self) -> None:
        for variable in self._caption_vars:
            variable.set(self.caption())
        for widget in self._admin_widgets:
            try:
                widget.configure(state="normal" if self.is_admin else "disabled")
            except tk.TclError:
                pass

    def _pin_entry(self, parent, variable, *, width=20):
        def validate(proposed):
            return proposed == "" or (proposed.isdigit() and len(proposed) <= 14)

        vcmd = (parent.register(validate), "%P")
        return ttk.Entry(
            parent,
            textvariable=variable,
            show="•",
            justify="center",
            font=("Segoe UI", 18, "bold"),
            validate="key",
            validatecommand=vcmd,
            width=width,
        )

    @staticmethod
    def _center(win: tk.Toplevel) -> None:
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def login(self, parent, *, username_only: str | None = None) -> bool:
        """Modalna lokalna PIN prijava. Vrne True samo po uspešni prijavi."""
        store = self.store_factory()
        store.init()
        users = store.list_users(active_only=True)
        if username_only is not None:
            required_username = str(username_only).strip().lower()
            users = [
                user for user in users
                if str(user["username"]).lower() == required_username
            ]
        if not users:
            messagebox.showerror(
                "MikroFIX",
                "Zahtevani uporabnik ni več aktiven.",
                parent=parent,
            )
            return False

        initial_setup = (
            len(users) == 1
            and users[0]["username"] == DEFAULT_USERNAME
            and not store.has_pin(DEFAULT_USERNAME)
        )
        by_label = {
            f"{user['display_name']} ({user['username']})": user for user in users
        }
        labels = list(by_label)
        preferred_label = next(
            (
                label
                for label, user in by_label.items()
                if user["username"] == self.username
            ),
            labels[0],
        )
        result = {"ok": False}

        win = tk.Toplevel(parent)
        win.title(f"MikroFIX {self.version_label} - Prijava")
        win.geometry("540x565" if initial_setup else "540x500")
        win.resizable(False, False)
        win.configure(bg=self.bg)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        # Prijava zaklene samo MikroFIX. Okno ne sme ostati globalno nad
        # drugimi programi Windows in uporabnik ga mora smeti minimizirati.

        card = tk.Frame(
            win,
            bg=self.panel,
            highlightthickness=1,
            highlightbackground=self.light_gray,
        )
        card.pack(fill="both", expand=True, padx=28, pady=28)

        brand = tk.Frame(card, bg=self.panel)
        brand.pack(pady=(24, 6))
        tk.Label(
            brand,
            text="Mikro",
            bg=self.panel,
            fg=self.gray,
            font=("Segoe UI", 27, "bold"),
        ).pack(side="left")
        tk.Label(
            brand,
            text="FIX",
            bg=self.panel,
            fg=self.blue,
            font=("Segoe UI", 27, "bold"),
        ).pack(side="left")

        title_var = tk.StringVar()
        help_var = tk.StringVar()
        tk.Label(
            card,
            textvariable=title_var,
            bg=self.panel,
            fg=self.dark,
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(10, 4))
        tk.Label(
            card,
            textvariable=help_var,
            bg=self.panel,
            fg=self.gray,
            font=("Segoe UI", 9),
        ).pack(pady=(0, 12))

        user_var = tk.StringVar(value=preferred_label)
        show_user_picker = not initial_setup and len(users) > 1
        if show_user_picker:
            tk.Label(
                card,
                text="Uporabnik",
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(pady=(2, 5))
            user_combo = ttk.Combobox(
                card,
                textvariable=user_var,
                values=labels,
                state="readonly",
                width=34,
                justify="center",
            )
            user_combo.pack(ipady=4)
        else:
            user_combo = None

        def selected_user():
            return by_label.get(user_var.get(), users[0])

        def refresh_text(_event=None):
            user = selected_user()
            if initial_setup:
                title_var.set(f"Nastavi PIN za uporabnika {user['display_name']}")
                help_var.set("PIN mora imeti 6–14 številk in ne sme biti preprost.")
            else:
                title_var.set(f"{user['display_name']} – prijava v {self.login_context}")
                help_var.set("Vnesi svoj 6–14 mestni številčni PIN.")

        refresh_text()
        if user_combo is not None:
            user_combo.bind("<<ComboboxSelected>>", refresh_text)

        pin_var = tk.StringVar()
        pin_entry = self._pin_entry(card, pin_var)
        pin_entry.pack(ipady=7, padx=70, pady=(14, 0))

        confirm_var = tk.StringVar()
        confirm_entry = None
        if initial_setup:
            tk.Label(
                card,
                text="Ponovi PIN",
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(pady=(16, 5))
            confirm_entry = self._pin_entry(card, confirm_var)
            confirm_entry.pack(ipady=7, padx=70)

        message_var = tk.StringVar(value="")
        tk.Label(
            card,
            textvariable=message_var,
            bg=self.panel,
            fg=self.danger,
            font=("Segoe UI", 9),
        ).pack(pady=(12, 0))

        def submit(_event=None):
            user = selected_user()
            username = str(user["username"])
            pin = pin_var.get()
            if not validate_pin_format(pin):
                message_var.set(
                    "PIN mora vsebovati 6 do 14 številk in ne sme biti preprost."
                )
                return

            if initial_setup:
                if pin != confirm_var.get():
                    message_var.set("PIN-a se ne ujemata.")
                    return
                store.set_pin(pin, username, event_type="PIN_SET")
                if not self._configure_admin_recovery(win, required=True):
                    pin_var.set("")
                    confirm_var.set("")
                    message_var.set(
                        "Pred nadaljevanjem nastavi obnovitveni namig in geslo."
                    )
                    return
                self._set_current(user)
                result["ok"] = True
                win.destroy()
                return

            if not store.has_pin(username):
                message_var.set("Uporabnik še nima PIN-a. Nastavi ga mora skrbnik.")
                return

            login = store.verify_pin(pin, username)
            if login.ok:
                if login.must_change_pin:
                    new_pin = self._ask_new_pin(
                        win,
                        "MikroFIX - Obvezna menjava PIN-a",
                        "Začasni PIN zamenjaj s svojim novim PIN-om.",
                    )
                    if new_pin is None:
                        pin_var.set("")
                        message_var.set("Pred nadaljevanjem moraš določiti novi PIN.")
                        pin_entry.focus_set()
                        return
                    store.set_pin(
                        new_pin,
                        username,
                        event_type="PIN_CHANGED_FIRST_LOGIN",
                        changed_by=username,
                    )
                if (
                    username == DEFAULT_USERNAME
                    and not store.has_recovery(DEFAULT_USERNAME)
                    and not self._configure_admin_recovery(win, required=True)
                ):
                    pin_var.set("")
                    message_var.set(
                        "Pred nadaljevanjem nastavi obnovitveni namig in geslo."
                    )
                    return
                self._set_current(user)
                result["ok"] = True
                win.destroy()
                return

            pin_var.set("")
            if login.reason == "LOCKED":
                messagebox.showerror(
                    "MikroFIX",
                    "Prijava je zaradi napačnih poskusov začasno zaklenjena.\n"
                    f"Ponovno bo na voljo po: {login.locked_until or '-'}",
                    parent=win,
                )
                win.destroy()
                return
            message_var.set(
                f"Napačen PIN. Preostali poskusi: {login.remaining_attempts}"
            )
            pin_entry.focus_set()

        ttk.Button(
            card,
            text="NASTAVI PIN" if initial_setup else "PRIJAVA",
            command=submit,
            style="Blue.TButton",
        ).pack(pady=(18, 8), ipadx=18, ipady=5)

        ttk.Button(
            card,
            text="OBNOVI DOSTOP",
            command=lambda: self._recover_admin_access(
                win, str(selected_user()["username"])
            ),
        ).pack(pady=(0, 10))

        pin_entry.bind("<Return>", submit)
        if confirm_entry:
            confirm_entry.bind("<Return>", submit)

        self._center(win)
        pin_entry.focus_set()
        win.grab_set()
        parent.wait_window(win)
        return bool(result["ok"])

    def _configure_admin_recovery(self, parent, *, required: bool) -> bool:
        store = self.store_factory()
        result = {"ok": False}
        win = tk.Toplevel(parent)
        win.title("MikroFIX - Obnovitveni dostop")
        win.geometry("520x500")
        win.resizable(False, False)
        win.configure(bg=self.bg)
        win.transient(parent)
        win.grab_set()

        def close():
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        outer = tk.Frame(win, bg=self.panel)
        outer.pack(fill="both", expand=True, padx=24, pady=24)
        tk.Label(
            outer,
            text="Obnovitveni dostop skrbnika",
            bg=self.panel,
            fg=self.dark,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            outer,
            text=(
                "Namig je viden v obnovitvenem oknu. Geslo naj bo drugačno "
                "od PIN-a in ga program ne more prikazati ali povrniti."
            ),
            bg=self.panel,
            fg=self.gray,
            font=("Segoe UI", 9),
            wraplength=450,
            justify="left",
        ).pack(anchor="w", pady=(4, 14))

        hint_var = tk.StringVar()
        password_var = tk.StringVar()
        confirm_var = tk.StringVar()
        for label, variable, masked in (
            ("Obnovitveni namig", hint_var, False),
            ("Obnovitveno geslo", password_var, True),
            ("Ponovi obnovitveno geslo", confirm_var, True),
        ):
            tk.Label(
                outer,
                text=label,
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(7, 4))
            ttk.Entry(
                outer,
                textvariable=variable,
                show="•" if masked else "",
            ).pack(fill="x", ipady=4)

        def save():
            if password_var.get() != confirm_var.get():
                messagebox.showwarning(
                    "MikroFIX", "Obnovitveni gesli se ne ujemata.", parent=win
                )
                return
            try:
                store.set_recovery(
                    hint_var.get(),
                    password_var.get(),
                    DEFAULT_USERNAME,
                    changed_by=DEFAULT_USERNAME,
                )
            except ValueError as exc:
                messagebox.showwarning("MikroFIX", str(exc), parent=win)
                return
            result["ok"] = True
            win.destroy()

        actions = tk.Frame(outer, bg=self.panel)
        actions.pack(fill="x", pady=(20, 0))
        ttk.Button(
            actions,
            text="SHRANI OBNOVITVENI DOSTOP",
            command=save,
            style="Blue.TButton",
        ).pack(side="left")
        if not required:
            ttk.Button(actions, text="PREKLIČI", command=close).pack(side="right")
        self._center(win)
        parent.wait_window(win)
        return bool(result["ok"])

    def _recover_admin_access(self, parent, username: str) -> None:
        if username != DEFAULT_USERNAME:
            messagebox.showinfo(
                "MikroFIX",
                "PIN navadnega uporabnika sledljivo ponastavi skrbnik.",
                parent=parent,
            )
            return
        store = self.store_factory()
        if not store.has_recovery(DEFAULT_USERNAME):
            messagebox.showwarning(
                "MikroFIX",
                "Obnovitveni dostop skrbnika še ni nastavljen.",
                parent=parent,
            )
            return

        win = tk.Toplevel(parent)
        win.title("MikroFIX - Obnovi dostop")
        win.geometry("500x540")
        win.resizable(False, False)
        win.configure(bg=self.bg)
        win.transient(parent)
        win.grab_set()
        outer = tk.Frame(win, bg=self.panel)
        outer.pack(fill="both", expand=True, padx=24, pady=24)
        tk.Label(
            outer,
            text="Obnovi skrbniški dostop",
            bg=self.panel,
            fg=self.dark,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            outer,
            text=f"Namig: {store.recovery_hint(DEFAULT_USERNAME)}",
            bg="#F7F9FA",
            fg=self.dark,
            font=("Segoe UI", 10, "bold"),
            wraplength=430,
            justify="left",
            padx=10,
            pady=10,
        ).pack(fill="x", pady=(12, 10))

        secret_var = tk.StringVar()
        new_pin_var = tk.StringVar()
        confirm_pin_var = tk.StringVar()
        tk.Label(
            outer,
            text="Obnovitveno geslo",
            bg=self.panel,
            fg=self.gray,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(5, 4))
        ttk.Entry(outer, textvariable=secret_var, show="•").pack(fill="x", ipady=4)
        for label, variable in (
            ("Novi PIN", new_pin_var),
            ("Ponovi novi PIN", confirm_pin_var),
        ):
            tk.Label(
                outer,
                text=label,
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(8, 4))
            self._pin_entry(outer, variable).pack(fill="x", ipady=3)

        message_var = tk.StringVar()
        tk.Label(
            outer,
            textvariable=message_var,
            bg=self.panel,
            fg=self.danger,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(10, 0))

        def recover():
            if not validate_pin_format(new_pin_var.get()):
                message_var.set(
                    "Novi PIN mora imeti 6–14 številk in ne sme biti preprost."
                )
                return
            if new_pin_var.get() != confirm_pin_var.get():
                message_var.set("Nova PIN-a se ne ujemata.")
                return
            verification = store.verify_recovery(
                secret_var.get(), DEFAULT_USERNAME
            )
            if not verification.ok:
                secret_var.set("")
                if verification.reason == "LOCKED":
                    message_var.set(
                        "Obnova je zaklenjena do "
                        f"{verification.locked_until or '-'}."
                    )
                else:
                    message_var.set(
                        "Napačno obnovitveno geslo. Preostali poskusi: "
                        f"{verification.remaining_attempts}"
                    )
                return
            store.set_pin(
                new_pin_var.get(),
                DEFAULT_USERNAME,
                event_type="PIN_RECOVERED",
                changed_by=DEFAULT_USERNAME,
            )
            messagebox.showinfo(
                "MikroFIX",
                "PIN je ponastavljen. Prijavi se z novim PIN-om.",
                parent=win,
            )
            win.destroy()

        ttk.Button(
            outer,
            text="OBNOVI IN NASTAVI PIN",
            command=recover,
            style="Blue.TButton",
        ).pack(anchor="w", pady=(14, 0))
        self._center(win)

    def change_pin(self, parent) -> None:
        store = self.store_factory()
        if not store.has_pin(self.username):
            return

        win = tk.Toplevel(parent)
        win.title(f"MikroFIX {self.version_label} - Spremeni PIN")
        win.geometry("480x430")
        win.resizable(False, False)
        win.configure(bg=self.bg)
        win.transient(parent)
        win.grab_set()

        outer = tk.Frame(win, bg=self.panel)
        outer.pack(fill="both", expand=True, padx=24, pady=24)
        tk.Label(
            outer,
            text=f"Spremeni PIN – {self.display_name}",
            bg=self.panel,
            fg=self.dark,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 14))

        old_var = tk.StringVar()
        new_var = tk.StringVar()
        confirm_var = tk.StringVar()
        for label, variable in (
            ("Trenutni PIN", old_var),
            ("Novi PIN", new_var),
            ("Ponovi novi PIN", confirm_var),
        ):
            tk.Label(
                outer,
                text=label,
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(7, 4))
            self._pin_entry(outer, variable).pack(fill="x", ipady=4)

        def save_pin():
            if not store.verify_pin(old_var.get(), self.username).ok:
                messagebox.showerror(
                    "MikroFIX", "Trenutni PIN ni pravilen.", parent=win
                )
                return
            if not validate_pin_format(new_var.get()):
                messagebox.showwarning(
                    "MikroFIX",
                    "Novi PIN mora vsebovati 6 do 14 številk in ne sme biti preprost.",
                    parent=win,
                )
                return
            if new_var.get() != confirm_var.get():
                messagebox.showwarning(
                    "MikroFIX", "Nova PIN-a se ne ujemata.", parent=win
                )
                return
            store.set_pin(
                new_var.get(),
                self.username,
                event_type="PIN_CHANGED",
                changed_by=self.username,
            )
            messagebox.showinfo("MikroFIX", "PIN je spremenjen.", parent=win)
            win.destroy()

        ttk.Button(
            outer,
            text="SHRANI NOVI PIN",
            command=save_pin,
            style="Blue.TButton",
        ).pack(anchor="w", pady=(18, 0))
        self._center(win)

    def _ask_new_pin(self, parent, title: str, intro: str) -> str | None:
        result = {"pin": None}
        win = tk.Toplevel(parent)
        win.title(title)
        win.geometry("440x330")
        win.resizable(False, False)
        win.configure(bg=self.bg)
        win.transient(parent)
        win.grab_set()

        outer = tk.Frame(win, bg=self.panel)
        outer.pack(fill="both", expand=True, padx=22, pady=22)
        tk.Label(
            outer,
            text=intro,
            bg=self.panel,
            fg=self.dark,
            font=("Segoe UI", 13, "bold"),
            wraplength=370,
        ).pack(anchor="w", pady=(0, 12))

        pin_var = tk.StringVar()
        confirm_var = tk.StringVar()
        for label, variable in (("Novi PIN", pin_var), ("Ponovi PIN", confirm_var)):
            tk.Label(
                outer,
                text=label,
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(6, 4))
            self._pin_entry(outer, variable).pack(fill="x", ipady=3)

        def save():
            if not validate_pin_format(pin_var.get()):
                messagebox.showwarning(
                    "MikroFIX",
                    "PIN mora vsebovati 6 do 14 številk in ne sme biti preprost.",
                    parent=win,
                )
                return
            if pin_var.get() != confirm_var.get():
                messagebox.showwarning(
                    "MikroFIX", "PIN-a se ne ujemata.", parent=win
                )
                return
            result["pin"] = pin_var.get()
            win.destroy()

        ttk.Button(
            outer, text="SHRANI PIN", command=save, style="Blue.TButton"
        ).pack(anchor="w", pady=(16, 0))
        self._center(win)
        parent.wait_window(win)
        return result["pin"]

    def manage_users(self, parent) -> None:
        if not self.has_permission(PERMISSION_MANAGE_USERS):
            messagebox.showwarning(
                "MikroFIX",
                "Za urejanje uporabnikov nimaš dovoljenja.",
                parent=parent,
            )
            return

        store = self.store_factory()
        store.init()
        win = tk.Toplevel(parent)
        win.title(f"MikroFIX {self.version_label} - Uporabniki")
        # Večja začetna višina je potrebna pri Windows merilu 125/150 %, sicer
        # je spodnja vrstica gumbov lahko izven vidnega dela okna.
        win.geometry("900x650")
        win.minsize(780, 570)
        win.configure(bg=self.bg)
        win.transient(parent)

        outer = tk.Frame(win, bg=self.panel)
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        tk.Label(
            outer,
            text="Uporabniki",
            bg=self.panel,
            fg=self.dark,
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="Uporabnika ne brišemo; po potrebi ga izključimo, da ostane zgodovina sledljiva.",
            bg=self.panel,
            fg=self.gray,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 12))

        columns = ("username", "display", "role", "active", "last_login")
        tree = ttk.Treeview(outer, columns=columns, show="headings", height=10)
        headings = {
            "username": "Uporabniško ime",
            "display": "Ime",
            "role": "Vloga",
            "active": "Aktiven",
            "last_login": "Zadnja prijava",
        }
        widths = {
            "username": 135,
            "display": 150,
            "role": 100,
            "active": 75,
            "last_login": 155,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="w")
        tree.pack(fill="both", expand=True)

        users_by_name: dict[str, dict] = {}

        def refresh():
            users_by_name.clear()
            for item in tree.get_children():
                tree.delete(item)
            for user in store.list_users():
                username = str(user["username"])
                users_by_name[username] = user
                tree.insert(
                    "",
                    "end",
                    values=(
                        username,
                        user["display_name"],
                        ROLE_LABELS.get(str(user["role"]), user["role"]),
                        "DA" if user["is_active"] else "NE",
                        user["last_login_at"] or "-",
                    ),
                )

        def selected():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(
                    "MikroFIX", "Najprej izberi uporabnika.", parent=win
                )
                return None
            username = str(tree.item(selection[0], "values")[0])
            return users_by_name.get(username)

        def add_user():
            child = tk.Toplevel(win)
            child.title("MikroFIX - Dodaj uporabnika")
            child.geometry("500x500")
            child.resizable(False, False)
            child.configure(bg=self.bg)
            child.transient(win)
            child.grab_set()
            form = tk.Frame(child, bg=self.panel)
            form.pack(fill="both", expand=True, padx=22, pady=22)

            username_var = tk.StringVar()
            display_var = tk.StringVar()
            role_var = tk.StringVar(value=ROLE_LABELS["SERVICE"])
            pin_var = tk.StringVar()
            confirm_var = tk.StringVar()

            tk.Label(
                form,
                text="Dodaj uporabnika",
                bg=self.panel,
                fg=self.dark,
                font=("Segoe UI", 16, "bold"),
            ).pack(anchor="w", pady=(0, 12))

            for label, variable in (
                ("Uporabniško ime", username_var),
                ("Prikazno ime", display_var),
            ):
                tk.Label(
                    form,
                    text=label,
                    bg=self.panel,
                    fg=self.gray,
                    font=("Segoe UI", 9, "bold"),
                ).pack(anchor="w", pady=(6, 4))
                ttk.Entry(form, textvariable=variable).pack(fill="x", ipady=3)

            tk.Label(
                form,
                text="Vloga",
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor="w", pady=(6, 4))
            ttk.Combobox(
                form,
                textvariable=role_var,
                values=list(ROLE_BY_LABEL),
                state="readonly",
            ).pack(fill="x", ipady=3)

            for label, variable in (("Začetni PIN", pin_var), ("Ponovi PIN", confirm_var)):
                tk.Label(
                    form,
                    text=label,
                    bg=self.panel,
                    fg=self.gray,
                    font=("Segoe UI", 9, "bold"),
                ).pack(anchor="w", pady=(6, 4))
                self._pin_entry(form, variable).pack(fill="x", ipady=3)

            def save_user():
                if not validate_pin_format(pin_var.get()):
                    messagebox.showwarning(
                        "MikroFIX",
                        "Začetni PIN mora vsebovati 6 do 14 številk in ne sme biti preprost.",
                        parent=child,
                    )
                    return
                if pin_var.get() != confirm_var.get():
                    messagebox.showwarning(
                        "MikroFIX", "PIN-a se ne ujemata.", parent=child
                    )
                    return
                try:
                    store.create_user(
                        username_var.get(),
                        display_var.get(),
                        ROLE_BY_LABEL[role_var.get()],
                        pin=pin_var.get(),
                        created_by=self.username,
                    )
                except (ValueError, KeyError) as exc:
                    messagebox.showerror("MikroFIX", str(exc), parent=child)
                    return
                refresh()
                child.destroy()

            ttk.Button(
                form,
                text="DODAJ UPORABNIKA",
                command=save_user,
                style="Blue.TButton",
            ).pack(anchor="w", pady=(18, 0))
            self._center(child)

        def toggle_active():
            user = selected()
            if not user:
                return
            active = bool(user["is_active"])
            username = str(user["username"])
            if active and username == self.username:
                messagebox.showwarning(
                    "MikroFIX",
                    "Trenutno prijavljenega uporabnika ni mogoče izključiti.",
                    parent=win,
                )
                return
            try:
                store.set_user_active(
                    username, not active, changed_by=self.username
                )
            except ValueError as exc:
                messagebox.showerror("MikroFIX", str(exc), parent=win)
                return
            refresh()

        def reset_pin():
            user = selected()
            if not user:
                return
            if not user["is_active"]:
                messagebox.showwarning(
                    "MikroFIX",
                    "Pred nastavitvijo PIN-a uporabnika najprej vključi.",
                    parent=win,
                )
                return
            pin = self._ask_new_pin(
                win,
                "MikroFIX - Nastavi PIN",
                f"Nastavi novi PIN za {user['display_name']}",
            )
            if pin is None:
                return
            try:
                store.set_pin(
                    pin,
                    str(user["username"]),
                    event_type="PIN_RESET_BY_ADMIN",
                    changed_by=self.username,
                    temporary=True,
                )
            except ValueError as exc:
                messagebox.showerror("MikroFIX", str(exc), parent=win)
                return
            messagebox.showinfo("MikroFIX", "Novi PIN je shranjen.", parent=win)
            refresh()

        def edit_permissions():
            user = selected()
            if not user:
                return
            username = str(user["username"])
            current_permissions = store.permissions_for(username)
            child = tk.Toplevel(win)
            child.title("MikroFIX - Dovoljenja uporabnika")
            child.geometry("650x610")
            child.minsize(590, 560)
            child.configure(bg=self.bg)
            child.transient(win)
            child.grab_set()

            form = tk.Frame(child, bg=self.panel)
            form.pack(fill="both", expand=True, padx=22, pady=22)
            tk.Label(
                form,
                text=f"Dovoljenja – {user['display_name']}",
                bg=self.panel,
                fg=self.dark,
                font=("Segoe UI", 16, "bold"),
            ).pack(anchor="w")
            tk.Label(
                form,
                text=(
                    "Dovoljenje se preveri tudi ob izvedbi ukaza. "
                    "Danielova skrbniška dovoljenja so zaklenjena."
                ),
                bg=self.panel,
                fg=self.gray,
                font=("Segoe UI", 9),
                wraplength=570,
                justify="left",
            ).pack(anchor="w", pady=(3, 14))

            checks = tk.Frame(form, bg=self.panel)
            checks.pack(fill="both", expand=True)
            variables: dict[str, tk.BooleanVar] = {}
            locked = username == DEFAULT_USERNAME
            for index, permission in enumerate(PERMISSION_LABELS):
                variable = tk.BooleanVar(
                    value=permission in current_permissions or locked
                )
                variables[permission] = variable
                check = ttk.Checkbutton(
                    checks,
                    text=PERMISSION_LABELS[permission],
                    variable=variable,
                )
                if locked:
                    check.configure(state="disabled")
                check.grid(
                    row=index // 2,
                    column=index % 2,
                    sticky="w",
                    padx=(0, 26),
                    pady=8,
                )
            checks.grid_columnconfigure(0, weight=1)
            checks.grid_columnconfigure(1, weight=1)

            actions = tk.Frame(form, bg=self.panel)
            actions.pack(fill="x", pady=(14, 0))

            def save_permissions():
                selected_permissions = {
                    permission
                    for permission, variable in variables.items()
                    if variable.get()
                }
                try:
                    store.set_permissions(
                        username,
                        selected_permissions,
                        changed_by=self.username,
                    )
                except ValueError as exc:
                    messagebox.showerror("MikroFIX", str(exc), parent=child)
                    return
                if username == self.username:
                    self._set_current(user)
                messagebox.showinfo(
                    "MikroFIX", "Dovoljenja so shranjena.", parent=child
                )
                child.destroy()

            ttk.Button(
                actions,
                text="SHRANI DOVOLJENJA",
                command=save_permissions,
                style="Blue.TButton",
                state="disabled" if locked else "normal",
            ).pack(side="left")
            ttk.Button(actions, text="ZAPRI", command=child.destroy).pack(
                side="right"
            )
            self._center(child)

        buttons = tk.Frame(outer, bg=self.panel)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(
            buttons, text="DODAJ UPORABNIKA", command=add_user, style="Blue.TButton"
        ).pack(side="left")
        ttk.Button(
            buttons, text="VKLJUČI / IZKLJUČI", command=toggle_active
        ).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="NASTAVI NOVI PIN", command=reset_pin).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="DOVOLJENJA", command=edit_permissions).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="ZAPRI", command=win.destroy).pack(side="right")

        refresh()
        self._center(win)
