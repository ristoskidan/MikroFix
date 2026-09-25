#!/usr/bin/env python3
"""Send a traceable MikroFix review task to a local Ollama model.

The bridge deliberately has no write access to application data.  It sends the
canonical context and one bounded task to Ollama, streams the answer to stdout,
and stores a complete Markdown transcript for later human review.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
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
DEFAULT_LESSONS = PROJECT_ROOT / "docs" / "local_agents" / "LEKCIJE.md"
DEFAULT_RULES_DIR = PROJECT_ROOT / "docs" / "local_agents" / "pravila"
DEFAULT_LOG_DIR = PROJECT_ROOT / "docs" / "local_agents" / "dnevnik"
DEFAULT_CONSOLE_LOG = (
    Path.home() / ".local" / "state" / "mikrofix" / "agent-console.log"
)
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

BRIDGE_SYSTEM_SUFFIX = """

---
MOST ZA DELEGIRANO NALOGO:
Uvodna potrditev, da je kontekst nalozen, je bila ze opravljena. Uporabnisko
sporocilo v tem klicu je konkretna operativna naloga. Odgovori neposredno na
NALOGO in upostevaj ZAHTEVANI IZHOD; ne odgovori samo s potrditvijo pripravljenosti.
"""


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
    parser.add_argument(
        "--project-memory", type=Path,
        help="Potrjeni trajni kontekst nadrejene lokalne seje.",
    )
    parser.add_argument(
        "--parent-session-id", default="",
        help="ID glavne seje, iz katere izvira ta podizvajalska naloga.",
    )
    parser.add_argument("--lessons", type=Path, default=DEFAULT_LESSONS)
    parser.add_argument("--rules-dir", type=Path, default=DEFAULT_RULES_DIR)
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help=(
            "Tematski obseg pravil; možnost se lahko ponovi. Brez nje most "
            "izbere obsege po nalogi in ob nejasnosti naloži vse."
        ),
    )
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--console-log",
        type=Path,
        default=DEFAULT_CONSOLE_LOG,
        help="Skupni tok za stalno vidno terminalsko okno.",
    )
    parser.add_argument("--ollama-url", default=OLLAMA_CHAT_URL)
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=32768,
        help="Kontekstno okno Ollama (privzeto 32768).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1600,
        help="Najvecje stevilo tokenov odgovora (privzeto 1600).",
    )
    parser.add_argument(
        "--thinking",
        choices=("low", "medium", "high"),
        default="medium",
        help="Stopnja sklepanja za GPT-OSS (privzeto medium).",
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


def load_scoped_context(
    rules_dir: Path,
    canonical_path: Path,
    requested_scopes: list[str],
    searchable_text: str,
) -> tuple[str, list[tuple[Path, str]], list[str]]:
    rules_dir = rules_dir.expanduser().resolve()
    canonical_path = canonical_path.expanduser().resolve()
    index_path = rules_dir / "INDEX.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Napaka pri branju indeksa pravil '{index_path}': {exc}") from exc

    canonical = read_text(canonical_path, "kanoničnega konteksta")
    canonical_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if canonical_hash != index.get("source_sha256"):
        raise SystemExit(
            "Moduli pravil niso usklajeni s kanoničnim kontekstom. Zaženi "
            "python3 tools/build_local_agent_rules.py."
        )

    scopes = index.get("scopes", {})
    unknown = sorted(set(requested_scopes) - set(scopes) - {"all"})
    if unknown:
        raise SystemExit(
            "Neznani obsegi pravil: " + ", ".join(unknown)
            + ". Dovoljeni: " + ", ".join(sorted(scopes))
        )

    if "all" in requested_scopes:
        selected = list(scopes)
    elif requested_scopes:
        selected = list(dict.fromkeys(requested_scopes))
    else:
        haystack = searchable_text.casefold()
        selected = [
            name
            for name, definition in scopes.items()
            if any(keyword.casefold() in haystack for keyword in definition["keywords"])
        ]
        if not selected:
            selected = list(scopes)

    file_names = list(index.get("always", []))
    file_names.extend(scopes[name]["file"] for name in selected)
    file_names = list(dict.fromkeys(file_names))
    loaded: list[tuple[Path, str]] = []
    blocks: list[str] = []
    for file_name in file_names:
        path = (rules_dir / file_name).resolve()
        if path.parent != rules_dir:
            raise SystemExit(f"Nedovoljena pot v indeksu pravil: {file_name}")
        content = read_text(path, "modula pravil")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        loaded.append((path, digest))
        blocks.append(f"# NALOŽEN MODUL: {file_name}\n\n{content.strip()}")
    return "\n\n---\n\n".join(blocks), loaded, selected


def build_system_prompt(context: str, lessons: str = "") -> str:
    """Adapt the reusable chat context for a one-shot delegated API call."""
    lesson_block = ""
    if lessons.strip():
        lesson_block = "\n\n---\nTRAJNE POTRJENE LEKCIJE:\n" + lessons.strip()
    return context.rstrip() + lesson_block + BRIDGE_SYSTEM_SUFFIX


def build_payload(
    model: str,
    context: str,
    prompt: str,
    num_ctx: int,
    max_tokens: int = 1600,
    lessons: str = "",
    thinking: str = "medium",
) -> dict[str, object]:
    reasoning: bool | str = thinking if model.startswith("gpt-oss:") else False
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(context, lessons)},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "think": reasoning,
        "keep_alive": "15m",
        "options": {"num_ctx": num_ctx, "num_predict": max_tokens},
    }


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
    context_files: list[tuple[Path, str]],
    selected_scopes: list[str],
    lessons_path: Path,
    lessons_hash: str,
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
- Izbrani obsegi: `{', '.join(selected_scopes)}`
- Naloženi moduli:
{os.linesep.join(f'  - `{path}` — `{digest}`' for path, digest in context_files)}
- Potrjene lekcije: `{lessons_path}`
- SHA-256 lekcij: `{lessons_hash}`
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


def console_header(
    *,
    task_id: str,
    created_at: str,
    agent: str,
    model: str,
    role: str,
    selected_scopes: list[str],
    context_files: list[tuple[Path, str]],
    task: str,
    requested_output: str,
    material_file: Path | None,
) -> str:
    material = str(material_file.expanduser().resolve()) if material_file else "brez datoteke"
    modules = ", ".join(path.name for path, _ in context_files)
    visible_agent = "QWEN" if agent == "qwen" else "GPT-OSS"
    return f"""
