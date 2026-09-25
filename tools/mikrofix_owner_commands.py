#!/usr/bin/env python3
"""Strict owner slash-command parsing for the local manager interfaces."""

from __future__ import annotations

from dataclasses import dataclass


class OwnerCommandError(ValueError):
    """An owner command is unknown or has invalid arguments."""


@dataclass(frozen=True)
class OwnerCommand:
    name: str
    argument: str = ""


ALIASES = {
    "/prepareplan": "/prepare-plan",
    "/plandispatch": "/plan-dispatch",
    "/preparereview": "/prepare-review",
    "/codexrejected": "/codex-rejected",
    "/codexapproved": "/codex-approved",
    "/approveplan": "/approve-plan",
    "/codexopinion": "/codex-opinion",
}

NO_ARGUMENT = frozenset({
    "/prepare-plan",
    "/plan-dispatch",
    "/approve-plan",
    "/prepare-review",
    "/status",
    "/help",
})
REQUIRED_ARGUMENT = frozenset({
    "/codex-rejected",
    "/codex-approved",
    "/replace-goal",
})
OPTIONAL_ARGUMENT = frozenset({"/codex-opinion"})
COMMAND_NAMES = NO_ARGUMENT | REQUIRED_ARGUMENT | OPTIONAL_ARGUMENT


def parse_owner_command(message: str) -> OwnerCommand | None:
    """Return a validated command, or ``None`` for ordinary conversation."""
    clean = message.strip()
    if not clean.startswith("/"):
        return None
    parts = clean.split(None, 1)
    token = parts[0]
    name = ALIASES.get(token.lower(), token.lower())
    if name not in COMMAND_NAMES:
        raise OwnerCommandError(
            f"Neznan ukaz {token}. Uporabi /help za seznam ukazov."
        )
    argument = parts[1].strip() if len(parts) == 2 else ""
    if name in NO_ARGUMENT and argument:
        raise OwnerCommandError(f"Ukaz {name} ne sprejme dodatnega besedila.")
    if name in REQUIRED_ARGUMENT and not argument:
        description = {
            "/codex-rejected": "razlog zavrnitve",
            "/codex-approved": "commit SHA",
            "/replace-goal": "nov potrjeni cilj",
        }[name]
        raise OwnerCommandError(f"Po ukazu {name} napiši {description}.")
    return OwnerCommand(name=name, argument=argument)


def help_text() -> str:
    return "\n".join((
        "Razpoložljivi ukazi:",
        "/prepare-plan — pripravi izvedbeni načrt iz pogovora",
        "/plan-dispatch — pošlje naprej zadnjo prikazano revizijo načrta",
        "/replace-goal <nov cilj> — izrecno zamenja glavni cilj pogovora",
        "/approve-plan — dovoli in zažene potrjeni načrt",
        "/prepare-review — pripravi nespremenljiv paket za Codex",
        "/codex-rejected <razlog> — zabeleži zavrnitev",
        "/codex-approved <commit SHA> — zabeleži odobritev in uporabo commita",
        "/codex-opinion [vprašanje] — pripravi vprašanje za Codex brez sprememb",
        "/status — pokaže stanje in trenutno dovoljene ukaze",
        "/help — pokaže ta seznam",
    ))
