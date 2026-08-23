#!/usr/bin/env python3
"""Validate the minimum SARIF shape before publishing scan results."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate-sarif.py FILE", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        runs = document["runs"]
        if document["version"] != "2.1.0" or not isinstance(runs, list) or not runs:
            raise ValueError("missing SARIF version or runs")
        for run in runs:
            tool = run["tool"]["driver"]["name"]
            if not isinstance(tool, str) or not tool:
                raise ValueError("missing SARIF tool name")
            if not isinstance(run["results"], list):
                raise ValueError("SARIF results must be a list")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid SARIF report {path}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