========================================================================
######################## {visible_agent} ########################
NALOGA: {task_id}
CAS: {created_at}
AGENT: {agent} — {role}
MODEL: {model}
OBSEGI PRAVIL: {', '.join(selected_scopes)}
MODULI: {modules}
GRADIVO: {material}

ZAHTEVA:
{task.strip()}

ZAHTEVANI IZHOD:
{requested_output.strip()}

--- ODGOVOR LOKALNEGA MODELA ---
"""


def console_write(handle, content: str) -> None:
    handle.write(content)
    handle.flush()


def stream_ollama(url: str, payload: dict[str, object], console_handle=None) -> str:
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
                    if console_handle is not None:
                        console_write(console_handle, content)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Ollama na {url} ni dosegljiva ali je prekinila odgovor: {exc}"
        ) from exc
    print()
    return "".join(chunks)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    agent = AGENTS[args.agent]
    lessons_path = args.lessons.expanduser().resolve()
    lessons = read_text(lessons_path, "potrjenih lekcij")
    task = args.task if args.task is not None else read_text(args.task_file, "naloge")
    material_parts = [args.material]
    if args.material_file:
        material_parts.append(read_text(args.material_file, "gradiva"))
    material = "\n\n".join(filter(None, material_parts))
    prompt = build_user_prompt(task, material, args.output)
    context_path = args.rules_dir.expanduser().resolve()
    context, context_files, selected_scopes = load_scoped_context(
        context_path,
        args.context,
        args.scope,
        task + "\n" + args.output,
    )
    if args.project_memory:
        memory_path = args.project_memory.expanduser().resolve()
        memory = read_text(memory_path, "trajnega projektnega spomina")
        memory_hash = hashlib.sha256(memory.encode("utf-8")).hexdigest()
        context_files.append((memory_path, memory_hash))
        context = (
            context.rstrip()
            + "\n\n---\n\n# POD­EDOVANI KONTEKST GLAVNE SEJE\n\n"
            + f"parent_session_id: {args.parent_session_id}\n\n"
            + memory
        )

    task_id = make_task_id()
    created_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    lessons_hash = hashlib.sha256(lessons.encode("utf-8")).hexdigest()
    log_path = args.log_dir.expanduser().resolve() / f"{task_id}.md"

    console_path = args.console_log.expanduser().resolve()
    console_path.parent.mkdir(parents=True, exist_ok=True)
    console_handle = console_path.open("a", encoding="utf-8")
    # En sam GPU ne sme obdelovati dveh lokalnih nalog hkrati. Zaklep tudi
    # zagotovi, da se odgovora v skupnem terminalsko vidnem toku ne prepletata.
    fcntl.flock(console_handle.fileno(), fcntl.LOCK_EX)
    console_write(
        console_handle,
        console_header(
            task_id=task_id,
            created_at=created_at,
            agent=args.agent,
            model=agent["model"],
            role=agent["role"],
            selected_scopes=selected_scopes,
            context_files=context_files,
            task=task,
            requested_output=args.output,
            material_file=args.material_file,
        ),
    )

    initial = transcript(
        task_id=task_id,
        created_at=created_at,
        agent=args.agent,
        model=agent["model"],
        role=agent["role"],
        context_path=context_path,
        context_hash=context_hash,
        context_files=context_files,
        selected_scopes=selected_scopes,
        lessons_path=lessons_path,
        lessons_hash=lessons_hash,
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
        console_write(
            console_handle,
            f"(DRY-RUN: model ni bil poklican.)\nDNEVNIK: {log_path}\n",
        )
        console_handle.close()
        return 0

    payload = build_payload(
        agent["model"], context, prompt, args.num_ctx, args.max_tokens, lessons,
        args.thinking,
    )

    print("\n--- odgovor lokalnega modela ---\n", flush=True)
    try:
        answer = stream_ollama(args.ollama_url, payload, console_handle)
        if not answer.strip():
            raise RuntimeError("Model ni vrnil koncnega vsebinskega odgovora.")
    except KeyboardInterrupt:
        interrupted = transcript(
            task_id=task_id,
            created_at=created_at,
            agent=args.agent,
            model=agent["model"],
            role=agent["role"],
            context_path=context_path,
            context_hash=context_hash,
            context_files=context_files,
            selected_scopes=selected_scopes,
            lessons_path=lessons_path,
            lessons_hash=lessons_hash,
            prompt=prompt,
            answer="NALOGA JE BILA PREKINJENA PRED KONČNIM ODGOVOROM.",
            status="PREKINJENO",
        )
        atomic_write(log_path, interrupted)
        console_write(
            console_handle,
            f"\n--- PREKINJENO ---\nDNEVNIK: {log_path}\n",
        )
        console_handle.close()
        print("\nLokalna naloga je bila prekinjena.", file=sys.stderr)
        return 130
    except (RuntimeError, json.JSONDecodeError) as exc:
        failed = transcript(
            task_id=task_id,
            created_at=created_at,
            agent=args.agent,
            model=agent["model"],
            role=agent["role"],
            context_path=context_path,
            context_hash=context_hash,
            context_files=context_files,
            selected_scopes=selected_scopes,
            lessons_path=lessons_path,
            lessons_hash=lessons_hash,
            prompt=prompt,
            answer=f"NAPAKA MOSTU: {exc}",
            status="NAPAKA",
        )
        atomic_write(log_path, failed)
        print(f"\nNapaka: {exc}", file=sys.stderr)
        console_write(
            console_handle,
            f"\n--- NAPAKA ---\n{exc}\nDNEVNIK: {log_path}\n",
        )
        console_handle.close()
        return 2

    completed = transcript(
        task_id=task_id,
        created_at=created_at,
        agent=args.agent,
        model=agent["model"],
        role=agent["role"],
        context_path=context_path,
        context_hash=context_hash,
        context_files=context_files,
        selected_scopes=selected_scopes,
        lessons_path=lessons_path,
        lessons_hash=lessons_hash,
        prompt=prompt,
        answer=answer,
        status="ODGOVOR PREJET",
    )
    atomic_write(log_path, completed)
    print(f"\nOdgovor je shranjen v: {log_path}")
    print("Lokalni odgovor ni odobritev; pregledati ga mora glavni pomočnik.")
    console_write(
        console_handle,
        "\n\n--- KONEC ODGOVORA ---\n"
        f"DNEVNIK: {log_path}\n"
        "STANJE: caka na pregled glavnega pomocnika.\n",
    )
    console_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
