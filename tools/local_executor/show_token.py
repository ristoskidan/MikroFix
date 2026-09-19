#!/usr/bin/env python3
"""Show the bearer token only after a local GUI confirmation."""

from pathlib import Path
import subprocess


TOKEN_FILE = Path.home() / ".config" / "mikrofix-local-executor" / "token"

result = subprocess.run(
    [
        "zenity", "--question", "--title=MikroFIX – ključ lokalnega mostu",
        "--text=Prikažem lokalni API ključ? Ne pošiljaj ga nikomur in ga uporabi samo v lokalnem Open WebUI.",
        "--width=480",
    ],
    check=False,
)
if result.returncode == 0:
    subprocess.run(
        [
            "zenity", "--info", "--title=MikroFIX – lokalni API ključ",
            f"--text={TOKEN_FILE.read_text(encoding='utf-8').strip()}", "--width=620",
        ],
        check=False,
    )
