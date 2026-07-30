"""Build a deterministic Studio MCP Multisession release archive.

The rc.5 migration bridge deliberately retains the historical archive and
bootstrap filename prefixes so the immutable rc.4 updater can authenticate
and ingest the candidate without changing its compatibility contract.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import platform_support

from . import installer


ARCHIVE_BASENAME = (
    "roblox-studio-mcp-v2-"
    + installer.VERSION
    + "-"
    + platform_support.TARGET_PLATFORM
)
ARCHIVE_FILENAME = ARCHIVE_BASENAME + ".tar.gz"
BOOTSTRAP_FILENAME = (
    "roblox-studio-mcp-v2-bootstrap-" + installer.VERSION + ".py"
)
CHECKSUM_MANIFEST_FILENAME = "SHA256SUMS"

# This is intentionally a file allowlist, not a repository traversal.  In
# particular it cannot reach credential-bearing live-v2-run directories.
RUNTIME_MODULES: Sequence[str] = (
    "__init__.py",
    "auth.py",
    "catalog.py",
    "catalog_review.py",
    "errors.py",
    "frontend.py",
    "http_api.py",
    "hub.py",
    "lifecycle.py",
    "mcp_stdio.py",
    "multi_edit.py",
    "play_bridge.py",
    "registry.py",
    "service.py",
    "session.py",
    "schema_validation.py",
    "validation.py",
)

PACKAGE_SOURCES: Sequence[Tuple[str, str, int]] = (
    ("release_tools/installer.py", "install.py", 0o755),
    ("platform_support.py", "platform_support.py", 0o644),
    ("release_tools/updater.py", "release_updater.py", 0o644),
    ("release_tools/bootstrap.py", "bootstrap.py", 0o755),
    (
        "release_tools/runtime_launcher.py",
        "launcher-template.py",
        0o644,
    ),
    ("release_tools/PORTABLE_INSTALL.md", "INSTALL.md", 0o644),
    (
        "config/durable-tool-catalog.json",
        "payload/config/durable-tool-catalog.json",
        0o644,
    ),
    (
        "config/tool-catalog.json",
        "payload/config/tool-catalog.json",
        0o644,
    ),
    (
        "config/upstream-compatibility-map.json",
        "payload/config/upstream-compatibility-map.json",
        0o644,
    ),
    (
        "config/v1-capability-parity.json",
        "payload/config/v1-capability-parity.json",
        0o644,
    ),
    (
        "docs/CAPABILITY_PARITY.md",
        "CAPABILITY_PARITY.md",
        0o644,
    ),
    (
        "scripts/render_studio_plugin.py",
        "payload/scripts/render_studio_plugin.py",
        0o644,
    ),
    (
        "scripts/studio_plugin_template.luau",
        "payload/scripts/studio_plugin_template.luau",
        0o644,
    ),
    (
        "scripts/play_server_bridge.luau",
        "payload/scripts/play_server_bridge.luau",
        0o644,
    ),
    (
        "scripts/durable_operation_handlers.luau",
        "payload/scripts/durable_operation_handlers.luau",
        0o644,
    ),
    (
        "scripts/review_upstream_catalog.py",
        "payload/scripts/review_upstream_catalog.py",
        0o755,
    ),
)

FORBIDDEN_PORTABLE_FRAGMENTS: Sequence[bytes] = (
    b"live-v2-run",
    b"host-context.json",
    b"client-context.json",
    b"run-manifest.json",
    b"/Users/",
)


@dataclass(frozen=True)
class BuiltRelease:
    archive: Path
    checksum_file: Path
    sha256: str
    manifest: Dict[str, object]
    bootstrap: Path
    bootstrap_checksum_file: Path
    bootstrap_sha256: str
    checksum_manifest: Path


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _read_explicit_sources(project_root: Path) -> List[Tuple[str, bytes, int]]:
    items: List[Tuple[str, bytes, int]] = []
    for source_relative, archive_relative, mode in PACKAGE_SOURCES:
        source = project_root / source_relative
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(
                "required release source is missing: " + source_relative
            )
        items.append((archive_relative, source.read_bytes(), mode))
    for module in RUNTIME_MODULES:
        source_relative = "studio_mcp_v2/" + module
        source = project_root / source_relative
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(
                "required runtime module is missing: " + source_relative
            )
        items.append(
            ("payload/studio_mcp_v2/" + module, source.read_bytes(), 0o644)
        )
    items.sort(key=lambda item: item[0])
    return items


def _validate_portable_content(items: Iterable[Tuple[str, bytes, int]]) -> None:
    paths = set()
    for relative, data, _ in items:
        if relative in paths:
            raise ValueError("duplicate archive path: " + relative)
        paths.add(relative)
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("archive path is unsafe: " + relative)
        lowered = relative.lower().encode("utf-8")
        for fragment in FORBIDDEN_PORTABLE_FRAGMENTS:
            if fragment.lower() in lowered or fragment in data:
                raise ValueError(
                    "portable release contains forbidden local/run material: "
                    + fragment.decode("utf-8", "replace")
                    + " in "
                    + relative
                )
        # Rendered credentials must never be packaged. Durable templates retain
        # placeholders; generated bearer values are local install artifacts.
        if (
            relative.endswith(".luau")
            and b"studio_bearer_token" in data
            and b"__STUDIO_BEARER_TOKEN__" not in data
        ):
            raise ValueError(
                "portable plugin source appears to contain a rendered credential"
            )


def _manifest(items: Sequence[Tuple[str, bytes, int]]) -> Dict[str, object]:
    return {
        "format": installer.PACKAGE_FORMAT,
        "manifest_version": installer.PACKAGE_MANIFEST_VERSION,
        "product": installer.PRODUCT,
        "version": installer.VERSION,
        "platform": platform_support.TARGET_PLATFORM,
        "python_requires": ">=3.9",
        "source_date_epoch": 0,
        "files": [
            {
                "path": relative,
                "sha256": _sha256(data),
                "size": len(data),
                "mode": mode,
            }
            for relative, data, mode in items
        ],
    }


def _tar_bytes(
    items: Sequence[Tuple[str, bytes, int]], manifest: Dict[str, object]
) -> bytes:
    root = ARCHIVE_BASENAME
    all_items = [
        *items,
        (
            installer.PACKAGE_MANIFEST_FILENAME,
            _json_bytes(manifest),
            0o644,
        ),
    ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for relative, data, mode in sorted(all_items, key=lambda item: item[0]):
            info = tarfile.TarInfo(root + "/" + relative)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _gzip_bytes(value: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0
    ) as handle:
        handle.write(value)
    return output.getvalue()


def _atomic_write(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix="." + path.name + ".tmp-", dir=str(path.parent)
    )
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def build_release(project_root: Path, output_directory: Path) -> BuiltRelease:
    platform_support.require_supported_platform()
    platform_support.require_supported_runtime()
    root = Path(project_root).resolve(strict=True)
    output = Path(output_directory).resolve()
    items = _read_explicit_sources(root)
    _validate_portable_content(items)
    manifest = _manifest(items)
    archive_bytes = _gzip_bytes(_tar_bytes(items, manifest))
    archive = output / ARCHIVE_FILENAME
    digest = _sha256(archive_bytes)
    checksum_file = output / (ARCHIVE_FILENAME + ".sha256")
    bootstrap_source = root / "release_tools" / "bootstrap.py"
    if not bootstrap_source.is_file() or bootstrap_source.is_symlink():
        raise FileNotFoundError("required standalone bootstrap source is missing")
    bootstrap_bytes = bootstrap_source.read_bytes()
    _validate_portable_content(
        [(BOOTSTRAP_FILENAME, bootstrap_bytes, 0o755)]
    )
    bootstrap = output / BOOTSTRAP_FILENAME
    bootstrap_digest = _sha256(bootstrap_bytes)
    bootstrap_checksum_file = output / (BOOTSTRAP_FILENAME + ".sha256")
    checksum_manifest = output / CHECKSUM_MANIFEST_FILENAME
    _atomic_write(archive, archive_bytes, 0o600)
    _atomic_write(
        checksum_file,
        (digest + "  " + ARCHIVE_FILENAME + "\n").encode("ascii"),
        0o600,
    )
    _atomic_write(bootstrap, bootstrap_bytes, 0o700)
    _atomic_write(
        bootstrap_checksum_file,
        (bootstrap_digest + "  " + BOOTSTRAP_FILENAME + "\n").encode("ascii"),
        0o600,
    )
    _atomic_write(
        checksum_manifest,
        (
            digest
            + "  "
            + ARCHIVE_FILENAME
            + "\n"
            + bootstrap_digest
            + "  "
            + BOOTSTRAP_FILENAME
            + "\n"
        ).encode("ascii"),
        0o600,
    )
    return BuiltRelease(
        archive=archive,
        checksum_file=checksum_file,
        sha256=digest,
        manifest=manifest,
        bootstrap=bootstrap,
        bootstrap_checksum_file=bootstrap_checksum_file,
        bootstrap_sha256=bootstrap_digest,
        checksum_manifest=checksum_manifest,
    )
