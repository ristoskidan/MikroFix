#!/usr/bin/env python3
"""OpenAI-compatible, fail-closed front door from Open WebUI to MikroFIX."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import time
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from tools.mikrofix_local_manager import (
        ManagerError, enabled_role_profiles, installed_ollama_models, model_runtime_presets,
        model_runtime_settings, normalize_runtime_selection,
        parse_research_envelope, prepare_plan, resolve_runtime_selection,
        start_research, talk,
    )
    from tools.mikrofix_local_manager_ui import DEFAULT_WORKSPACE
    from tools.mikrofix_local_web import dispatch_owner_command
    from tools.mikrofix_owner_commands import OwnerCommandError, parse_owner_command
    from tools.mikrofix_task_lifecycle import read_profile_snapshot
    from tools.mikrofix_workspace_lock import workspace_lock
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from mikrofix_local_manager import (  # type: ignore
        ManagerError, enabled_role_profiles, installed_ollama_models, model_runtime_presets,
        model_runtime_settings, normalize_runtime_selection,
        parse_research_envelope, prepare_plan, resolve_runtime_selection,
        start_research, talk,
    )
    from mikrofix_local_manager_ui import DEFAULT_WORKSPACE  # type: ignore
    from mikrofix_local_web import dispatch_owner_command  # type: ignore
    from mikrofix_owner_commands import (  # type: ignore
        OwnerCommandError, parse_owner_command,
    )
    from mikrofix_task_lifecycle import read_profile_snapshot  # type: ignore
    from mikrofix_workspace_lock import workspace_lock  # type: ignore


MODEL_PREFIX = "mikrofix::"
RUN_MARKER = re.compile(r"(?:MikroFIX pogovor|Pogovor):\s*(MF-LOCAL-[A-Za-z0-9_-]+)")
MODEL_MARKER = re.compile(r"MikroFIX izbrani model:\s*([^\s]+)")
SELECTION_MARKER = re.compile(r"MikroFIX izbor:\s*([^\s]+)")
SELECTION_RESET_MARKER = "MikroFIX izbor ponastavljen: true"
TOKEN_FILE = Path.home() / ".config/mikrofix-openwebui-gateway/token"
WORKSPACE = DEFAULT_WORKSPACE


def _read_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Gateway token is unavailable: {TOKEN_FILE}") from exc
    if not token:
        raise RuntimeError("Gateway token is empty")
    return token


def authenticate(authorization: str | None) -> None:
    expected = _read_token()
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:]
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid gateway token")


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item.get("text", "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        return "\n".join(parts).strip()
    return ""


def _encode_selection(model_tag: str, runtime: dict[str, Any]) -> str:
    payload = json.dumps(
        {"version": 1, "model_tag": model_tag, "runtime": runtime},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        _read_token().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _decode_selection(token: str) -> tuple[str, dict[str, Any]]:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("Neveljaven MikroFIX izbor modela.") from exc
    expected = hmac.new(
        _read_token().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Podpis izbora modela ni veljaven.")
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Neveljaven MikroFIX izbor modela.") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "model_tag", "runtime"}:
        raise ValueError("Neveljavna vsebina izbora modela.")
    model_tag = str(payload.get("model_tag") or "").strip()
    runtime = payload.get("runtime")
    if payload.get("version") != 1 or not model_tag or not isinstance(runtime, dict):
        raise ValueError("Neveljavna vsebina izbora modela.")
    return model_tag, normalize_runtime_selection(model_tag, runtime)


def extract_request(messages: object) -> tuple[str, str, str, dict[str, Any]]:
    """Return latest owner text, immutable conversation id and model choice."""
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    owner_message = ""
    run_id = ""
    model_tag = ""
    runtime: dict[str, Any] = {}
    for message in messages:
        text = _message_text(message)
        if not text:
            continue
        if isinstance(message, dict) and message.get("role") == "user":
            owner_message = text
        if isinstance(message, dict) and message.get("role") == "assistant":
            match = RUN_MARKER.search(text)
            if match:
                run_id = match.group(1)
            if SELECTION_RESET_MARKER in text:
                model_tag, runtime = "", {}
            selection_matches = SELECTION_MARKER.findall(text)
            if selection_matches:
                model_tag, runtime = _decode_selection(selection_matches[-1])
            elif not selection_matches:
                model_matches = MODEL_MARKER.findall(text)
                if model_matches:
                    model_tag = model_matches[-1]
                    runtime = {}
    if not owner_message:
        raise ValueError("The latest owner message is empty")
    return owner_message, run_id, model_tag, runtime


def _latest_reply(conversation: dict[str, Any]) -> tuple[str, str]:
    run_id = str(conversation.get("parent_run_id") or "")
    turns = conversation.get("turns")
    if not run_id or not isinstance(turns, list):
        raise ValueError("Manager returned an invalid conversation")
    reply = next(
        (
            str(turn.get("content") or "").strip()
            for turn in reversed(turns)
            if isinstance(turn, dict) and turn.get("role") == "lead"
        ),
        "",
    )
    if not reply:
        raise ValueError("Manager returned no lead reply")
    return reply, run_id


def virtual_model_id(role_id: str) -> str:
    if "::" in role_id:
        raise ValueError("Role ids must not contain '::'")
    return f"{MODEL_PREFIX}{role_id}"


def parse_virtual_model(model_id: object) -> str:
    if not isinstance(model_id, str) or not model_id.startswith(MODEL_PREFIX):
        raise ValueError("Unknown MikroFIX role selection")
    role_id = model_id[len(MODEL_PREFIX):]
    if not role_id or "::" in role_id:
        raise ValueError("Invalid MikroFIX role selection")
    roles = enabled_role_profiles()
    if role_id not in roles:
        raise ValueError(f"Unknown or disabled MikroFIX role: {role_id}")
    return role_id


def available_virtual_models() -> list[dict[str, object]]:
    roles = enabled_role_profiles()
    return [
        {
            "id": virtual_model_id(role_id),
            "object": "model", "created": 0, "owned_by": "mikrofix",
            "name": role["display_name"],
        }
        for role_id, role in roles.items()
    ]


def _locked_model(run_id: str, role_id: str) -> str:
    snapshot = read_profile_snapshot(WORKSPACE / "jobs" / run_id)
    if snapshot["profile_id"] != role_id:
        raise ValueError(
            "Ta pogovor je zaklenjen na drugo vlogo. "
            "Za primerjalni test odpri nov Open WebUI pogovor."
        )
    return str(snapshot["model_tag"])


def _parse_runtime_value(key: str, raw: str) -> Any:
    if key in {"num_ctx", "num_predict", "top_k", "seed"}:
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} zahteva celo število.") from exc
    if key in {"temperature", "top_p", "min_p", "repeat_penalty"}:
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{key} zahteva število.") from exc
    if key == "think":
        lowered = raw.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        return lowered
    raise ValueError(f"Nedovoljena nastavitev modela: {key}")


def _selection_summary(model_tag: str, runtime: dict[str, Any]) -> str:
    defaults = model_runtime_settings(model_tag)
    effective = {
        "num_ctx": runtime.get("num_ctx", defaults["conversation_num_ctx"]),
        "num_predict": runtime.get("num_predict", "model default"),
        "temperature": runtime.get("temperature", "model default"),
        "top_k": runtime.get("top_k", "model default"),
        "top_p": runtime.get("top_p", "model default"),
        "min_p": runtime.get("min_p", "model default"),
        "repeat_penalty": runtime.get("repeat_penalty", "model default"),
        "seed": runtime.get("seed", "model default"),
        "think": runtime.get("think", defaults["think"]),
    }
    lines = [f"Izbrani model: {model_tag}", "", "Runtime:"]
    lines.extend(f"- {key}: {str(value).lower() if isinstance(value, bool) else value}" for key, value in effective.items())
    return "\n".join(lines)


def _model_selection_command(
    message: str, *, run_id: str, selected_model: str,
    selected_runtime: dict[str, Any], role_id: str,
) -> str | None:
    clean = message.strip()
    try:
        parts = shlex.split(clean)
    except ValueError as exc:
        raise ValueError(f"Neveljaven /llm ukaz: {exc}") from exc
    if not parts:
        return None
    command = parts[0].lower()
    if command not in {"/llm", "/llms", "/llm-settings", "/llm-reset"}:
        return None
    if command == "/llms":
        if len(parts) != 1:
            raise ValueError("Ukaz /llms ne sprejme dodatnega besedila.")
        models = installed_ollama_models()
        lines = ["Nameščeni lokalni modeli:"]
        for model in models:
            presets = ", ".join(sorted(model_runtime_presets(model)))
            suffix = f" (presets: {presets})" if presets else ""
            lines.append(f"- {model}{suffix}")
        if run_id:
            lines.append(f"\nTa naloga je zaklenjena na: {_locked_model(run_id, role_id)}")
        elif selected_model:
            lines.append(f"\nTrenutno izbran za novo nalogo: {selected_model}")
        lines.append("\nIzberi z: /llm <točen tag> [preset=ime] [ključ=vrednost ...]")
        return "\n".join(lines)
    if command == "/llm-settings":
        if len(parts) != 1:
            raise ValueError("Ukaz /llm-settings ne sprejme dodatnega besedila.")
        if run_id:
            snapshot = read_profile_snapshot(WORKSPACE / "jobs" / run_id)
            runtime = {
                "num_ctx": snapshot.get("conversation_num_ctx", snapshot["num_ctx"]),
                "think": snapshot["think"],
                **{
                    key: value for key, value in (snapshot.get("runtime_options") or {}).items()
                    if value is not None
                },
            }
            return _selection_summary(str(snapshot["model_tag"]), runtime) + "\n\nStanje: zaklenjeno"
        if not selected_model:
            return "Za novo nalogo še ni izbran model. Uporabi /llms."
        return _selection_summary(selected_model, selected_runtime) + "\n\nStanje: predizbira"
    if command == "/llm-reset":
        if len(parts) != 1:
            raise ValueError("Ukaz /llm-reset ne sprejme dodatnega besedila.")
        if run_id:
            raise ValueError("Zaklenjenega modela ni mogoče ponastaviti. Odpri nov pogovor.")
        return (
            "Izbor modela za novo nalogo je odstranjen."
            f"\n\n<!-- {SELECTION_RESET_MARKER} -->"
        )
    if len(parts) < 2:
        raise ValueError("Po ukazu /llm napiši točen tag. Uporabi /llms.")
    if run_id:
        locked = _locked_model(run_id, role_id)
        raise ValueError(
            f"Ta naloga je že zaklenjena na {locked}. Za drug model odpri nov pogovor."
        )
    model_tag = parts[1]
    if model_tag not in installed_ollama_models():
        raise ValueError(f"Model ni nameščen v Ollami: {model_tag}")
    preset: str | None = None
    overrides: dict[str, Any] = {}
    for item in parts[2:]:
        key, separator, raw = item.partition("=")
        if not separator or not key or not raw:
            raise ValueError(f"Nastavitev mora biti zapisana kot ključ=vrednost: {item}")
        if key == "preset":
            if preset is not None:
                raise ValueError("preset je lahko naveden samo enkrat.")
            preset = raw
            continue
        if key in overrides:
            raise ValueError(f"Nastavitev {key} je podvojena.")
        overrides[key] = _parse_runtime_value(key, raw)
    runtime = resolve_runtime_selection(
        model_tag, preset=preset, overrides=overrides,
    )
    selection = _encode_selection(model_tag, runtime)
    return (
        _selection_summary(model_tag, runtime)
        + "\n\nNastavitev se bo zaklenila ob prvem običajnem sporočilu."
        + f"\n\n<!-- MikroFIX izbrani model: {model_tag}"
        + f"\nMikroFIX izbor: {selection} -->"
    )


def route_message(
    message: str, run_id: str, role_id: str, selected_model: str,
    selected_runtime: dict[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Route slash commands to trusted code and ordinary text to the locked lead."""
    model_response = _model_selection_command(
        message, run_id=run_id, selected_model=selected_model,
        selected_runtime=selected_runtime or {}, role_id=role_id,
    )
    if model_response is not None:
        return model_response
    command = parse_owner_command(message)
    if command is not None:
        if command.name == "/research":
            if run_id:
                raise ValueError(
                    "/research začne novo nalogo. V obstoječem MikroFIX "
                    "pogovoru napiši navadno dopolnilo."
                )
            if not selected_model:
                raise ValueError("Pred /research izberi model z /llm <tag>.")
            if selected_model not in installed_ollama_models():
                raise ValueError(f"Izbrani model ni več nameščen: {selected_model}")
            research_input = parse_research_envelope(command.argument)
            worker_model = str(research_input["worker_model"])
            if worker_model not in installed_ollama_models():
                raise ValueError(f"Izbrani worker model ni nameščen: {worker_model}")
            conversation = start_research(
                command.argument, workspace=WORKSPACE,
                lead_profile=role_id, model_tag=selected_model,
                runtime_overrides=selected_runtime,
                progress_callback=progress_callback,
            )
            reply, actual_run_id = _latest_reply(conversation)
            return (
                f"{reply}\n\nMikroFIX pogovor: {actual_run_id}"
                f"\nVloga: {role_id}\nModel: {selected_model}"
                f"\nMikroFIX izbrani model: {selected_model}"
            )
        if command.name != "/help" and not run_id:
            raise ValueError(
                f"Ukaz {command.name} potrebuje obstoječi MikroFIX pogovor."
            )
        if run_id:
            model_tag = _locked_model(run_id, role_id)
        else:
            model_tag = selected_model
        if command.name == "/prepare-plan":
            conversation = prepare_plan(
                run_id, workspace=WORKSPACE,
                progress_callback=progress_callback,
            )
            reply, actual_run_id = _latest_reply(conversation)
            return (
                f"{reply}\n\nMikroFIX pogovor: {actual_run_id}"
                f"\nVloga: {role_id}\nModel: {model_tag}"
                f"\nMikroFIX izbrani model: {model_tag}"
            )
        result = dispatch_owner_command(
            WORKSPACE, run_id, message,
            wait=command.name in {"/plan-dispatch", "/approve-plan"},
        )
        response = str(result.get("message") or "Ukaz je bil sprejet.").strip()
        if command.name == "/help":
            response += (
                "\n/llm <tag> [preset=ime] [ključ=vrednost ...] — izbere LLM in runtime"
                "\n/llms — pokaže nameščene lokalne modele"
                "\n/llm-settings — pokaže trenutno predizbiro ali zaklenjeni runtime"
                "\n/llm-reset — odstrani predizbiro pred nastankom naloge"
            )
        return response + (f"\n\nMikroFIX pogovor: {run_id}" if run_id else "")
    if run_id:
        model_tag = _locked_model(run_id, role_id)
        conversation = talk(
            message, parent_run_id=run_id, workspace=WORKSPACE,
            lead_profile=role_id, progress_callback=progress_callback,
        )
    else:
        raise ValueError(
            "Nova MikroFIX naloga se začne samo z izrecnim /research in "
            "razdelkoma [OWNER_REQUEST] ter [CODEX_RESEARCH_PROMPT]. "
            "Navadna dopolnila so dovoljena po nastanku MikroFIX pogovora."
        )
    reply, actual_run_id = _latest_reply(conversation)
    return (
        f"{reply}\n\nMikroFIX pogovor: {actual_run_id}"
        f"\nVloga: {role_id}\nModel: {model_tag}"
        f"\nMikroFIX izbrani model: {model_tag}"
    )


