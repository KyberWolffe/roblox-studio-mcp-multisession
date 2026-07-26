#!/usr/bin/env python3
"""Audit a GitHub candidate repository and deterministic release archives."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.audit import audit_many  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail closed on local state, credentials, absolute user paths, "
            "unsafe artifacts, or non-deterministic release contents."
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        help="candidate repository root; .git metadata alone is excluded",
    )
    parser.add_argument(
        "--archive",
        action="append",
        type=Path,
        default=[],
        help="release .tar.gz to audit; may be repeated",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = audit_many(repository=args.repo, archives=args.archive)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "error": str(exc), "reports": []}
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
