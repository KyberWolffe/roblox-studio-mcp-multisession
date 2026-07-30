#!/usr/bin/env python3
"""Prove exact Multisession rc.5 -> rc.7 -> rc.5 rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.multisession_update_proof import (  # noqa: E402
    ProofError,
    prove_multisession_update_rollback,
)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise ProofError("proof output target must be fresh")
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
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise ProofError("proof output target must remain fresh") from exc
        temporary.unlink()
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


def _contains_path(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_output_paths(
    output: Path,
    *,
    protected_inputs: Iterable[Path],
    project_root: Path = PROJECT_ROOT,
    user_home: Optional[Path] = None,
) -> Tuple[Path, Path]:
    target = Path(output).expanduser().resolve()
    checksum = target.with_name(target.name + ".sha256")
    if (
        target.exists()
        or target.is_symlink()
        or checksum.exists()
        or checksum.is_symlink()
    ):
        raise ProofError(
            "proof output and checksum paths must both be fresh"
        )
    home = (
        Path(pwd.getpwuid(os.getuid()).pw_dir)
        if user_home is None
        else Path(user_home)
    ).resolve(strict=True)
    roots = (
        Path(project_root).resolve(strict=True),
        home / ".codex",
        home / "Documents" / "Roblox" / "Plugins",
        home
        / "Library"
        / "Application Support"
        / "RobloxStudioMCPv2",
    )
    for candidate in (target, checksum):
        if any(
            _contains_path(root.resolve(), candidate)
            for root in roots
        ):
            raise ProofError(
                "proof output path overlaps source or a known live root"
            )
    for value in protected_inputs:
        protected = Path(value).expanduser().resolve(strict=True)
        if target == protected or checksum == protected:
            raise ProofError("proof output path collides with a proof input")
    return target, checksum


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install exact immutable 0.4.0-rc.5 and an exact rc.7 "
            "candidate in one disposable synthetic home, verify the "
            "transactional update and immediate rollback target, restore "
            "all active bytes/modes/registration, and emit non-secret JSON."
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
    try:
        output, checksum = _validated_output_paths(
            args.output,
            protected_inputs=(
                args.prior_archive,
                args.prior_checksum_file,
                args.candidate_archive,
                args.candidate_checksum_file,
            ),
        )
        report = prove_multisession_update_rollback(
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
            source_repository=PROJECT_ROOT,
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
