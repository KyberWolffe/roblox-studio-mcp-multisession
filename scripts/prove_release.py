#!/usr/bin/env python3
"""Exercise a verified release archive in a disposable synthetic home."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.proof import ProofError, prove_release  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify, audit, install, diagnose, repair, and uninstall a release "
            "inside a temporary HOME without starting a broker."
        )
    )
    parser.add_argument("--archive", type=Path, required=True)
    verification = parser.add_mutually_exclusive_group(required=True)
    verification.add_argument("--checksum-file", type=Path)
    verification.add_argument("--expected-sha256")
    parser.add_argument(
        "--temporary-parent",
        type=Path,
        help="optional existing parent for the auto-removed proof directory",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = prove_release(
            args.archive,
            checksum_file=args.checksum_file,
            expected_sha256=args.expected_sha256,
            temporary_parent=args.temporary_parent,
        )
    except (OSError, ProofError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(1)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
