#!/usr/bin/env python3
"""Build the explicit-allowlist portable Studio MCP v2 archive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.builder import build_release  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "dist",
    )
    args = parser.parse_args()
    result = build_release(PROJECT_ROOT, args.output_dir)
    print(
        json.dumps(
            {
                "archive": str(result.archive),
                "checksum_file": str(result.checksum_file),
                "sha256": result.sha256,
                "bootstrap": str(result.bootstrap),
                "bootstrap_checksum_file": str(
                    result.bootstrap_checksum_file
                ),
                "bootstrap_sha256": result.bootstrap_sha256,
                "checksum_manifest": str(result.checksum_manifest),
                "version": result.manifest["version"],
                "file_count": len(result.manifest["files"]),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
