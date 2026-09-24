#!/usr/bin/env python3
"""Simple owner-facing desktop conversation with the MikroFix local manager."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any

try:
    from tools.mikrofix_local_manager import installed_ollama_models
except ModuleNotFoundError:  # direct execution adds tools/, not the repo root
    from mikrofix_local_manager import installed_ollama_models  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
MANAGER = PROJECT_ROOT / "tools" / "mikrofix_local_manager.py"
WINDOW_TITLE = "MikroFix – lokalni vodja projekta"

STATE_LABELS = {
    "CONVERSATION": "Pogovor z izbranim lokalnim vodjo",
    "PLANNING": "Izbrani model pripravlja predlog, GPT-OSS pa preverja načrt",
    "READY_FOR_OWNER_LEASE": "Načrt je pripravljen — čaka tvojo odobritev izvedbe",
    "WAITING_FOR_OWNER_LEASE": "Čaka na tvojo potrditev dovoljenja za pisanje",
    "RUNNING_LOCAL": "GPT-OSS izvaja nalogo in ločeni pregledovalec jo preverja",
    "READY_FOR_CODEX_REVIEW": "Kandidat je pripravljen za Codexov pregled",
    "CODEX_REJECTED": "Codex je kandidata zavrnil — vrnjen je lokalnemu vodji",
    "APPLIED": "Codex je spremembo odobril in vključitev je zabeležena",
    "BLOCKED": "Naloga potrebuje popravek ali konkretno odločitev",
    "PLAN_READY": "Načrt je pripravljen",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_json_from_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def list_jobs(workspace: Path) -> list[dict[str, Any]]:
    jobs = []
    root = workspace / "jobs"
    if not root.is_dir():
        return jobs
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        request = read_json(directory / "request.json")
        manager = read_json(directory / "manager.json")
        if not request:
            continue
        effective_state = manager.get("state", "UNKNOWN")
        supervisor_path = manager.get("supervisor_state")
        if supervisor_path:
            supervisor = read_json(Path(supervisor_path))
            stage = supervisor.get("stage")
            if stage == "AWAIT_CODEX_REVIEW":
                effective_state = "READY_FOR_CODEX_REVIEW"
                manager["checkpoint"] = supervisor.get("checkpoint")
            elif stage == "BLOCKED":
                effective_state = "BLOCKED"
                manager["exact_reason"] = supervisor.get("feedback", "")
            elif stage not in {None, "PLANNED", "NEEDS_WRITE_LEASE"}:
                effective_state = "RUNNING_LOCAL"
        parent_id = request.get("parent_run_id")
        review_root = root / parent_id if (
            isinstance(parent_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", parent_id)
        ) else directory
        review = read_json(review_root / "codex-review.json")
        if review.get("status") == "REJECTED":
            effective_state = "CODEX_REJECTED"
        elif review.get("status") == "APPLIED":
            effective_state = "APPLIED"
        jobs.append({
            "run_id": request.get("run_id", directory.name),
            "task": request.get("task", ""),
            "parent_run_id": request.get("parent_run_id"),
            "created_at": request.get("created_at", ""),
            "state": effective_state,
            "manager": manager,
            "directory": directory,
            "lead_profile": request.get("lead_profile", ""),
            "model_tag": request.get("model_tag", request.get("model", "")),
            "review": review,
        })
    jobs.sort(key=lambda item: item["created_at"], reverse=True)
    return jobs


def planner_activity(directory: Path, limit: int = 12) -> list[str]:
    job_root = directory / "planner-runtime" / "bridge" / "jobs"
    logs = sorted(job_root.glob("*/log.txt"), key=lambda path: path.stat().st_mtime)
    if not logs:
        return []
    try:
        return logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []


def render_job(job: dict[str, Any]) -> str:
    manager = job["manager"]
    lines = [
        "TI",
        job["task"].strip(),
        "",
        "LOKALNI VODJA",
        STATE_LABELS.get(job["state"], job["state"]),
        f"Naloga: {job['run_id']}",
    ]
    if job.get("parent_run_id"):
        lines.append(f"Nadaljevanje naloge: {job['parent_run_id']}")
    plan_path = manager.get("plan")
    if plan_path:
        plan = read_json(Path(plan_path))
        if plan.get("objective"):
            lines.extend(["", "Načrt:", plan["objective"]])
        atoms = plan.get("atoms") or []
        for atom in atoms:
            lines.append(f"• {atom.get('task', atom.get('id', 'podnaloga'))}")
    reason = manager.get("exact_reason")
    if reason:
        lines.extend(["", "Potrebujem / razlog:", str(reason)])
    activity = planner_activity(job["directory"])
    if activity and job["state"] == "PLANNING":
        lines.extend(["", "Trenutno dogajanje:", *activity])
    if manager.get("checkpoint"):
        lines.extend(["", "Kandidat:", str(manager["checkpoint"])])
    conversation = read_json(job["directory"] / "conversation.json")
    turns = conversation.get("turns") or []
    if turns:
        model_tag = str(
            conversation.get("model_tag") or job.get("model_tag") or "izbrani model"
        )
        lead_name = f"POGOVORNI LOKALNI VODJA · {model_tag}"
        lines.extend(["", f"NEPOSREDNI POGOVOR · {lead_name}"])
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            role = "TI" if turn.get("role") == "owner" else lead_name
            lines.extend(["", role, str(turn.get("content") or "").strip()])
            if turn.get("routing"):
                lines.append(f"Odločitev: {turn['routing']}")
            if turn.get("routed_run_id"):
                lines.append(f"Predano v obdelavo: {turn['routed_run_id']}")
    return "\n".join(lines).strip() + "\n"


def activate_existing_window() -> bool:
    """Raise the already-running manager when the desktop shortcut is clicked again."""
    try:
        search = subprocess.run(
            ["xdotool", "search", "--name", WINDOW_TITLE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
        window_ids = [line.strip() for line in search.stdout.splitlines() if line.strip()]
        if search.returncode != 0 or not window_ids:
            return False
        activated = subprocess.run(
            ["xdotool", "windowactivate", "--sync", window_ids[-1]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return activated.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class LocalManagerUI:
    def __init__(self, root: tk.Tk, workspace: Path = DEFAULT_WORKSPACE):
        self.root = root
        self.workspace = workspace.expanduser().resolve()
        self.jobs: list[dict[str, Any]] = []
        self.selected_run_id: str | None = None
        self.last_render = ""
        root.title(WINDOW_TITLE)
        root.geometry("1180x760")
        root.minsize(900, 620)
        root.configure(bg="#17212b")
        self._build()
        self.refresh()

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#17212b")
        style.configure("TLabel", background="#17212b", foreground="#e7edf3")
        style.configure("Header.TLabel", font=("Sans", 18, "bold"))
        style.configure("Status.TLabel", font=("Sans", 10, "bold"), foreground="#7dd3fc")
        style.configure("TButton", padding=8)

        header = ttk.Frame(self.root, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(header, text="MikroFix – lokalni vodja projekta",
                  style="Header.TLabel").pack(side="left")
        self.global_status = ttk.Label(header, text="Pripravljen", style="Status.TLabel")
        self.global_status.pack(side="right")

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        left = ttk.Frame(body, padding=8)
        right = ttk.Frame(body, padding=8)
        body.add(left, weight=1)
        body.add(right, weight=3)

        ttk.Label(left, text="Naloge", style="Status.TLabel").pack(anchor="w", pady=(0, 6))
        self.job_list = tk.Listbox(
            left, bg="#202d3a", fg="#e7edf3", selectbackground="#2563eb",
            selectforeground="white", borderwidth=0, highlightthickness=0,
            font=("Sans", 10), activestyle="none",
        )
        self.job_list.pack(fill="both", expand=True)
        self.job_list.bind("<<ListboxSelect>>", self.on_select)
        ttk.Button(left, text="Nova naloga", command=self.new_task).pack(fill="x", pady=(8, 0))

        self.transcript = tk.Text(
            right, wrap="word", state="disabled", bg="#0f1720", fg="#e7edf3",
            insertbackground="white", relief="flat", padx=16, pady=14,
            font=("Sans", 11), spacing1=2, spacing3=8,
        )
        self.transcript.pack(fill="both", expand=True)

        ttk.Label(right, text="Napiši novo nalogo ali pojasnilo izbrani nalogi:").pack(
            anchor="w", pady=(10, 4)
        )
        self.input = tk.Text(
            right, height=5, wrap="word", bg="#202d3a", fg="white",
            insertbackground="white", relief="flat", padx=10, pady=8,
            font=("Sans", 11),
        )
        self.input.pack(fill="x")
        self.input.bind("<Control-Return>", lambda _event: self.send())

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Label(actions, text="Pogovorni lokalni vodja").pack(
            side="left", padx=(0, 8)
        )
        try:
            model_values = tuple(sorted(installed_ollama_models()))
        except Exception:
            model_values = ()
        self.model = ttk.Combobox(
            actions, state="readonly", width=30, values=model_values,
        )
        if model_values:
            preferred = (
                model_values.index("gpt-oss:20b")
                if "gpt-oss:20b" in model_values else 0
            )
            self.model.current(preferred)
        self.model.pack(side="left", padx=(0, 8))
        self.send_button = ttk.Button(actions, text="Pošlji lokalnemu vodji",
                                      command=self.send)
        self.send_button.pack(side="left")
        self.plan_button = ttk.Button(
            actions, text="Pripravi izvedbeni načrt", command=self.prepare_plan,
        )
        self.plan_button.pack(side="left", padx=(8, 0))
        self.execute_button = ttk.Button(actions, text="Dovoli in izvedi",
                                         command=self.execute_selected)
        self.execute_button.pack(side="left", padx=8)
        self.codex_button = ttk.Button(actions, text="Pripravi Codex pregled",
                                       command=self.prepare_for_codex)
        self.codex_button.pack(side="left")
        self.rejected_button = ttk.Button(
            actions, text="Codex zavrnil", command=self.codex_rejected,
        )
        self.rejected_button.pack(side="left", padx=(8, 0))
        self.approved_button = ttk.Button(
            actions, text="Codex odobril", command=self.codex_approved,
        )
        self.approved_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Osveži", command=self.refresh).pack(side="right")

    def selected_job(self) -> dict[str, Any] | None:
        return next((job for job in self.jobs if job["run_id"] == self.selected_run_id), None)

    def refresh(self) -> None:
        previous = self.selected_run_id
        self.jobs = list_jobs(self.workspace)
        self.job_list.delete(0, "end")
        for job in self.jobs:
            label = STATE_LABELS.get(job["state"], job["state"])
            short = job["task"].replace("\n", " ")[:48]
            self.job_list.insert("end", f"{short}\n  {label}")
        if previous:
            for index, job in enumerate(self.jobs):
                if job["run_id"] == previous:
                    self.job_list.selection_set(index)
                    self.job_list.see(index)
                    break
        if not self.selected_run_id and self.jobs:
            self.selected_run_id = self.jobs[0]["run_id"]
            self.job_list.selection_set(0)
        self.show_selected()
        self.root.after(2000, self.refresh)

    def on_select(self, _event=None) -> None:
        selection = self.job_list.curselection()
        if selection:
            self.selected_run_id = self.jobs[selection[0]]["run_id"]
            self.show_selected()

    def show_selected(self) -> None:
        job = self.selected_job()
        text = render_job(job) if job else (
            "Začni novo nalogo. Izbrani model bo v vlogi pogovornega lokalnega "
            "vodje pregledal projekt in pripravil predlog. Po tvoji potrditvi "
            "izvedbo prevzamejo vnaprej določene vloge.\n"
        )
        if text != self.last_render:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.insert("1.0", text)
            self.transcript.configure(state="disabled")
            self.transcript.see("end")
            self.last_render = text
        state = job["state"] if job else ""
        if job and job.get("model_tag"):
            values = tuple(self.model.cget("values"))
            if job["model_tag"] not in values:
                values = (*values, job["model_tag"])
                self.model.configure(values=values)
            self.model.set(job["model_tag"])
            self.model.configure(state="disabled")
        else:
            self.model.configure(state="readonly")
        self.execute_button.configure(
            state="normal" if state == "READY_FOR_OWNER_LEASE" else "disabled"
        )
        self.plan_button.configure(
            state="normal" if state == "CONVERSATION" else "disabled"
        )
        self.codex_button.configure(
            state="normal" if state == "READY_FOR_CODEX_REVIEW" else "disabled"
        )
        prepared = bool(job and job.get("review", {}).get("status") == "PREPARED")
        self.rejected_button.configure(state="normal" if prepared else "disabled")
        self.approved_button.configure(state="normal" if prepared else "disabled")

    def new_task(self) -> None:
        self.selected_run_id = None
        self.job_list.selection_clear(0, "end")
        self.input.delete("1.0", "end")
        self.input.focus_set()
        self.last_render = ""
        self.model.configure(state="readonly")
        self.show_selected()

    def spawn_manager(self, arguments: list[str]) -> None:
        reports = self.workspace / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        log_path = reports / f"ui-{int(time.time() * 1000)}.log"
        log = log_path.open("w", encoding="utf-8")
        env = {**os.environ, "MIKROFIX_VISIBLE_PLANNER": "0"}
        subprocess.Popen(
            [sys.executable, str(MANAGER), "--workspace", str(self.workspace), *arguments],
            cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        log.close()

    def send(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("MikroFix", "Najprej napiši nalogo ali pojasnilo.")
            return
        model_tag = self.model.get().strip()
        if not self.selected_run_id and not model_tag:
            messagebox.showerror(
                "MikroFix", "Ni najdenega nameščenega lokalnega modela.",
            )
            return
        arguments = (
            ["talk", "--message", text, "--parent", self.selected_run_id,
             "--profile", "conversation_lead"]
            if self.selected_run_id else
            ["start", "--message", text, "--profile", "conversation_lead",
             "--model", model_tag]
        )
        self.spawn_manager(arguments)
        self.input.delete("1.0", "end")
        self.global_status.configure(text="Lokalni vodja je prevzel sporočilo")

    def execute_selected(self) -> None:
        job = self.selected_job()
        if not job:
            return
        self.spawn_manager(["execute", job["run_id"], "--lease-minutes", "30"])
        self.global_status.configure(text="Čakam na tvojo potrditev dovoljenja")

    def prepare_plan(self) -> None:
        job = self.selected_job()
        if not job:
            return
        self.spawn_manager(["prepare-plan", job["run_id"]])
        self.global_status.configure(text="Lokalni vodja pripravlja izvedbeni načrt")

    def run_manager_now(self, arguments: list[str]) -> dict[str, Any] | None:
        result = subprocess.run(
            [sys.executable, str(MANAGER), "--workspace", str(self.workspace),
             *arguments],
            cwd=PROJECT_ROOT, env={**os.environ, "MIKROFIX_VISIBLE_PLANNER": "0"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            error = read_json_from_text(result.stderr)
            messagebox.showerror(
                "MikroFix", str(error.get("exact_reason") or result.stderr.strip()),
            )
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            messagebox.showerror("MikroFix", "Manager ni vrnil veljavnega odgovora.")
            return None
        return value if isinstance(value, dict) else None

    def prepare_for_codex(self) -> None:
        job = self.selected_job()
        if not job:
            return
        result = self.run_manager_now(["prepare-review", job["run_id"]])
        if not result:
            return
        message = f"Preglej pripravljeni Codex paket: {result['package_path']}"
        self.root.clipboard_clear()
        self.root.clipboard_append(message)
        self.root.update_idletasks()
        self.global_status.configure(text="Pot do Codex paketa je kopirana")

    def codex_rejected(self) -> None:
        job = self.selected_job()
        if not job:
            return
        feedback = simpledialog.askstring(
            "Codex zavrnil", "Prilepi Codexove ugotovitve:", parent=self.root,
        )
        if not feedback or not feedback.strip():
            return
        if self.run_manager_now([
            "codex-rejected", job["run_id"], "--feedback", feedback.strip(),
        ]):
            self.global_status.configure(text="Naloga je vrnjena lokalnemu vodji")

    def codex_approved(self) -> None:
        job = self.selected_job()
        if not job:
            return
        commit = simpledialog.askstring(
            "Codex odobril", "Prilepi commit SHA vključene spremembe:",
            parent=self.root,
        )
        if not commit or not commit.strip():
            return
        if self.run_manager_now([
            "codex-approved", job["run_id"], "--commit", commit.strip(),
        ]):
            self.global_status.configure(text="Odobritev in vključitev sta zabeleženi")


def main() -> int:
    if not (DEFAULT_WORKSPACE / "workspace.json").is_file():
        messagebox.showerror(
            "MikroFix – lokalni vodja",
            "Delovno območje lokalnih agentov še ni pripravljeno.",
        )
        return 2
    lock_path = Path.home() / ".local/state/mikrofix-manager-ui.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        activate_existing_window()
        lock.close()
        return 0
    root = tk.Tk()
    LocalManagerUI(root)
    root.mainloop()
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
