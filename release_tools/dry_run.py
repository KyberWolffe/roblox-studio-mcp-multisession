"""One-command deterministic release-candidate proof."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from platform_support import (
    require_supported_platform,
    require_supported_runtime,
)

from .audit import audit_archive, audit_repository
from .builder import build_release
from .proof import prove_release
from scripts.validate_capability_parity import validate_capability_parity


class DryRunError(RuntimeError):
    """The local release dry run failed closed."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix="." + path.name + ".tmp-",
        dir=str(path.parent),
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
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


def _stage_assets(
    project_root: Path,
    output_directory: Path,
    *,
    assets: Mapping[str, Tuple[bytes, int]],
) -> Dict[str, Any]:
    output = output_directory.resolve()
    try:
        output.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise DryRunError(
            "release output must be outside the audited repository worktree"
        )
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir():
            raise DryRunError("release output is not a safe directory")
    else:
        output.mkdir(mode=0o700, parents=True)

    unexpected = sorted(
        path.name for path in output.iterdir() if path.name not in assets
    )
    if unexpected:
        raise DryRunError(
            "release output contains unexpected entries: "
            + ", ".join(unexpected)
        )
    for name, (data, mode) in assets.items():
        target = output / name
        if target.exists() and (
            target.is_symlink()
            or not target.is_file()
            or target.read_bytes() != data
        ):
            if target.is_symlink() or not target.is_file():
                raise DryRunError("release output target is unsafe: " + name)
        if not target.exists() or target.read_bytes() != data:
            _atomic_write(target, data, mode)
        else:
            os.chmod(target, mode)
    return {
        "directory": str(output),
        "files": {
            name: {"sha256": _sha256(data), "size": len(data)}
            for name, (data, _mode) in sorted(assets.items())
        },
    }


def run_release_dry_run(
    project_root: Path,
    *,
    output_directory: Optional[Path] = None,
    platform_check: Callable[[], Any] = require_supported_platform,
    runtime_check: Callable[[], Any] = require_supported_runtime,
) -> Dict[str, Any]:
    """Audit, reproduce, inspect, and install-proof the exact local release."""

    # Architecture/runtime checks precede any build or output mutation.
    runtime_check()
    platform = platform_check()
    root = Path(project_root).resolve(strict=True)
    if output_directory is not None:
        resolved_output = Path(output_directory).resolve()
        try:
            resolved_output.relative_to(root)
        except ValueError:
            pass
        else:
            raise DryRunError(
                "release output must be outside the audited repository worktree"
            )
    repository_report = audit_repository(root)
    if not repository_report.ok:
        codes = sorted({item.code for item in repository_report.findings})
        raise DryRunError("repository audit failed: " + ", ".join(codes))
    parity = validate_capability_parity(root)

    with tempfile.TemporaryDirectory(
        prefix="studio-mcp-v2-dry-run-"
    ) as temporary:
        work = Path(temporary)
        first = build_release(root, work / "build-a")
        second = build_release(root, work / "build-b")
        first_assets = {
            first.archive.name: (first.archive.read_bytes(), 0o600),
            first.checksum_file.name: (
                first.checksum_file.read_bytes(),
                0o600,
            ),
            first.bootstrap.name: (first.bootstrap.read_bytes(), 0o700),
            first.bootstrap_checksum_file.name: (
                first.bootstrap_checksum_file.read_bytes(),
                0o600,
            ),
            first.checksum_manifest.name: (
                first.checksum_manifest.read_bytes(),
                0o600,
            ),
        }
        second_assets = {
            second.archive.name: second.archive.read_bytes(),
            second.checksum_file.name: second.checksum_file.read_bytes(),
            second.bootstrap.name: second.bootstrap.read_bytes(),
            second.bootstrap_checksum_file.name: (
                second.bootstrap_checksum_file.read_bytes()
            ),
            second.checksum_manifest.name: (
                second.checksum_manifest.read_bytes()
            ),
        }
        if (
            first.sha256 != second.sha256
            or first.bootstrap_sha256 != second.bootstrap_sha256
            or {name: data for name, (data, _mode) in first_assets.items()}
            != second_assets
        ):
            raise DryRunError(
                "two clean release builds were not byte-for-byte reproducible"
            )
        archive_report = audit_archive(first.archive)
        if not archive_report.ok:
            codes = sorted({item.code for item in archive_report.findings})
            raise DryRunError("release archive audit failed: " + ", ".join(codes))
        proof = prove_release(
            first.archive,
            checksum_file=first.checksum_file,
            temporary_parent=work,
        )
        staged = None
        if output_directory is not None:
            staged = _stage_assets(
                root,
                Path(output_directory),
                assets=first_assets,
            )
        manifest_file_count = len(first.manifest["files"])
        archive_name = first.archive.name
        archive_sha256 = first.sha256
        version = str(first.manifest["version"])

    platform_value = (
        platform.as_dict() if callable(getattr(platform, "as_dict", None)) else None
    )
    return {
        "ok": True,
        "version": version,
        "platform": platform_value or "macos-arm64",
        "repository_audit": {
            "ok": True,
            "files_checked": repository_report.files_checked,
            "bytes_checked": repository_report.bytes_checked,
        },
        "capability_parity": {
            "ok": True,
            "modern_tool_count": parity["modern_tool_count"],
            "p0_complete": parity["p0_complete"],
            "p0_gap_count": parity["p0_gap_count"],
            "full_parity_claimed": parity["full_parity_claimed"],
        },
        "reproducible_build": {
            "ok": True,
            "archive": archive_name,
            "sha256": archive_sha256,
            "manifest_file_count": manifest_file_count,
            "build_count": 2,
        },
        "archive_audit": {
            "ok": True,
            "files_checked": archive_report.files_checked,
            "bytes_checked": archive_report.bytes_checked,
        },
        "isolated_install_proof": proof,
        "staged_assets": staged,
        "live_state_touched": False,
    }
