#!/usr/bin/env python3
"""Prove exact 0.4.0-rc.4 -> Multisession -> rc.4 rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.cross_version_proof import (  # noqa: E402
    ProofError,
    prove_multisession_migration_rollback,
)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    target = Path(path)
    if target.exists() and (
        target.is_symlink() or not target.is_file()
    ):
        raise ProofError("proof output target is unsafe")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix="." + target.name + ".tmp-",
        dir=str(target.parent),
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, mode)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the immutable 0.4.0-rc.4 and exact Multisession "
            "candidate installers in one disposable home, prove the "
            "single-registration migration and byte/mode rollback, then "
            "uninstall and emit non-secret JSON."
        )
    )
    parser.add_argument("--prior-archive", type=Path, required=True)
    parser.add_argument(
        "--prior-checksum-file", type=Path, required=True
    )
    parser.add_argument("--candidate-archive", type=Path, required=True)
    parser.add_argument(
        "--candidate-checksum-file", type=Path, required=True
    )
    parser.add_argument(
        "--candidate-expected-sha256", required=True
    )
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--temporary-parent", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output = args.output.resolve()
    try:
        output.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise SystemExit(
            "proof output must be outside the source repository"
        )
    try:
        report = prove_multisession_migration_rollback(
            prior_archive=args.prior_archive,
            prior_checksum_file=args.prior_checksum_file,
            candidate_archive=args.candidate_archive,
            candidate_checksum_file=args.candidate_checksum_file,
            candidate_expected_sha256=(
                args.candidate_expected_sha256
            ),
            candidate_version=args.candidate_version,
            source_commit=args.source_commit,
            source_tree=args.source_tree,
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
    raw = (
        json.dumps(
            report, indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(output, raw, 0o600)
    digest = hashlib.sha256(raw).hexdigest()
    checksum = output.with_name(output.name + ".sha256")
    _atomic_write(
        checksum,
        (digest + "  " + output.name + "\n").encode("ascii"),
        0o600,
    )
    print(
        json.dumps(
            {
                **report,
                "proof_filename": output.name,
                "proof_sha256": digest,
                "proof_checksum_filename": checksum.name,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
