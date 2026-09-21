#!/usr/bin/env python3
"""Simple owner-facing desktop conversation with the MikroFix local manager."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = Path.home() / "MikroFIX_Agent_Workspace"
MANAGER = PROJECT_ROOT / "tools" / "mikrofix_local_manager.py"
WINDOW_TITLE = "MikroFix – lokalni vodja projekta"

STATE_LABELS = {
    "CONVERSATION": "Pogovor z izbranim lokalnim vodjo",
    "PLANNING": "Qwen razčlenjuje nalogo, GPT-OSS pa preverja načrt",
    "READY_FOR_OWNER_LEASE": "Načrt je pripravljen — čaka tvojo odobritev izvedbe",
    "WAITING_FOR_OWNER_LEASE": "Čaka na tvojo potrditev dovoljenja za pisanje",
    "RUNNING_LOCAL": "GPT-OSS izvaja nalogo in ločeni pregledovalec jo preverja",
    "READY_FOR_CODEX_REVIEW": "Kandidat je pripravljen za Codexov pregled",
    "BLOCKED": "Naloga potrebuje popravek ali konkretno odločitev",
    "PLAN_READY": "Načrt je pripravljen",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
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
        jobs.append({
            "run_id": request.get("run_id", directory.name),
            "task": request.get("task", ""),
            "parent_run_id": request.get("parent_run_id"),
            "created_at": request.get("created_at", ""),
            "state": effective_state,
            "manager": manager,
            "directory": directory,
            "lead_profile": request.get("lead_profile", ""),
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
        profile = str(conversation.get("lead_profile") or "gpt_oss")
        lead_name = (
            "QWEN THINKING VODJA" if profile == "qwen_thinking"
            else "GPT-OSS VODJA"
        )
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
        self.profile = ttk.Combobox(
            actions, state="readonly", width=22,
            values=("GPT-OSS vodja", "Qwen Thinking vodja"),
        )
        self.profile.current(0)
        self.profile.pack(side="left", padx=(0, 8))
        self.send_button = ttk.Button(actions, text="Pošlji lokalnemu vodji",
                                      command=self.send)
        self.send_button.pack(side="left")
        self.execute_button = ttk.Button(actions, text="Dovoli in izvedi",
                                         command=self.execute_selected)
        self.execute_button.pack(side="left", padx=8)
        self.codex_button = ttk.Button(actions, text="Kopiraj za Codex",
                                       command=self.copy_for_codex)
        self.codex_button.pack(side="left")
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
            "Začni novo nalogo. Lokalni vodja bo pregledal projekt, pripravil načrt "
            "in delo predal Qwenu šele po tvoji potrditvi.\n"
        )
        if text != self.last_render:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.insert("1.0", text)
            self.transcript.configure(state="disabled")
            self.transcript.see("end")
            self.last_render = text
        state = job["state"] if job else ""
        if job and job.get("lead_profile"):
            self.profile.current(1 if job["lead_profile"] == "qwen_thinking" else 0)
            self.profile.configure(state="disabled")
        else:
            self.profile.configure(state="readonly")
        self.execute_button.configure(
            state="normal" if state == "READY_FOR_OWNER_LEASE" else "disabled"
        )
        self.codex_button.configure(
            state="normal" if state == "READY_FOR_CODEX_REVIEW" else "disabled"
        )

    def new_task(self) -> None:
        self.selected_run_id = None
        self.job_list.selection_clear(0, "end")
        self.input.delete("1.0", "end")
        self.input.focus_set()
        self.last_render = ""
        self.profile.configure(state="readonly")
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
        profile = "qwen_thinking" if self.profile.current() == 1 else "gpt_oss"
        arguments = (
            ["talk", "--message", text, "--parent", self.selected_run_id,
             "--profile", profile]
            if self.selected_run_id else
            ["start", "--message", text, "--profile", profile]
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

    def copy_for_codex(self) -> None:
        job = self.selected_job()
        if not job:
            return
        message = f"Preglej lokalnega kandidata {job['run_id']}"
        self.root.clipboard_clear()
        self.root.clipboard_append(message)
        self.root.update_idletasks()
        self.global_status.configure(text="Sporočilo za Codex je kopirano")


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
