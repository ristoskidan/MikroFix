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
    implementation_path = project / "mikrofix_v30_receipts.py"
    tests_path = project / "test_v30_receipts.py"
    implementation_lines = implementation_path.read_text(encoding="utf-8").splitlines()
    test_lines = tests_path.read_text(encoding="utf-8").splitlines()
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
    print(json.dumps({
        "implementation": {
            "path": implementation_path.name,
            "lines": "47-75",
            "content": "\n".join(implementation_lines[46:75]),
        },
        "existing_tests": {
            "path": tests_path.name,
            "lines": "60-73",
            "content": "\n".join(test_lines[59:73]),
        },
        "observations": observations,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
