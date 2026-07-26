#!/usr/bin/env python3
"""Reproduce and prove the complete macOS arm64 release candidate locally."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from platform_support import UnsupportedPlatformError  # noqa: E402
from release_tools.dry_run import DryRunError, run_release_dry_run  # noqa: E402
from release_tools.proof import ProofError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the repository, build twice, compare bytes, audit the "
            "archive, and exercise it inside an auto-removed synthetic home."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "optional directory outside the repository for the proven archive, "
            "checksums, and bootstrap asset"
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = run_release_dry_run(
            PROJECT_ROOT,
            output_directory=args.output_dir,
        )
    except (DryRunError, OSError, ProofError, UnsupportedPlatformError) as exc:
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