def _chunk(
    request_id: str, model_id: str, content: str, finish_reason: str | None,
    *, reasoning_content: str = "",
) -> str:
    delta: dict[str, str] = {}
    if content:
        delta["content"] = content
    if reasoning_content:
        delta["reasoning_content"] = reasoning_content
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _stream_response(
    request_id: str, model_id: str, operation: Callable[[], str],
    progress_queue: asyncio.Queue[dict[str, Any]] | None = None,
):
    task = asyncio.create_task(asyncio.to_thread(operation))
    yield _chunk(request_id, model_id, "", None)
    last_keepalive = time.monotonic()
    while not task.done():
        if progress_queue is not None:
            try:
                event = progress_queue.get_nowait()
            except asyncio.QueueEmpty:
                event = None
            if event is not None:
                reasoning = progress_reasoning(event)
                if reasoning:
                    yield _chunk(
                        request_id, model_id, "", None,
                        reasoning_content=reasoning,
                    )
                continue
        done, _pending = await asyncio.wait({task}, timeout=0.2)
        if not done and time.monotonic() - last_keepalive >= 10:
            # A standards-valid empty delta keeps the local connection alive.
            yield _chunk(request_id, model_id, "", None)
            last_keepalive = time.monotonic()
    if progress_queue is not None:
        while not progress_queue.empty():
            reasoning = progress_reasoning(progress_queue.get_nowait())
            if reasoning:
                yield _chunk(
                    request_id, model_id, "", None,
                    reasoning_content=reasoning,
                )
    try:
        content = task.result()
    except Exception as exc:  # streamed responses can no longer change HTTP status
        content = f"MikroFIX je zahtevo varno zavrnil: {exc}"
    yield _chunk(request_id, model_id, content, "stop")
    yield "data: [DONE]\n\n"


