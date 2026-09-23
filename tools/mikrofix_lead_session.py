#!/usr/bin/env python3
"""Persistent trusted context shared by one local lead and its child jobs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import urllib.request
import uuid


SOURCES = (
    "docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md",
    "MIKROFIX_v30_MASTER_SPECIFIKACIJA.md",
    "docs/local_agents/LEKCIJE.md",
    "docs/local_agents/supervisor/PROTOCOL_V2.md",
)

PLANNING_SOURCES = (
    "docs/MIKROFIX_GPT_OSS_20B_KONTEKST.md",
    "docs/local_agents/LEKCIJE.md",
    "docs/local_agents/supervisor/PROTOCOL_V2.md",
    "AGENTS.md",
)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def ensure_lead_session(
    project_root: Path, home: Path, *, sources: tuple[str, ...] = SOURCES,
    memory_name: str = "GPT_OSS_LEAD_MEMORY.md",
    profile_snapshot_hash: str = "", model: str = "", model_digest: str = "",
) -> dict[str, object]:
    """Create or refresh the one parent context without model-side file reads."""
    project_root = project_root.resolve()
    home = home.expanduser().resolve()
    manifest: dict[str, str] = {}
    sections = [
        "# MikroFIX — trajni kontekst glavnega lokalnega vodje\n",
        "Ta kontekst je že naložen in velja tudi za vse podizvajalce z istim "
        "parent_session_id. Jedrnih virov pri vsaki nalogi ne odpiraj znova. "
        "Preveri samo konkretno kodo in teste trenutne naloge.\n",
    ]
    for relative in sources:
        content = (project_root / relative).read_text(encoding="utf-8")
        manifest[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        sections.extend((f"\n---\n\n## VIR: `{relative}`\n", content.rstrip(), "\n"))
    memory = "\n".join(sections).rstrip() + "\n"
    memory_hash = hashlib.sha256(memory.encode("utf-8")).hexdigest()
    state_file = home / "lead-session.json"
    old: dict[str, object] = {}
    if state_file.is_file():
        try:
            old = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            old = {}
    identity = {
        "profile_snapshot_hash": profile_snapshot_hash,
        "model": model,
        "model_digest": model_digest,
    }
    same_identity = all(old.get(key, "") == value for key, value in identity.items())
    same_context = old.get("memory_sha256") == memory_hash
    session_id = str(
        old.get("session_id")
        if same_identity and old.get("session_id")
        else f"LEAD-{uuid.uuid4().hex[:16]}"
    )
    memory_file = home / memory_name
    if not memory_file.is_file() or not same_context:
        _atomic_text(memory_file, memory)
        memory_file.chmod(0o400)
    acknowledgement_file = home / "context-ready.json"
    acknowledgement: dict[str, object] = {}
    if acknowledgement_file.is_file():
        try:
            acknowledgement = json.loads(
                acknowledgement_file.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            acknowledgement = {}
    acknowledged = acknowledgement == {
        "context_ack": "CONTEXT_READY",
        "session_id": session_id,
        "memory_sha256": memory_hash,
    }
    state = {
        "schema_version": 1,
        "session_id": session_id,
        "state": (
            "CONTEXT_READY"
            if same_context and old.get("state") == "CONTEXT_READY" and acknowledged
            else "AWAITING_CONTEXT_ACK"
        ),
        "memory_file": str(memory_file),
        "memory_sha256": memory_hash,
        "source_manifest": manifest,
        **identity,
    }
    if acknowledged:
        state["acknowledgement_file"] = str(acknowledgement_file)
    _atomic_text(state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return state


def confirm_context(home: Path, session_id: str, memory_hash: str) -> dict[str, object]:
    resolved_home = home.expanduser().resolve()
    state_file = resolved_home / "lead-session.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if state.get("session_id") != session_id or state.get("memory_sha256") != memory_hash:
        raise RuntimeError("Lead context acknowledgement does not match the parent session")
    state["state"] = "CONTEXT_READY"
    acknowledgement = {
        "context_ack": "CONTEXT_READY",
        "session_id": session_id,
        "memory_sha256": memory_hash,
    }
    acknowledgement_file = resolved_home / "context-ready.json"
    _atomic_text(
        acknowledgement_file,
        json.dumps(acknowledgement, ensure_ascii=False, indent=2) + "\n",
    )
    state["acknowledgement_file"] = str(acknowledgement_file)
    _atomic_text(state_file, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return state


def ensure_context_ready(
    project_root: Path, home: Path, *, ollama_url: str, model: str,
    num_ctx: int = 131072, keep_alive: str = "30m",
    sources: tuple[str, ...] = SOURCES,
    memory_name: str = "GPT_OSS_LEAD_MEMORY.md",
    profile_snapshot_hash: str = "", model_digest: str = "",
) -> dict[str, object]:
    """Ask the lead for one acknowledgement only when trusted context changed."""
    state = ensure_lead_session(
        project_root, home, sources=sources, memory_name=memory_name,
        profile_snapshot_hash=profile_snapshot_hash, model=model,
        model_digest=model_digest,
    )
    if state["state"] == "CONTEXT_READY":
        return state
    memory = Path(str(state["memory_file"])).read_text(encoding="utf-8")
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["context_ack", "session_id", "memory_sha256"],
        "properties": {
            "context_ack": {"const": "CONTEXT_READY"},
            "session_id": {"const": state["session_id"]},
            "memory_sha256": {"const": state["memory_sha256"]},
        },
    }
    payload = json.dumps({
        "model": model, "stream": False, "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx}, "format": schema,
        "messages": [
            {"role": "system", "content": memory},
            {"role": "user", "content": (
                "Preberi in sprejmi priloženi trajni projektni kontekst. Ne izvajaj "
                "naloge in ne uporabljaj orodij. Vrni samo zahtevani JSON, s katerim "
                "potrdiš parent session in natančni hash konteksta."
            )},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(
        ollama_url, data=payload, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=1800) as response:
        result = json.loads(response.read().decode("utf-8"))
    acknowledgement = json.loads(result["message"]["content"])
    if acknowledgement != {
        "context_ack": "CONTEXT_READY",
        "session_id": state["session_id"],
        "memory_sha256": state["memory_sha256"],
    }:
        raise RuntimeError("Local lead returned an invalid context acknowledgement")
    return confirm_context(home, str(state["session_id"]), str(state["memory_sha256"]))
