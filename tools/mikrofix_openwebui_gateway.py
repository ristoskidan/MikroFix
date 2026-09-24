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
    from tools.mikrofix_local_manager import start_conversation, talk
    from tools.mikrofix_local_manager_ui import DEFAULT_WORKSPACE
    from tools.mikrofix_local_web import dispatch_owner_command
    from tools.mikrofix_owner_commands import OwnerCommandError, parse_owner_command
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from mikrofix_local_manager import start_conversation, talk  # type: ignore
    from mikrofix_local_manager_ui import DEFAULT_WORKSPACE  # type: ignore
    from mikrofix_local_web import dispatch_owner_command  # type: ignore
    from mikrofix_owner_commands import (  # type: ignore
        OwnerCommandError, parse_owner_command,
    )


MODEL_ID = "mikrofix-local-team"
MODEL_NAME = "MikroFIX lokalna ekipa"
LEAD_PROFILE = "gpt_oss"
RUN_MARKER = re.compile(r"(?:MikroFIX pogovor|Pogovor):\s*(MF-LOCAL-[A-Za-z0-9_-]+)")
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


def extract_request(messages: object) -> tuple[str, str]:
    """Return the latest owner message and prior immutable conversation id."""
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    owner_message = ""
    run_id = ""
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
    if not owner_message:
        raise ValueError("The latest owner message is empty")
    return owner_message, run_id


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


def route_message(message: str, run_id: str) -> str:
    """Route slash commands to trusted code and ordinary text to the locked lead."""
    command = parse_owner_command(message)
    if command is not None:
        if command.name != "/help" and not run_id:
            raise ValueError(
                f"Ukaz {command.name} potrebuje obstoječi MikroFIX pogovor."
            )
        result = dispatch_owner_command(WORKSPACE, run_id, message)
        response = str(result.get("message") or "Ukaz je bil sprejet.").strip()
        return response + (f"\n\nMikroFIX pogovor: {run_id}" if run_id else "")
    if run_id:
        conversation = talk(
            message, parent_run_id=run_id, workspace=WORKSPACE,
            lead_profile=LEAD_PROFILE,
        )
    else:
        conversation = start_conversation(
            message, workspace=WORKSPACE, lead_profile=LEAD_PROFILE,
        )
    reply, actual_run_id = _latest_reply(conversation)
    return f"{reply}\n\nMikroFIX pogovor: {actual_run_id}"


def _chunk(request_id: str, content: str, finish_reason: str | None) -> str:
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": finish_reason,
        }],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _stream_response(
    request_id: str, operation: Callable[[], str],
):
    task = asyncio.create_task(asyncio.to_thread(operation))
    yield _chunk(request_id, "", None)
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except asyncio.TimeoutError:
            # A standards-valid empty delta keeps the local connection alive.
            yield _chunk(request_id, "", None)
    try:
        content = task.result()
    except Exception as exc:  # streamed responses can no longer change HTTP status
        content = f"MikroFIX je zahtevo varno zavrnil: {exc}"
    yield _chunk(request_id, content, "stop")
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
        "status": "ok", "model": MODEL_ID, "lead_profile": LEAD_PROFILE,
        "workspace": str(WORKSPACE),
    }


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)) -> dict[str, object]:
    authenticate(authorization)
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ID, "object": "model", "created": 0,
            "owned_by": "mikrofix",
        }],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    body: dict[str, Any], authorization: str | None = Header(default=None),
):
    authenticate(authorization)
    if body.get("model") != MODEL_ID:
        raise HTTPException(status_code=404, detail="Unknown MikroFIX model")
    try:
        message, run_id = extract_request(body.get("messages"))
        # Parse before starting any model work so unknown slash commands fail closed.
        parse_owner_command(message)
    except (ValueError, OwnerCommandError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request_id = f"chatcmpl-mf-{time.time_ns()}"
    operation = lambda: route_message(message, run_id)
    if bool(body.get("stream")):
        return StreamingResponse(
            _stream_response(request_id, operation),
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
        "model": MODEL_ID,
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
