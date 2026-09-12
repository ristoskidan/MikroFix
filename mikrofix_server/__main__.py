"""Run the development server on loopback only."""

from __future__ import annotations

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "mikrofix_server.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8765,
    )

