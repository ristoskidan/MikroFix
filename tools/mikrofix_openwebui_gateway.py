#!/usr/bin/env python3
"""OpenAI-compatible, fail-closed front door from Open WebUI to MikroFIX."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from tools.mikrofix_local_manager import (
        enabled_role_profiles, installed_ollama_models, start_conversation, talk,
    )
    from tools.mikrofix_local_manager_ui import DEFAULT_WORKSPACE
    from tools.mikrofix_local_web import dispatch_owner_command
    from tools.mikrofix_owner_commands import OwnerCommandError, parse_owner_command
    from tools.mikrofix_task_lifecycle import read_profile_snapshot
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from mikrofix_local_manager import (  # type: ignore
        enabled_role_profiles, installed_ollama_models, start_conversation, talk,
    )
    from mikrofix_local_manager_ui import DEFAULT_WORKSPACE  # type: ignore
    from mikrofix_local_web import dispatch_owner_command  # type: ignore
    from mikrofix_owner_commands import (  # type: ignore
        OwnerCommandError, parse_owner_command,
    )
    from mikrofix_task_lifecycle import read_profile_snapshot  # type: ignore


MODEL_PREFIX = "mikrofix::"
RUN_MARKER = re.compile(r"(?:MikroFIX pogovor|Pogovor):\s*(MF-LOCAL-[A-Za-z0-9_-]+)")
MODEL_MARKER = re.compile(r"MikroFIX izbrani model:\s*([^\s]+)")
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


def extract_request(messages: object) -> tuple[str, str, str]:
    """Return latest owner text, immutable conversation id and model choice."""
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    owner_message = ""
    run_id = ""
    model_tag = ""
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
            model_matches = MODEL_MARKER.findall(text)
            if model_matches:
                model_tag = model_matches[-1]
    if not owner_message:
        raise ValueError("The latest owner message is empty")
    return owner_message, run_id, model_tag


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


def _model_selection_command(
    message: str, *, run_id: str, selected_model: str, role_id: str,
) -> str | None:
    clean = message.strip()
    token, _, argument = clean.partition(" ")
    command = token.lower()
    if command not in {"/model", "/models"}:
        return None
    if command == "/models":
        if argument.strip():
            raise ValueError("Ukaz /models ne sprejme dodatnega besedila.")
        models = installed_ollama_models()
        lines = ["Nameščeni lokalni modeli:"]
        lines.extend(f"- {model}" for model in models)
        if run_id:
            lines.append(f"\nTa naloga je zaklenjena na: {_locked_model(run_id, role_id)}")
        elif selected_model:
            lines.append(f"\nTrenutno izbran za novo nalogo: {selected_model}")
        lines.append("\nIzberi z: /model <točen tag>")
        return "\n".join(lines)
    model_tag = argument.strip()
    if not model_tag:
        raise ValueError("Po ukazu /model napiši točen tag. Uporabi /models.")
    if run_id:
        locked = _locked_model(run_id, role_id)
        raise ValueError(
            f"Ta naloga je že zaklenjena na {locked}. Za drug model odpri nov pogovor."
        )
    if model_tag not in installed_ollama_models():
        raise ValueError(f"Model ni nameščen v Ollami: {model_tag}")
    return (
        f"Za novo nalogo je izbran model: {model_tag}. Zdaj napiši zahtevo."
        f"\n\nMikroFIX izbrani model: {model_tag}"
    )


def route_message(
    message: str, run_id: str, role_id: str, selected_model: str,
) -> str:
    """Route slash commands to trusted code and ordinary text to the locked lead."""
    model_response = _model_selection_command(
        message, run_id=run_id, selected_model=selected_model, role_id=role_id,
    )
    if model_response is not None:
        return model_response
    command = parse_owner_command(message)
    if command is not None:
        if command.name != "/help" and not run_id:
            raise ValueError(
                f"Ukaz {command.name} potrebuje obstoječi MikroFIX pogovor."
            )
        if run_id:
            model_tag = _locked_model(run_id, role_id)
        else:
            model_tag = selected_model
        result = dispatch_owner_command(WORKSPACE, run_id, message)
        response = str(result.get("message") or "Ukaz je bil sprejet.").strip()
        if command.name == "/help":
            response += (
                "\n/model <tag> — izbere nameščeni LLM za novo nalogo"
                "\n/models — pokaže nameščene lokalne modele"
            )
        return response + (f"\n\nMikroFIX pogovor: {run_id}" if run_id else "")
    if run_id:
        model_tag = _locked_model(run_id, role_id)
        conversation = talk(
            message, parent_run_id=run_id, workspace=WORKSPACE,
            lead_profile=role_id,
        )
    else:
        model_tag = selected_model
        if not model_tag:
            raise ValueError(
                "Najprej izberi lokalni model z /model <tag>. "
                "Seznam dobiš z /models."
            )
        if model_tag not in installed_ollama_models():
            raise ValueError(f"Izbrani model ni več nameščen: {model_tag}")
        conversation = start_conversation(
            message, workspace=WORKSPACE, lead_profile=role_id,
            model_tag=model_tag,
        )
    reply, actual_run_id = _latest_reply(conversation)
    return (
        f"{reply}\n\nMikroFIX pogovor: {actual_run_id}"
        f"\nVloga: {role_id}\nModel: {model_tag}"
        f"\nMikroFIX izbrani model: {model_tag}"
    )


def _chunk(
    request_id: str, model_id: str, content: str, finish_reason: str | None,
) -> str:
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": finish_reason,
        }],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _stream_response(
    request_id: str, model_id: str, operation: Callable[[], str],
):
    task = asyncio.create_task(asyncio.to_thread(operation))
    yield _chunk(request_id, model_id, "", None)
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except asyncio.TimeoutError:
            # A standards-valid empty delta keeps the local connection alive.
            yield _chunk(request_id, model_id, "", None)
    try:
        content = task.result()
    except Exception as exc:  # streamed responses can no longer change HTTP status
        content = f"MikroFIX je zahtevo varno zavrnil: {exc}"
    yield _chunk(request_id, model_id, content, "stop")
    yield "data: [DONE]\n\n"


app = FastAPI(
    title="MikroFIX Open WebUI Gateway",
    description=(
        "OpenAI-compatible front door. Slash commands are parsed by trusted "
        "manager code and are never sent to a language model."
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
        message, run_id, selected_model = extract_request(body.get("messages"))
        # Parse before starting any model work so unknown slash commands fail closed.
        gateway_command = message.strip().split(None, 1)[0].lower()
        if gateway_command not in {"/model", "/models"}:
            parse_owner_command(message)
    except (ValueError, OwnerCommandError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request_id = f"chatcmpl-mf-{time.time_ns()}"
    selected_model_id = virtual_model_id(role_id)
    operation = lambda: route_message(message, run_id, role_id, selected_model)
    if bool(body.get("stream")):
        return StreamingResponse(
            _stream_response(request_id, selected_model_id, operation),
            media_type="text/event-stream",
        )
    try:
        content = await asyncio.to_thread(operation)
    except (ValueError, OwnerCommandError) as exc:
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
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
