#!/usr/bin/env python3
"""Read-only fixed-input probe for the receipt decimal parser."""

from __future__ import annotations

import json
from pathlib import Path
import sys


VALUES = ("NaN", "Infinity", "-Infinity", "sNaN", None, "", "-0", "-0.01")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: decimal_value_edges.py PROJECT_ROOT")
    project = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(project))
    from mikrofix_v30_receipts import decimal_value  # noqa: PLC0415

    observations = []
    for value in VALUES:
        try:
            result = decimal_value(value, "Znesek")
            observations.append({
                "input": repr(value), "outcome": "value", "value": str(result),
            })
        except Exception as exc:  # record the public boundary for fixed inputs
            observations.append({
                "input": repr(value), "outcome": "error",
                "error_type": type(exc).__name__, "message": str(exc),
            })
    print(json.dumps({"observations": observations}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
