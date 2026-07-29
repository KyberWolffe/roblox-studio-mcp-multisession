#!/usr/bin/env python3
"""Produce a fail-closed native Studio compile receipt for one exact plugin."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.native_compile import (  # noqa: E402
    DEFAULT_STUDIO_EXECUTABLE,
    NativeCompileError,
    prove_native_studio_compilation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile one SHA-pinned rendered Studio plugin with the native "
            "arm64 Roblox Studio Luau compiler. This does not install the "
            "plugin or open a place."
        )
    )
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--expected-package-sha256", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--studio-executable",
        type=Path,
        default=DEFAULT_STUDIO_EXECUTABLE,
    )
    parser.add_argument(
        "--expected-studio-executable-sha256",
        required=True,
        help="lowercase SHA-256 of the exact signed Studio executable",
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument(
        "--temporary-parent",
        type=Path,
        help="existing parent for the auto-removed private runner directory",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prove_native_studio_compilation(
            args.package,
            expected_package_sha256=args.expected_package_sha256,
            expected_source_sha256=args.expected_source_sha256,
            receipt_path=args.receipt,
            expected_studio_executable_sha256=(
                args.expected_studio_executable_sha256
            ),
            studio_executable=args.studio_executable,
            timeout_seconds=args.timeout_seconds,
            temporary_parent=args.temporary_parent,
        )
    except (NativeCompileError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "ok": False,
                    "receipt": str(args.receipt.resolve()),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