def progress_reasoning(event: dict[str, Any]) -> str:
    """Render one trusted bridge event as compact Open WebUI reasoning."""
    event_type = str(event.get("type") or "")
    if event_type == "model":
        thinking = str(event.get("thinking") or "").strip()
        return thinking + "\n" if thinking else ""
    if event_type == "tool_call":
        tool = str(event.get("tool") or "orodje")
        arguments = event.get("arguments")
        rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        if len(rendered) > 500:
            rendered = rendered[:497] + "..."
        return f"\nUporabljam `{tool}`: `{rendered}`\n"
    if event_type == "tool_result":
        tool = str(event.get("tool") or "orodje")
        status = "uspešno" if event.get("ok") is True else "napaka"
        return f"Rezultat `{tool}`: {status}.\n"
    return ""


app = FastAPI(
    title="MikroFIX Open WebUI Gateway",
    description=(
        "OpenAI-compatible front door. Slash commands are parsed by trusted "
        "manager code; only /prepare-plan invokes the locked conversation "
        "lead through its fixed read-only planning contract."
    ),
    version="1.0.0",
)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok", "selection": "role_then_model_command",
        "workspace": str(WORKSPACE),
    }


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)) -> dict[str, object]:
    authenticate(authorization)
    return {
        "object": "list",
        "data": available_virtual_models(),
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    body: dict[str, Any], authorization: str | None = Header(default=None),
):
    authenticate(authorization)
    try:
        role_id = parse_virtual_model(body.get("model"))
        message, run_id, selected_model, selected_runtime = extract_request(
            body.get("messages")
        )
        # Parse before starting any model work so unknown slash commands fail closed.
        gateway_command = message.strip().split(None, 1)[0].lower()
        if gateway_command not in {
            "/llm", "/llms", "/llm-settings", "/llm-reset",
        }:
            parse_owner_command(message)
    except (ValueError, ManagerError, OwnerCommandError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request_id = f"chatcmpl-mf-{time.time_ns()}"
    selected_model_id = virtual_model_id(role_id)
    if bool(body.get("stream")):
        loop = asyncio.get_running_loop()
        progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def publish_progress(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(progress_queue.put_nowait, event)

        operation = lambda: route_message(
            message, run_id, role_id, selected_model,
            selected_runtime=selected_runtime,
            progress_callback=publish_progress,
        )
        return StreamingResponse(
            _stream_response(
                request_id, selected_model_id, operation, progress_queue,
            ),
            media_type="text/event-stream",
        )
    operation = lambda: route_message(
        message, run_id, role_id, selected_model,
        selected_runtime=selected_runtime,
    )
    try:
        content = await asyncio.to_thread(operation)
    except (ValueError, ManagerError, OwnerCommandError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": selected_model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="172.17.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    import uvicorn
    with workspace_lock(WORKSPACE):
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
