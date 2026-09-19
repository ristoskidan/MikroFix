#!/usr/bin/env python3
"""Send a traceable MikroFix review task to a local Ollama model.

The bridge deliberately has no write access to application data.  It sends the
canonical context and one bounded task to Ollama, streams the answer to stdout,
and stores a complete Markdown transcript for later human review.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTEXT = PROJECT_ROOT / "docs" / "MIKROFIX_GPT_OSS_20B_KONTEKST.md"
DEFAULT_LOG_DIR = PROJECT_ROOT / "docs" / "local_agents" / "dnevnik"
# Ollama is bound to Docker's host bridge so Open WebUI and local tools can use
# the same server.  The CLI option still permits a different deployment.
OLLAMA_CHAT_URL = "http://172.17.0.1:11434/api/chat"

AGENTS = {
    "qwen": {
        "model": "qwen3.5:9b",
        "role": "glavni lokalni tehnični pomočnik",
    },
    "reviewer": {
        "model": "gpt-oss:20b",
        "role": "skeptični drugi pregledovalec",
    },
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pošlji sledljivo nalogo lokalnemu MikroFix pomočniku in shrani "
            "celoten pogovor v projektni dnevnik."
        )
    )
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="Kratka, konkretna naloga.")
    task.add_argument("--task-file", type=Path, help="UTF-8 datoteka z nalogo.")
    parser.add_argument("--material", default="", help="Kratko gradivo za pregled.")
    parser.add_argument("--material-file", type=Path, help="UTF-8 gradivo za pregled.")
    parser.add_argument(
        "--output",
        default="Jedrnat pregled s sklepom, konflikti, tveganji in manjkajočimi testi.",
        help="Zahtevana oblika oziroma vsebina odgovora.",
    )
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--ollama-url", default=OLLAMA_CHAT_URL)
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=16384,
        help="Kontekstno okno Ollama (privzeto 16384).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zapiši zahtevek brez klica modela.",
    )
    return parser.parse_args(argv)


def read_text(path: Path, label: str) -> str:
    try:
        return path.expanduser().resolve().read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Napaka pri branju {label} '{path}': {exc}") from exc


def build_user_prompt(task: str, material: str, requested_output: str) -> str:
    return (
        "NALOGA:\n"
        f"{task.strip()}\n\n"
        "GRADIVO:\n"
        f"{material.strip() or '(ni dodatnega gradiva)'}\n\n"
        "ZAHTEVANI IZHOD:\n"
        f"{requested_output.strip()}\n"
    )


def make_task_id(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now().astimezone()
    suffix = uuid.uuid4().hex[:6].upper()
    return f"LOCAL-{current:%Y%m%d-%H%M%S}-{suffix}"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def transcript(
    *,
    task_id: str,
    created_at: str,
    agent: str,
    model: str,
    role: str,
    context_path: Path,
    context_hash: str,
    prompt: str,
    answer: str,
    status: str,
) -> str:
    return f"""# {task_id}

- Čas: `{created_at}`
- Agent: `{agent}` — {role}
- Model: `{model}`
- Kontekst: `{context_path}`
- SHA-256 konteksta: `{context_hash}`
- Stanje: **{status}**
- Odločitev glavnega pomočnika: **ČAKA NA PREGLED**

## Poslani uporabniški zahtevek

```text
{prompt.rstrip()}
```

## Nespremenjen odgovor lokalnega modela

```text
{answer.rstrip() or '(odgovor še ni nastal)'}
```

## Pregled glavnega pomočnika

_Sem se zapiše, kaj je sprejeto, zavrnjeno ali zahteva odločitev lastnika._
"""


def stream_ollama(url: str, payload: dict[str, object]) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    chunks: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            for raw_line in response:
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line.decode("utf-8"))
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))
                content = event.get("message", {}).get("content", "")
                if content:
                    chunks.append(content)
                    print(content, end="", flush=True)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Ollama na {url} ni dosegljiva ali je prekinila odgovor: {exc}"
        ) from exc
    print()
    return "".join(chunks)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    agent = AGENTS[args.agent]
    context_path = args.context.expanduser().resolve()
    context = read_text(context_path, "konteksta")
    task = args.task if args.task is not None else read_text(args.task_file, "naloge")
    material_parts = [args.material]
    if args.material_file:
        material_parts.append(read_text(args.material_file, "gradiva"))
    prompt = build_user_prompt(task, "\n\n".join(filter(None, material_parts)), args.output)

    task_id = make_task_id()
    created_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    log_path = args.log_dir.expanduser().resolve() / f"{task_id}.md"

    initial = transcript(
        task_id=task_id,
        created_at=created_at,
        agent=args.agent,
        model=agent["model"],
        role=agent["role"],
        context_path=context_path,
        context_hash=context_hash,
        prompt=prompt,
        answer="",
        status="PRIPRAVLJENO" if args.dry_run else "V TEKU",
    )
    atomic_write(log_path, initial)

    print(f"Naloga: {task_id}", flush=True)
    print(f"Model: {agent['model']}", flush=True)
    print(f"Dnevnik: {log_path}", flush=True)
    if args.dry_run:
        print("Dry-run: model ni bil poklican.")
        return 0

    payload: dict[str, object] = {
        "model": agent["model"],
        "messages": [
            {"role": "system", "content": context},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "keep_alive": "15m",
        "options": {"num_ctx": args.num_ctx},
    }

    print("\n--- odgovor lokalnega modela ---\n", flush=True)
    try:
        answer = stream_ollama(args.ollama_url, payload)
    except (RuntimeError, json.JSONDecodeError) as exc:
        failed = transcript(
            task_id=task_id,
            created_at=created_at,
            agent=args.agent,
            model=agent["model"],
            role=agent["role"],
            context_path=context_path,
            context_hash=context_hash,
            prompt=prompt,
            answer=f"NAPAKA MOSTU: {exc}",
            status="NAPAKA",
        )
        atomic_write(log_path, failed)
        print(f"\nNapaka: {exc}", file=sys.stderr)
        return 2

    completed = transcript(
        task_id=task_id,
        created_at=created_at,
        agent=args.agent,
        model=agent["model"],
        role=agent["role"],
        context_path=context_path,
        context_hash=context_hash,
        prompt=prompt,
        answer=answer,
        status="ODGOVOR PREJET",
    )
    atomic_write(log_path, completed)
    print(f"\nOdgovor je shranjen v: {log_path}")
    print("Lokalni odgovor ni odobritev; pregledati ga mora glavni pomočnik.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
