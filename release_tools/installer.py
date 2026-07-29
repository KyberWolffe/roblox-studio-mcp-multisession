#!/usr/bin/env python3
"""Portable, idempotent installer for Roblox Studio MCP v2.

The release builder copies this file to the archive root as ``install.py``.
Only the explicitly owned v2 support root, plugin filename, and Codex table
are mutable.  The existing v1 installation is never inspected as an install
target and is never changed.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import difflib
import hashlib
import importlib.util
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from platform_support import (
    TARGET_PLATFORM,
    UnsupportedPlatformError,
    detect_platform,
    require_supported_platform,
    require_supported_runtime,
)

PRODUCT = "RobloxStudioMCPv2"
VERSION = "0.4.0-dev.1"
PACKAGE_FORMAT = "roblox-studio-mcp-v2-portable-release"
PACKAGE_MANIFEST_VERSION = 1
INSTALL_STATE_FORMAT = "roblox-studio-mcp-v2-install-state"
INSTALL_STATE_VERSION = 1
RUNTIME_SCHEMA_VERSION = 1
SECRETS_SCHEMA_VERSION = 1
SERVER_NAME = "Roblox_Studio_v2"
SERVER_HEADER = "[mcp_servers." + SERVER_NAME + "]"
PLUGIN_FILENAME = "StudioMCPv2SideBySide.rbxmx"
STABLE_LAUNCHER_NAME = "roblox-studio-mcp-v2"
STABLE_MANAGER_NAME = "roblox-studio-mcp-v2-manage"
ENTRYPOINT_MODULE = "studio_mcp_v2.lifecycle"
DEFAULT_PORT = 44756
DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
CATALOG_FILENAME = "tool-catalog.json"
PACKAGE_MANIFEST_FILENAME = "release-manifest.json"
INSTALL_STATE_FILENAME = "install-state.json"
RUNTIME_FILENAME = "runtime.json"
SECRETS_FILENAME = "secrets.json"
INSTALL_RUN_ID_KEY = "install_run_id"

_TABLE_HEADER = re.compile(
    rb"(?m)^[ \t]*\[[ \t]*mcp_servers\.Roblox_Studio_v2[ \t]*\]"
    rb"[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)"
)
_ANY_TOML_HEADER = re.compile(
    rb"(?m)^[ \t]*\[{1,2}[^\r\n\]]+\]{1,2}[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)"
)
_SAFE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?$")
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9]{16,64}$")
_SAFE_SECRET = re.compile(r"^[A-Za-z0-9_.:-]{32,256}$")


class InstallError(RuntimeError):
    """Fail-closed installation or ownership error."""


@dataclass(frozen=True)
class InstallLayout:
    home: Path
    support_root: Path
    codex_config: Path
    studio_plugins: Path

    @classmethod
    def for_user(
        cls,
        *,
        home: Optional[Path] = None,
        prefix: Optional[Path] = None,
    ) -> "InstallLayout":
        user_home = (
            Path(pwd.getpwuid(os.getuid()).pw_dir)
            if home is None
            else Path(home).expanduser()
        )
        if not user_home.is_absolute():
            raise InstallError("home must be an absolute path")
        user_home = user_home.resolve()
        support = (
            user_home / "Library" / "Application Support" / PRODUCT
            if prefix is None
            else Path(prefix).expanduser()
        )
        if not support.is_absolute():
            raise InstallError("--prefix must be an absolute path")
        support = support.resolve()
        forbidden_broad_targets = {
            Path("/"),
            user_home,
            user_home / "Library",
            user_home / "Library" / "Application Support",
            user_home / "Documents",
            user_home / "Documents" / "Roblox",
            user_home / "Documents" / "Roblox" / "Plugins",
            user_home / ".codex",
        }
        if support in forbidden_broad_targets or len(support.parts) < 3:
            raise InstallError("support root is too broad")
        return cls(
            home=user_home,
            support_root=support,
            codex_config=user_home / ".codex" / "config.toml",
            studio_plugins=user_home / "Documents" / "Roblox" / "Plugins",
        )

    @property
    def releases(self) -> Path:
        return self.support_root / "releases"

    @property
    def release(self) -> Path:
        return self.releases / VERSION

    @property
    def packages(self) -> Path:
        return self.support_root / "packages"

    @property
    def package(self) -> Path:
        return self.packages / VERSION

    @property
    def config(self) -> Path:
        return self.support_root / "config"

    @property
    def run(self) -> Path:
        return self.support_root / "run"

    @property
    def logs(self) -> Path:
        return self.support_root / "logs"

    @property
    def state(self) -> Path:
        return self.support_root / "state"

    @property
    def backups(self) -> Path:
        return self.support_root / "backups"

    @property
    def artifacts(self) -> Path:
        return self.support_root / "artifacts"

    @property
    def bin(self) -> Path:
        return self.support_root / "bin"

    @property
    def runtime_config(self) -> Path:
        return self.config / RUNTIME_FILENAME

    @property
    def secrets_config(self) -> Path:
        return self.config / SECRETS_FILENAME

    @property
    def effective_catalog(self) -> Path:
        return self.config / CATALOG_FILENAME

    @property
    def catalog_artifact(self) -> Path:
        return self.artifacts / CATALOG_FILENAME

    @property
    def upstream_catalog(self) -> Path:
        return self.config / "upstream-known-tool-catalog.json"

    @property
    def compatibility_manifest(self) -> Path:
        return self.config / "upstream-compatibility-map.json"

    @property
    def trusted_v1_cache(self) -> Path:
        return (
            self.home
            / "Library"
            / "Application Support"
            / "StudioMCP"
            / "tools-cache.json"
        )

    @property
    def plugin_artifact(self) -> Path:
        return self.artifacts / PLUGIN_FILENAME

    @property
    def plugin_target(self) -> Path:
        return self.studio_plugins / PLUGIN_FILENAME

    @property
    def install_state(self) -> Path:
        return self.state / INSTALL_STATE_FILENAME

    @property
    def launcher(self) -> Path:
        return self.bin / STABLE_LAUNCHER_NAME

    @property
    def launcher_bootstrap(self) -> Path:
        return self.bin / (STABLE_LAUNCHER_NAME + "-bootstrap.py")

    @property
    def manager(self) -> Path:
        return self.bin / STABLE_MANAGER_NAME


def _utc_stamp() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)


def _ensure_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise InstallError("expected a non-symlink directory: " + str(path))
    else:
        _ensure_parent_directory(path.parent)
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    os.chmod(path, 0o700)
    _fsync_directory(path)


def _ensure_parent_directory(path: Path) -> None:
    """Create missing parents privately without chmoding shared existing dirs."""

    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise InstallError("parent path is not a non-symlink directory: " + str(current))
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _fsync_directory(directory.parent)
    # Existing shared parents deliberately retain their original modes.


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise InstallError(
            "unable to open directory for durability sync: " + str(path)
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise InstallError(
                "durability sync target is not a directory: " + str(path)
            )
        os.fsync(descriptor)
    except OSError as exc:
        raise InstallError(
            "unable to durability-sync directory: " + str(path)
        ) from exc
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    _ensure_parent_directory(path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix="." + path.name + ".tmp-", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _copy_atomic(source: Path, target: Path, mode: int) -> None:
    _atomic_write(target, source.read_bytes(), mode)


def _backup_file(path: Path, backup_dir: Path, label: str) -> Optional[Path]:
    if not path.exists() and not path.is_symlink():
        return None
    if not _regular_file(path):
        raise InstallError("refusing to back up a non-regular file: " + str(path))
    _ensure_private_directory(backup_dir)
    digest = _sha256_file(path)
    target = backup_dir / (
        label + "." + _utc_stamp() + "." + digest[:16] + ".bak"
    )
    _copy_atomic(path, target, 0o600)
    return target


def _move_aside(path: Path, backup_dir: Path, label: str) -> Path:
    if path.is_symlink():
        raise InstallError("refusing to move an owned symlink: " + str(path))
    _ensure_private_directory(backup_dir)
    target = backup_dir / (label + "." + _utc_stamp() + "." + uuid.uuid4().hex)
    os.replace(path, target)
    _fsync_directory(path.parent)
    _fsync_directory(backup_dir)
    return target


def _load_json(path: Path, label: str) -> Dict[str, Any]:
    if not _regular_file(path):
        raise InstallError(label + " must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError(label + " is invalid JSON: " + str(exc))
    if not isinstance(value, dict):
        raise InstallError(label + " must contain a JSON object")
    return value


def _validate_relative_file(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise InstallError("release manifest contains an invalid file path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise InstallError("release manifest file path escapes the package")
    return value


def verify_release_package(package_root: Path) -> Dict[str, Any]:
    root = Path(package_root).resolve(strict=True)
    manifest_path = root / PACKAGE_MANIFEST_FILENAME
    manifest = _load_json(manifest_path, "release manifest")
    if (
        manifest.get("format") != PACKAGE_FORMAT
        or manifest.get("manifest_version") != PACKAGE_MANIFEST_VERSION
        or manifest.get("version") != VERSION
        or manifest.get("platform") != TARGET_PLATFORM
    ):
        raise InstallError("release manifest identity/version is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise InstallError("release manifest files must be a nonempty array")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size",
            "mode",
        }:
            raise InstallError("release manifest file entry is invalid")
        relative = _validate_relative_file(item["path"])
        if relative in seen:
            raise InstallError("duplicate release manifest path: " + relative)
        seen.add(relative)
        digest = item["sha256"]
        size = item["size"]
        mode = item["mode"]
        if (
            not isinstance(digest, str)
            or _SAFE_SHA256.fullmatch(digest) is None
            or not isinstance(size, int)
            or size < 0
            or mode not in (0o600, 0o644, 0o700, 0o755)
        ):
            raise InstallError("release manifest metadata is invalid")
        source = root / relative
        if not _regular_file(source):
            raise InstallError("release package file is missing: " + relative)
        data = source.read_bytes()
        if len(data) != size or not secrets.compare_digest(
            _sha256_bytes(data), digest
        ):
            raise InstallError("release package hash mismatch: " + relative)
    required = {
        "install.py",
        "platform_support.py",
        "release_updater.py",
        "bootstrap.py",
        "launcher-template.py",
        "INSTALL.md",
        "payload/studio_mcp_v2/__init__.py",
        "payload/config/durable-tool-catalog.json",
        "payload/scripts/render_studio_plugin.py",
        "payload/scripts/studio_plugin_template.luau",
        "payload/scripts/play_server_bridge.luau",
    }
    missing = sorted(required - seen)
    if missing:
        raise InstallError(
            "release package is missing required files: " + ", ".join(missing)
        )
    return manifest


def _manifest_map(manifest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {str(item["path"]): item for item in manifest["files"]}


def _tree_matches(
    root: Path,
    items: Iterable[Tuple[str, Mapping[str, Any]]],
) -> bool:
    for relative, metadata in items:
        target = root / relative
        if not _regular_file(target):
            return False
        if target.stat().st_size != metadata["size"]:
            return False
        if not secrets.compare_digest(_sha256_file(target), metadata["sha256"]):
            return False
    return True


def _install_tree(
    *,
    package_root: Path,
    target: Path,
    files: Sequence[Tuple[str, str, Mapping[str, Any]]],
    backup_root: Path,
    label: str,
) -> bool:
    expected = [(destination, metadata) for _, destination, metadata in files]
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise InstallError("owned tree target is not a directory: " + str(target))
        if _tree_matches(target, expected):
            return False
        _move_aside(target, backup_root, label)

    _ensure_private_directory(target.parent)
    temp_target = target.parent / ("." + target.name + ".tmp-" + uuid.uuid4().hex)
    temp_target.mkdir(mode=0o700)
    _fsync_directory(target.parent)
    try:
        for source_relative, destination_relative, metadata in files:
            source = package_root / source_relative
            destination = temp_target / destination_relative
            _ensure_private_directory(destination.parent)
            _copy_atomic(source, destination, int(metadata["mode"]))
        if not _tree_matches(temp_target, expected):
            raise InstallError("installed tree failed post-copy verification")
        os.replace(temp_target, target)
        _fsync_directory(target.parent)
    except Exception:
        if temp_target.exists() and not temp_target.is_symlink():
            shutil.rmtree(temp_target)
            _fsync_directory(temp_target.parent)
        raise
    return True


def _expected_codex_block(layout: InstallLayout) -> bytes:
    command = json.dumps(str(layout.launcher), ensure_ascii=False)
    return (
        SERVER_HEADER
        + "\n"
        + "command = "
        + command
        + "\n"
        + "args = []\n"
        + "enabled = true\n"
        + "required = false\n"
        + 'default_tools_approval_mode = "writes"\n'
        + "startup_timeout_sec = 20\n"
        + "tool_timeout_sec = 180\n"
    ).encode("utf-8")


def _find_codex_table(data: bytes) -> Optional[Tuple[int, int, bytes]]:
    matches = list(_TABLE_HEADER.finditer(data))
    if len(matches) > 1:
        raise InstallError("Codex config contains duplicate Roblox_Studio_v2 tables")
    if not matches:
        return None
    match = matches[0]
    next_header = _ANY_TOML_HEADER.search(data, match.end())
    end = len(data) if next_header is None else next_header.start()
    return match.start(), end, data[match.start() : end]


def _owned_codex_separator(
    layout: InstallLayout,
    state: Mapping[str, Any],
    data: bytes,
    table_start: int,
) -> bytes:
    """Recover only whitespace that v2 itself inserted before its table."""

    state_codex = state.get("codex")
    if not isinstance(state_codex, Mapping):
        raise InstallError("install state lacks Codex ownership metadata")
    encoded = state_codex.get("inserted_separator_hex")
    allowed = {b"", b"\n", b"\n\n"}
    if isinstance(encoded, str):
        try:
            separator = bytes.fromhex(encoded)
        except ValueError as exc:
            raise InstallError(
                "install state has invalid Codex separator ownership"
            ) from exc
        if separator not in allowed:
            raise InstallError(
                "install state has invalid Codex separator ownership"
            )
        if separator and (
            table_start < len(separator)
            or data[table_start - len(separator) : table_start] != separator
        ):
            raise InstallError(
                "Codex config changed at the v2 table ownership boundary"
            )
        return separator

    # One-time migration for 0.2.0 installs created before separator ownership
    # was recorded.  Adopt bytes only when the exact pre-install backup proves
    # they were appended by v2; otherwise conservatively own no whitespace.
    backup_value = state_codex.get("last_backup")
    if not isinstance(backup_value, str):
        return b""
    backup = Path(backup_value)
    try:
        resolved_backup = backup.resolve(strict=True)
        resolved_directory = (layout.backups / "codex").resolve(strict=True)
        resolved_backup.relative_to(resolved_directory)
    except (FileNotFoundError, OSError, ValueError):
        return b""
    if not _regular_file(resolved_backup):
        return b""
    prefix = data[:table_start]
    original = resolved_backup.read_bytes()
    matches = [
        separator
        for separator in allowed
        if prefix == original + separator
    ]
    return matches[0] if len(matches) == 1 else b""


def _write_codex_config(
    layout: InstallLayout,
    expected_block: bytes,
    state: Optional[Mapping[str, Any]],
    *,
    replace_owned_config: bool,
) -> Tuple[str, Optional[str], bool, str]:
    config_path = layout.codex_config
    data = config_path.read_bytes() if _regular_file(config_path) else b""
    if config_path.exists() and not _regular_file(config_path):
        raise InstallError("Codex config is not a regular file")
    existing = _find_codex_table(data)
    expected_hash = _sha256_bytes(expected_block)
    state_codex = state.get("codex") if isinstance(state, Mapping) else None
    owned_hash = (
        state_codex.get("block_sha256")
        if isinstance(state_codex, Mapping)
        else None
    )

    separator = b""
    if existing is None:
        separator = b"" if not data else (b"\n" if data.endswith(b"\n") else b"\n\n")
        if data and data.endswith(b"\n") and not data.endswith(b"\n\n"):
            separator = b"\n"
        new_data = data + separator + expected_block
    else:
        start, end, block = existing
        current_hash = _sha256_bytes(block)
        if state is None:
            raise InstallError(
                "Codex config already contains an unowned Roblox_Studio_v2 table"
            )
        separator = _owned_codex_separator(layout, state, data, start)
        if secrets.compare_digest(current_hash, expected_hash):
            return expected_hash, None, False, separator.hex()
        if not replace_owned_config:
            raise InstallError(
                "owned Roblox_Studio_v2 table drifted; review it, then rerun "
                "with --replace-owned-config to replace only that table"
            )
        if not isinstance(owned_hash, str) or _SAFE_SHA256.fullmatch(owned_hash) is None:
            raise InstallError("install state lacks a valid Codex ownership hash")
        new_data = data[:start] + expected_block + data[end:]

    backup = _backup_file(
        config_path,
        layout.backups / "codex",
        "config.toml",
    )
    existing_mode = 0o600
    if _regular_file(config_path):
        existing_mode = stat.S_IMODE(config_path.stat().st_mode)
    _atomic_write(config_path, new_data, existing_mode)
    return (
        expected_hash,
        None if backup is None else str(backup),
        True,
        separator.hex(),
    )


def _preflight_codex(
    layout: InstallLayout,
    state: Optional[Mapping[str, Any]],
    expected_block: bytes,
    *,
    replace_owned_config: bool,
) -> None:
    path = layout.codex_config
    if not path.exists():
        return
    if not _regular_file(path):
        raise InstallError("Codex config is not a regular file")
    existing = _find_codex_table(path.read_bytes())
    if existing is None:
        return
    if state is None:
        raise InstallError(
            "Codex config already contains an unowned Roblox_Studio_v2 table"
        )
    current_hash = _sha256_bytes(existing[2])
    if secrets.compare_digest(current_hash, _sha256_bytes(expected_block)):
        return
    if not replace_owned_config:
        raise InstallError(
            "owned Roblox_Studio_v2 table drifted; use doctor before explicitly "
            "allowing its replacement"
        )


def _validate_secrets(value: Mapping[str, Any]) -> Dict[str, str]:
    if set(value) != {"schema_version", "client_token", "studio_token"}:
        raise InstallError("secrets.json fields are invalid")
    if value.get("schema_version") != SECRETS_SCHEMA_VERSION:
        raise InstallError("secrets.json schema version is invalid")
    client = value.get("client_token")
    studio = value.get("studio_token")
    if (
        not isinstance(client, str)
        or _SAFE_SECRET.fullmatch(client) is None
        or not isinstance(studio, str)
        or _SAFE_SECRET.fullmatch(studio) is None
        or secrets.compare_digest(client, studio)
    ):
        raise InstallError("secrets.json credentials are invalid")
    return {"client_token": client, "studio_token": studio}


def _read_or_create_secrets(
    layout: InstallLayout,
    *,
    rotate: bool,
) -> Tuple[Dict[str, str], bool]:
    path = layout.secrets_config
    if path.exists() or path.is_symlink():
        if not _regular_file(path):
            raise InstallError("secrets.json is not a regular file")
        if stat.S_IMODE(path.stat().st_mode) != 0o600 and not rotate:
            raise InstallError("secrets.json must have mode 0600")
        if not rotate:
            return _validate_secrets(_load_json(path, "secrets.json")), False
        _backup_file(path, layout.backups / "secrets", "secrets.json")
    client = secrets.token_urlsafe(48)
    studio = secrets.token_urlsafe(48)
    while secrets.compare_digest(client, studio):
        studio = secrets.token_urlsafe(48)
    value: Dict[str, Any] = {
        "schema_version": SECRETS_SCHEMA_VERSION,
        "client_token": client,
        "studio_token": studio,
    }
    _atomic_write(path, _json_bytes(value), 0o600)
    return {"client_token": client, "studio_token": studio}, True


def _runtime_value(layout: InstallLayout) -> Dict[str, Any]:
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "host": "127.0.0.1",
        "port": DEFAULT_PORT,
        "catalog": str(layout.effective_catalog),
        "allowed_studios": ["*"],
        "allowed_tools": ["*"],
        "startup_timeout_seconds": DEFAULT_STARTUP_TIMEOUT_SECONDS,
    }


def _write_owned_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    state_hash: Optional[str],
    backup_dir: Path,
    label: str,
    first_install_collision_error: str,
) -> Tuple[str, bool]:
    expected = _json_bytes(value)
    expected_hash = _sha256_bytes(expected)
    if path.exists() or path.is_symlink():
        if not _regular_file(path):
            raise InstallError(label + " is not a regular file")
        current_hash = _sha256_file(path)
        if secrets.compare_digest(current_hash, expected_hash):
            return expected_hash, False
        if state_hash is None:
            raise InstallError(first_install_collision_error)
        _backup_file(path, backup_dir, label)
    _atomic_write(path, expected, 0o600)
    return expected_hash, True


def _load_renderer(release_root: Path) -> Any:
    path = release_root / "scripts" / "render_studio_plugin.py"
    if not _regular_file(path):
        raise InstallError("installed durable plugin renderer is missing")
    module_name = "_studio_mcp_v2_durable_renderer_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise InstallError("unable to load durable plugin renderer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_release_submodule(release_root: Path, submodule: str) -> Any:
    """Load one installed package submodule without trusting cwd/sys.path."""

    package_directory = release_root / "studio_mcp_v2"
    package_init = package_directory / "__init__.py"
    module_path = package_directory / (submodule + ".py")
    if not _regular_file(package_init) or not _regular_file(module_path):
        raise InstallError("installed release module is missing: " + submodule)
    package_name = "_installed_studio_mcp_v2_" + uuid.uuid4().hex
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        package_init,
        submodule_search_locations=[str(package_directory)],
    )
    if package_spec is None or package_spec.loader is None:
        raise InstallError("unable to load installed release package")
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    try:
        package_spec.loader.exec_module(package)
        qualified = package_name + "." + submodule
        spec = importlib.util.spec_from_file_location(qualified, module_path)
        if spec is None or spec.loader is None:
            raise InstallError("unable to load installed module: " + submodule)
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        for key in list(sys.modules):
            if key == package_name or key.startswith(package_name + "."):
                sys.modules.pop(key, None)
        raise


def _validate_durable_contract(
    release_root: Path,
    catalog_path: Path,
    compatibility_manifest_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any], bytes]:
    review = _load_release_submodule(release_root, "catalog_review")
    try:
        payload, raw = review.load_catalog(catalog_path)
        manifest = review.load_compatibility_manifest(
            compatibility_manifest_path
        )
        contract = review.validate_durable_contract(
            payload,
            compatibility_manifest=manifest,
            handler_source_path=(
                release_root / "scripts" / "durable_operation_handlers.luau"
            ),
        )
    except Exception as exc:
        raise InstallError("durable catalog contract validation failed: " + str(exc))
    details = _catalog_details(payload)
    if not details["compatible"]:
        raise InstallError(
            "durable catalog provenance is incompatible: "
            + "; ".join(details["reasons"])
        )
    details["contract"] = contract
    return payload, details, raw


def _render_plugin(
    release_root: Path,
    *,
    studio_token: str,
    install_run_id: str,
) -> bytes:
    renderer = _load_renderer(release_root)
    render_durable = getattr(renderer, "render_durable", None)
    package_rbxmx = getattr(renderer, "package_rbxmx", None)
    if not callable(render_durable) or not callable(package_rbxmx):
        raise InstallError("release does not expose the durable plugin renderer API")
    source = render_durable(
        studio_token=studio_token,
        run_id=install_run_id,
        base_url="http://127.0.0.1:" + str(DEFAULT_PORT),
    )
    if not isinstance(source, str) or not source:
        raise InstallError("durable plugin renderer returned invalid source")
    package = package_rbxmx(source, package_name="StudioMCPv2SideBySide")
    if not isinstance(package, str) or not package:
        raise InstallError("durable plugin packager returned invalid XML")
    return package.encode("utf-8")


def _catalog_details(value: Mapping[str, Any]) -> Dict[str, Any]:
    tools = value.get("tools")
    names: List[str] = []
    structurally_valid = isinstance(tools, list)
    if isinstance(tools, list):
        seen = set()
        for item in tools:
            if not isinstance(item, dict):
                structurally_valid = False
                continue
            name = item.get("name")
            schema = item.get("inputSchema")
            if (
                not isinstance(name, str)
                or not name
                or name in seen
                or not isinstance(schema, dict)
            ):
                structurally_valid = False
                continue
            seen.add(name)
            names.append(name)
    upstream = value.get("upstream")
    upstream_version = None
    upstream_sha = None
    upstream_compatibility = None
    if isinstance(upstream, dict):
        upstream_version = upstream.get("version")
        upstream_sha = upstream.get("source_sha256")
        upstream_compatibility = upstream.get("compatibility")
    compatible = (
        structurally_valid
        and value.get("format") == "studio-mcp-v2-durable-catalog"
        and isinstance(value.get("catalog_version"), str)
        and bool(value.get("catalog_version"))
        and isinstance(upstream_version, str)
        and bool(upstream_version)
        and isinstance(upstream_sha, str)
        and _SAFE_SHA256.fullmatch(upstream_sha) is not None
        and upstream_compatibility
        in {
            "reviewed-local-subset-only",
            "reviewed-exact-handler-mapping",
        }
    )
    reasons = []
    if not structurally_valid:
        reasons.append("tools are structurally invalid or duplicated")
    if value.get("format") != "studio-mcp-v2-durable-catalog":
        reasons.append("format is not the durable catalog format")
    if not isinstance(value.get("catalog_version"), str):
        reasons.append("catalog_version is missing")
    if not isinstance(upstream_version, str):
        reasons.append("upstream version is missing")
    if (
        not isinstance(upstream_sha, str)
        or _SAFE_SHA256.fullmatch(upstream_sha) is None
    ):
        reasons.append("upstream source_sha256 is invalid")
    if upstream_compatibility not in {
        "reviewed-local-subset-only",
        "reviewed-exact-handler-mapping",
    }:
        reasons.append("upstream compatibility is not a reviewed policy")
    return {
        "format": value.get("format"),
        "catalog_version": value.get("catalog_version"),
        "upstream_version": upstream_version,
        "upstream_source_sha256": upstream_sha,
        "upstream_compatibility": upstream_compatibility,
        "tool_count": len(names),
        "tool_names": sorted(names),
        "compatible": compatible,
        "reasons": reasons,
    }


def _read_catalog(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any], bytes]:
    if not _regular_file(path):
        raise InstallError("catalog is not a regular non-symlink file: " + str(path))
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError("catalog is invalid JSON: " + str(exc))
    if not isinstance(value, dict):
        raise InstallError("catalog must contain a JSON object")
    details = _catalog_details(value)
    return value, details, raw


def _launcher_bootstrap_source(
    package_root: Path,
    layout: InstallLayout,
    python_executable: str,
) -> bytes:
    template = (package_root / "launcher-template.py").read_text(encoding="utf-8")
    replacements = {
        "__SUPPORT_ROOT_LITERAL__": repr(str(layout.support_root)),
        "__RELEASE_ROOT_LITERAL__": repr(str(layout.release)),
        "__PYTHON_EXECUTABLE_LITERAL__": repr(python_executable),
        "__ENTRYPOINT_MODULE_LITERAL__": repr(ENTRYPOINT_MODULE),
    }
    for placeholder, replacement in replacements.items():
        if template.count(placeholder) != 1:
            raise InstallError("launcher template placeholder is invalid: " + placeholder)
        template = template.replace(placeholder, replacement)
    if re.search(r"__[A-Z0-9_]+__", template):
        raise InstallError("launcher template contains unresolved placeholders")
    return template.encode("utf-8")


def _shell_exec(python_executable: str, script: Path, extra: Sequence[str] = ()) -> bytes:
    pieces = [
        "exec",
        shlex.quote(python_executable),
        "-B",
        shlex.quote(str(script)),
        *(shlex.quote(item) for item in extra),
        '"$@"',
    ]
    return ("#!/bin/sh\n" + " ".join(pieces) + "\n").encode("utf-8")


def _load_release_updater_module() -> Any:
    """Load the source-tree or portable-archive updater implementation."""

    if __package__:
        from . import updater

        return updater
    try:
        import release_updater
    except ImportError as exc:
        raise InstallError("verified release updater component is missing") from exc
    return release_updater


class Installer:
    def __init__(
        self,
        package_root: Path,
        layout: InstallLayout,
        *,
        python_executable: Optional[str] = None,
    ):
        self.package_root = Path(package_root).resolve(strict=True)
        self.layout = layout
        self.python_executable = str(
            Path(sys.executable if python_executable is None else python_executable)
        )
        self.manifest = verify_release_package(self.package_root)
        self.manifest_files = _manifest_map(self.manifest)

    def _invoke_lifecycle(
        self,
        command: str,
        *,
        require_ok: bool = True,
        timeout: int = 20,
        allow_ephemeral_repair_launcher: bool = False,
    ) -> Dict[str, Any]:
        if command not in {"start", "status", "doctor", "stop"}:
            raise InstallError("unsupported lifecycle management command")
        argv: List[str]
        cleanup_path: Optional[Path] = None
        stable_trusted = False
        if _regular_file(self.layout.launcher):
            try:
                state = self._load_state(optional=False)
                launchers = state.get("launchers")
                expected = (
                    launchers.get(self.layout.launcher.name)
                    if isinstance(launchers, Mapping)
                    else None
                )
                stable_trusted = isinstance(expected, str) and secrets.compare_digest(
                    _sha256_file(self.layout.launcher), expected
                )
            except InstallError:
                stable_trusted = False
        if stable_trusted:
            argv = [str(self.layout.launcher), command, "--json"]
        elif allow_ephemeral_repair_launcher:
            release_items = [
                (relative[len("payload/") :], metadata)
                for relative, metadata in self.manifest_files.items()
                if relative.startswith("payload/")
            ]
            if not self.layout.release.is_dir() or not _tree_matches(
                self.layout.release, release_items
            ):
                raise InstallError(
                    "stable launcher is damaged and the pinned release cannot "
                    "be verified; reinstall from the portable archive"
                )
            _validate_secrets(
                _load_json(self.layout.secrets_config, "secrets.json")
            )
            if stat.S_IMODE(self.layout.secrets_config.stat().st_mode) != 0o600:
                raise InstallError(
                    "stable launcher is damaged and secrets are not private"
                )
            if _load_json(self.layout.runtime_config, "runtime.json") != _runtime_value(
                self.layout
            ):
                raise InstallError(
                    "stable launcher is damaged and runtime config drifted"
                )
            _ensure_private_directory(self.layout.run)
            cleanup_path = self.layout.run / (
                ".repair-launcher-" + uuid.uuid4().hex + ".py"
            )
            _atomic_write(
                cleanup_path,
                _launcher_bootstrap_source(
                    self.package_root, self.layout, self.python_executable
                ),
                0o700,
            )
            argv = [
                self.python_executable,
                "-I",
                "-B",
                str(cleanup_path),
                command,
                "--json",
            ]
        else:
            raise InstallError(
                "stable v2 lifecycle launcher is missing or drifted"
            )
        try:
            process = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError(
                "unable to run the v2 lifecycle " + command + ": " + str(exc)
            )
        finally:
            if cleanup_path is not None:
                try:
                    cleanup_path.unlink()
                    _fsync_directory(cleanup_path.parent)
                except OSError:
                    pass
        if len(process.stdout) > 1_000_000 or len(process.stderr) > 1_000_000:
            raise InstallError("v2 lifecycle output exceeded the safety bound")
        try:
            payload = json.loads(process.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InstallError(
                "v2 lifecycle " + command + " returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise InstallError("v2 lifecycle response must be a JSON object")
        if process.returncode != 0 or (require_ok and payload.get("ok") is not True):
            message = payload.get("error")
            raise InstallError(
                "v2 lifecycle "
                + command
                + " was refused safely: "
                + json.dumps(message, sort_keys=True)
            )
        return payload

    def _safe_stop_lifecycle(self) -> Dict[str, Any]:
        payload = self._invoke_lifecycle(
            "stop", allow_ephemeral_repair_launcher=True
        )
        if payload.get("running") is not False or payload.get("stopped") not in {
            True,
            False,
        }:
            raise InstallError("v2 lifecycle stop acknowledgement is invalid")
        return payload

    def _load_state(self, *, optional: bool = True) -> Optional[Dict[str, Any]]:
        path = self.layout.install_state
        if not path.exists() and not path.is_symlink():
            if optional:
                return None
            raise InstallError("v2 install state does not exist")
        value = _load_json(path, "install state")
        if (
            value.get("format") != INSTALL_STATE_FORMAT
            or value.get("schema_version") != INSTALL_STATE_VERSION
            or value.get("product") != PRODUCT
        ):
            raise InstallError("install state identity/schema is invalid")
        if Path(str(value.get("support_root", ""))) != self.layout.support_root:
            raise InstallError("install state belongs to a different support root")
        return value

    def _preflight_first_install(self, state: Optional[Mapping[str, Any]]) -> None:
        if state is not None:
            return
        root = self.layout.support_root
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir():
                raise InstallError("support root is not a directory")
            if any(root.iterdir()):
                raise InstallError(
                    "support root is nonempty but has no valid v2 ownership state"
                )
        if self.layout.plugin_target.exists() or self.layout.plugin_target.is_symlink():
            raise InstallError(
                "Studio plugin target already exists without v2 ownership state"
            )

    def _prepare_directories(self) -> None:
        for path in (
            self.layout.support_root,
            self.layout.releases,
            self.layout.packages,
            self.layout.config,
            self.layout.run,
            self.layout.logs,
            self.layout.state,
            self.layout.backups,
            self.layout.artifacts,
            self.layout.bin,
        ):
            _ensure_private_directory(path)

    def _lifecycle_sensitive_change_needed(
        self,
        state: Mapping[str, Any],
        *,
        rotate_secrets: bool,
    ) -> bool:
        """Conservatively detect whether a healthy broker can remain untouched."""

        if rotate_secrets or state.get("version") != VERSION:
            return True
        release_items = [
            (relative[len("payload/") :], metadata)
            for relative, metadata in self.manifest_files.items()
            if relative.startswith("payload/")
        ]
        if not self.layout.release.is_dir() or not _tree_matches(
            self.layout.release, release_items
        ):
            return True
        try:
            secrets_value = _validate_secrets(
                _load_json(self.layout.secrets_config, "secrets.json")
            )
            if (
                not secrets_value
                or stat.S_IMODE(self.layout.secrets_config.stat().st_mode)
                != 0o600
            ):
                return True
        except InstallError:
            return True
        if (
            not _regular_file(self.layout.runtime_config)
            or self.layout.runtime_config.read_bytes()
            != _json_bytes(_runtime_value(self.layout))
        ):
            return True

        catalog_state = state.get("catalog")
        if not isinstance(catalog_state, Mapping):
            return True
        catalog_checks = (
            (self.layout.effective_catalog, catalog_state.get("sha256")),
            (
                self.layout.catalog_artifact,
                catalog_state.get("artifact_sha256"),
            ),
            (
                self.layout.upstream_catalog,
                catalog_state.get("upstream_snapshot_sha256"),
            ),
            (
                self.layout.artifacts / "upstream-known-tool-catalog.json",
                catalog_state.get("upstream_snapshot_sha256"),
            ),
        )
        for path, digest in catalog_checks:
            if (
                not _regular_file(path)
                or not isinstance(digest, str)
                or not secrets.compare_digest(_sha256_file(path), digest)
            ):
                return True
        compatibility_source = (
            self.package_root
            / "payload"
            / "config"
            / "upstream-compatibility-map.json"
        )
        if (
            not _regular_file(self.layout.compatibility_manifest)
            or not secrets.compare_digest(
                _sha256_file(self.layout.compatibility_manifest),
                _sha256_file(compatibility_source),
            )
        ):
            return True
        try:
            _validate_durable_contract(
                self.layout.release,
                self.layout.effective_catalog,
                self.layout.compatibility_manifest,
            )
        except InstallError:
            return True

        plugin_state = state.get("plugin")
        if not isinstance(plugin_state, Mapping):
            return True
        for path, key in (
            (self.layout.plugin_target, "sha256"),
            (self.layout.plugin_artifact, "artifact_sha256"),
        ):
            digest = plugin_state.get(key)
            if (
                not _regular_file(path)
                or not isinstance(digest, str)
                or not secrets.compare_digest(_sha256_file(path), digest)
            ):
                return True

        try:
            expected_launchers = {
                self.layout.launcher_bootstrap: _launcher_bootstrap_source(
                    self.package_root, self.layout, self.python_executable
                ),
                self.layout.launcher: _shell_exec(
                    self.python_executable, self.layout.launcher_bootstrap
                ),
                self.layout.manager: _shell_exec(
                    self.python_executable,
                    self.layout.package / "install.py",
                    ("--prefix", str(self.layout.support_root)),
                ),
            }
        except (OSError, InstallError):
            return True
        for path, expected in expected_launchers.items():
            if not _regular_file(path) or not secrets.compare_digest(
                _sha256_file(path), _sha256_bytes(expected)
            ):
                return True
        return False

    def _install_package_and_release(self) -> Dict[str, bool]:
        package_files: List[Tuple[str, str, Mapping[str, Any]]] = []
        release_files: List[Tuple[str, str, Mapping[str, Any]]] = []
        for relative, metadata in self.manifest_files.items():
            package_files.append((relative, relative, metadata))
            if relative.startswith("payload/"):
                release_files.append((relative, relative[len("payload/") :], metadata))
        package_manifest_source = self.package_root / PACKAGE_MANIFEST_FILENAME
        package_manifest_metadata = {
            "sha256": _sha256_file(package_manifest_source),
            "size": package_manifest_source.stat().st_size,
            "mode": 0o644,
        }
        package_files.append(
            (
                PACKAGE_MANIFEST_FILENAME,
                PACKAGE_MANIFEST_FILENAME,
                package_manifest_metadata,
            )
        )
        return {
            "package": _install_tree(
                package_root=self.package_root,
                target=self.layout.package,
                files=package_files,
                backup_root=self.layout.backups / "packages",
                label="package-" + VERSION,
            ),
            "release": _install_tree(
                package_root=self.package_root,
                target=self.layout.release,
                files=release_files,
                backup_root=self.layout.backups / "releases",
                label="release-" + VERSION,
            ),
        }

    def _seed_or_repair_catalog(
        self, state: Optional[Mapping[str, Any]]
    ) -> Tuple[str, Dict[str, Any], Dict[str, str], bool]:
        durable_source = (
            self.layout.release / "config" / "durable-tool-catalog.json"
        )
        upstream_source = self.layout.release / "config" / "tool-catalog.json"
        compatibility_source = (
            self.layout.release / "config" / "upstream-compatibility-map.json"
        )
        _validate_durable_contract(
            self.layout.release,
            durable_source,
            compatibility_source,
        )
        state_catalog = state.get("catalog") if isinstance(state, Mapping) else None
        owned_hash = (
            state_catalog.get("sha256")
            if isinstance(state_catalog, Mapping)
            else None
        )
        upstream_owned_hash = (
            state_catalog.get("upstream_snapshot_sha256")
            if isinstance(state_catalog, Mapping)
            else None
        )

        changed = False

        def restore_owned_pair(
            source: Path,
            target: Path,
            artifact: Path,
            prior_hash: Optional[str],
            label: str,
        ) -> str:
            nonlocal changed
            source_raw = source.read_bytes()
            source_hash = _sha256_bytes(source_raw)
            if state is None:
                for existing in (target, artifact):
                    if existing.exists() or existing.is_symlink():
                        if not _regular_file(existing):
                            raise InstallError(label + " collision is not a regular file")
                        if not secrets.compare_digest(
                            _sha256_file(existing), source_hash
                        ):
                            raise InstallError(
                                "unowned " + label + " already exists"
                            )
                desired_raw = source_raw
                desired_hash = source_hash
            elif (
                isinstance(prior_hash, str)
                and _SAFE_SHA256.fullmatch(prior_hash) is not None
                and _regular_file(target)
                and secrets.compare_digest(_sha256_file(target), prior_hash)
            ):
                desired_raw = target.read_bytes()
                desired_hash = prior_hash
            elif (
                isinstance(prior_hash, str)
                and _SAFE_SHA256.fullmatch(prior_hash) is not None
                and _regular_file(artifact)
                and secrets.compare_digest(_sha256_file(artifact), prior_hash)
            ):
                desired_raw = artifact.read_bytes()
                desired_hash = prior_hash
            else:
                # Both owned copies drifted or disappeared. Restore the
                # package's validated default, never accept superficial JSON.
                desired_raw = source_raw
                desired_hash = source_hash

            for destination in (artifact, target):
                if destination.exists() or destination.is_symlink():
                    if not _regular_file(destination):
                        raise InstallError(label + " is not a regular file")
                    if secrets.compare_digest(
                        _sha256_file(destination), desired_hash
                    ):
                        continue
                    _backup_file(
                        destination,
                        self.layout.backups / "catalog",
                        destination.name,
                    )
                _atomic_write(destination, desired_raw, 0o600)
                changed = True
            return desired_hash

        compatibility_raw = compatibility_source.read_bytes()
        compatibility_hash = _sha256_bytes(compatibility_raw)
        compatibility_target = self.layout.compatibility_manifest
        if compatibility_target.exists() or compatibility_target.is_symlink():
            if not _regular_file(compatibility_target):
                raise InstallError("compatibility manifest is not a regular file")
            if not secrets.compare_digest(
                _sha256_file(compatibility_target), compatibility_hash
            ):
                if state is None:
                    raise InstallError(
                        "unowned compatibility manifest already exists"
                    )
                _backup_file(
                    compatibility_target,
                    self.layout.backups / "catalog",
                    compatibility_target.name,
                )
                _atomic_write(compatibility_target, compatibility_raw, 0o600)
                changed = True
        else:
            _atomic_write(compatibility_target, compatibility_raw, 0o600)
            changed = True

        upstream_artifact = (
            self.layout.artifacts / "upstream-known-tool-catalog.json"
        )
        upstream_hash = restore_owned_pair(
            upstream_source,
            self.layout.upstream_catalog,
            upstream_artifact,
            upstream_owned_hash,
            "upstream snapshot",
        )
        review = _load_release_submodule(
            self.layout.release, "catalog_review"
        )
        try:
            review.load_catalog(self.layout.upstream_catalog)
        except Exception as exc:
            raise InstallError("upstream snapshot validation failed: " + str(exc))

        catalog_hash = restore_owned_pair(
            durable_source,
            self.layout.effective_catalog,
            self.layout.catalog_artifact,
            owned_hash,
            "durable catalog",
        )
        _, details, _ = _validate_durable_contract(
            self.layout.release,
            self.layout.effective_catalog,
            self.layout.compatibility_manifest,
        )
        return (
            catalog_hash,
            details,
            {
                "artifact_sha256": _sha256_file(self.layout.catalog_artifact),
                "upstream_snapshot_sha256": upstream_hash,
                "compatibility_manifest_sha256": compatibility_hash,
            },
            changed,
        )

    def _install_plugin(
        self,
        state: Optional[Mapping[str, Any]],
        credentials: Mapping[str, str],
        install_run_id: str,
        *,
        force_render: bool,
    ) -> Tuple[str, str, bool]:
        state_plugin = state.get("plugin") if isinstance(state, Mapping) else None
        owned_plugin_hash = (
            state_plugin.get("sha256")
            if isinstance(state_plugin, Mapping)
            else None
        )
        artifact = self.layout.plugin_artifact
        render = force_render or not _regular_file(artifact)
        if not render and isinstance(state_plugin, Mapping):
            artifact_hash = state_plugin.get("artifact_sha256")
            if not isinstance(artifact_hash, str) or not secrets.compare_digest(
                _sha256_file(artifact), artifact_hash
            ):
                render = True
        if render:
            if artifact.exists() or artifact.is_symlink():
                if not _regular_file(artifact):
                    raise InstallError("plugin artifact is not a regular file")
                _backup_file(
                    artifact, self.layout.backups / "plugin", "plugin-artifact"
                )
            package = _render_plugin(
                self.layout.release,
                studio_token=credentials["studio_token"],
                install_run_id=install_run_id,
            )
            _atomic_write(artifact, package, 0o600)
        artifact_hash = _sha256_file(artifact)

        target = self.layout.plugin_target
        changed = False
        if target.exists() or target.is_symlink():
            if not _regular_file(target):
                raise InstallError("Studio plugin target is not a regular file")
            current_hash = _sha256_file(target)
            if secrets.compare_digest(current_hash, artifact_hash):
                os.chmod(target, 0o600)
                return artifact_hash, artifact_hash, changed
            if state is None:
                raise InstallError(
                    "Studio plugin target exists without v2 ownership state"
                )
            if not isinstance(owned_plugin_hash, str):
                raise InstallError("install state lacks plugin ownership hash")
            _backup_file(target, self.layout.backups / "plugin", PLUGIN_FILENAME)
        _ensure_parent_directory(target.parent)
        _copy_atomic(artifact, target, 0o600)
        changed = True
        return artifact_hash, artifact_hash, changed

    def _install_launchers(
        self, state: Optional[Mapping[str, Any]]
    ) -> Tuple[Dict[str, str], bool]:
        bootstrap = _launcher_bootstrap_source(
            self.package_root, self.layout, self.python_executable
        )
        launcher = _shell_exec(
            self.python_executable, self.layout.launcher_bootstrap
        )
        manager_script = self.layout.package / "install.py"
        manager = _shell_exec(
            self.python_executable,
            manager_script,
            ("--prefix", str(self.layout.support_root)),
        )
        expected = {
            self.layout.launcher_bootstrap: (bootstrap, 0o700),
            self.layout.launcher: (launcher, 0o700),
            self.layout.manager: (manager, 0o700),
        }
        state_launchers = (
            state.get("launchers") if isinstance(state, Mapping) else None
        )
        changed = False
        hashes: Dict[str, str] = {}
        for path, (data, mode) in expected.items():
            digest = _sha256_bytes(data)
            hashes[path.name] = digest
            if path.exists() or path.is_symlink():
                if not _regular_file(path):
                    raise InstallError("launcher target is not a regular file")
                if secrets.compare_digest(_sha256_file(path), digest):
                    os.chmod(path, mode)
                    continue
                if state is None:
                    raise InstallError(
                        "launcher target exists without v2 ownership state"
                    )
                previous = (
                    state_launchers.get(path.name)
                    if isinstance(state_launchers, Mapping)
                    else None
                )
                if not isinstance(previous, str):
                    raise InstallError("install state lacks launcher ownership")
                _backup_file(path, self.layout.backups / "launchers", path.name)
            _atomic_write(path, data, mode)
            changed = True
        return hashes, changed

    def install(
        self,
        *,
        repair: bool = False,
        replace_owned_config: bool = False,
        rotate_secrets: bool = False,
    ) -> Dict[str, Any]:
        try:
            require_supported_platform()
            require_supported_runtime()
        except UnsupportedPlatformError as exc:
            raise InstallError(str(exc)) from exc
        updater = _load_release_updater_module()
        release_updater = updater.ReleaseUpdater(self)
        recovery_status = release_updater.interrupted_update_status()
        if repair:
            try:
                recovery = release_updater.recover_interrupted_update()
            except updater.UpdateError as exc:
                raise InstallError(
                    "interrupted release recovery was refused: " + str(exc)
                ) from exc
            if recovery.get("recovered") is True:
                return {
                    **recovery,
                    "support_root": str(self.layout.support_root),
                    "plugin": str(self.layout.plugin_target),
                    "codex_server": SERVER_NAME,
                    "v1_fallback": "untouched",
                }
        elif (
            recovery_status.get("present") is True
            and recovery_status.get("active") is not True
        ):
            raise InstallError(
                "an interrupted release transaction exists; run repair "
                "instead of continuing the half-installed candidate"
            )
        state = self._load_state(optional=True)
        if isinstance(state, Mapping):
            installed_version = state.get("version")
            if (
                not isinstance(installed_version, str)
                or _SAFE_VERSION.fullmatch(installed_version) is None
            ):
                raise InstallError("install state version is invalid")
            if (
                installed_version != VERSION
                and not release_updater.authorizes_cross_version_install(
                    VERSION
                )
            ):
                raise InstallError(
                    "direct cross-version installation is refused; use "
                    "roblox-studio-mcp-v2-manage update with an exact verified "
                    "release instead"
                )
        self._preflight_first_install(state)
        expected_block = _expected_codex_block(self.layout)
        _preflight_codex(
            self.layout,
            state,
            expected_block,
            replace_owned_config=replace_owned_config,
        )
        lifecycle_stop = None
        if state is not None and self._lifecycle_sensitive_change_needed(
            state, rotate_secrets=rotate_secrets
        ):
            # Authenticate with the existing secrets/config before changing
            # any installed runtime, credential, catalog, or launcher byte.
            lifecycle_stop = self._safe_stop_lifecycle()
        self._prepare_directories()
        tree_changes = self._install_package_and_release()
        credentials, credentials_changed = _read_or_create_secrets(
            self.layout, rotate=rotate_secrets
        )

        old_runtime_hash = (
            state.get("runtime", {}).get("sha256")
            if isinstance(state, Mapping)
            and isinstance(state.get("runtime"), Mapping)
            else None
        )
        runtime_hash, runtime_changed = _write_owned_json(
            self.layout.runtime_config,
            _runtime_value(self.layout),
            state_hash=old_runtime_hash,
            backup_dir=self.layout.backups / "runtime",
            label=RUNTIME_FILENAME,
            first_install_collision_error=(
                "runtime.json already exists without v2 ownership state"
            ),
        )
        catalog_hash, catalog_details, catalog_ownership, catalog_changed = (
            self._seed_or_repair_catalog(state)
        )

        install_run_id = (
            state.get(INSTALL_RUN_ID_KEY) if isinstance(state, Mapping) else None
        )
        if (
            rotate_secrets
            or not isinstance(install_run_id, str)
            or _SAFE_RUN_ID.fullmatch(install_run_id) is None
        ):
            install_run_id = secrets.token_hex(16)
        plugin_hash, artifact_hash, plugin_changed = self._install_plugin(
            state,
            credentials,
            install_run_id,
            force_render=(
                rotate_secrets
                or credentials_changed
                or state is None
                or state.get("version") != VERSION
            ),
        )
        launcher_hashes, launchers_changed = self._install_launchers(state)
        (
            block_hash,
            config_backup,
            config_changed,
            codex_separator_hex,
        ) = _write_codex_config(
            self.layout,
            expected_block,
            state,
            replace_owned_config=replace_owned_config,
        )

        installed_at = (
            state.get("installed_at")
            if isinstance(state, Mapping)
            and isinstance(state.get("installed_at"), str)
            else _datetime.datetime.now(_datetime.timezone.utc).isoformat()
        )
        prior_codex = (
            state.get("codex")
            if isinstance(state, Mapping)
            and isinstance(state.get("codex"), Mapping)
            else {}
        )
        last_config_backup = (
            config_backup
            if config_backup is not None
            else prior_codex.get("last_backup")
        )
        state_value: Dict[str, Any] = {
            "format": INSTALL_STATE_FORMAT,
            "schema_version": INSTALL_STATE_VERSION,
            "product": PRODUCT,
            "version": VERSION,
            "support_root": str(self.layout.support_root),
            "installed_at": installed_at,
            "python_executable": self.python_executable,
            INSTALL_RUN_ID_KEY: install_run_id,
            "release_manifest_sha256": _sha256_file(
                self.package_root / PACKAGE_MANIFEST_FILENAME
            ),
            "runtime": {
                "path": str(self.layout.runtime_config),
                "sha256": runtime_hash,
            },
            "catalog": {
                "path": str(self.layout.effective_catalog),
                "sha256": catalog_hash,
                "catalog_version": catalog_details.get("catalog_version"),
                "upstream_version": catalog_details.get("upstream_version"),
                "upstream_source_sha256": catalog_details.get(
                    "upstream_source_sha256"
                ),
                "upstream_compatibility": catalog_details.get(
                    "upstream_compatibility"
                ),
                **catalog_ownership,
            },
            "plugin": {
                "path": str(self.layout.plugin_target),
                "sha256": plugin_hash,
                "artifact_path": str(self.layout.plugin_artifact),
                "artifact_sha256": artifact_hash,
            },
            "launchers": launcher_hashes,
            "codex": {
                "config_path": str(self.layout.codex_config),
                "table": SERVER_HEADER,
                "block_sha256": block_hash,
                "last_backup": last_config_backup,
                "inserted_separator_hex": codex_separator_hex,
            },
        }
        state_bytes = _json_bytes(state_value)
        state_changed = (
            not _regular_file(self.layout.install_state)
            or self.layout.install_state.read_bytes() != state_bytes
        )
        if state_changed:
            _atomic_write(self.layout.install_state, state_bytes, 0o600)

        return {
            "ok": True,
            "action": "repair" if repair else "install",
            "version": VERSION,
            "support_root": str(self.layout.support_root),
            "plugin": str(self.layout.plugin_target),
            "codex_server": SERVER_NAME,
            "catalog": catalog_details,
            "changed": any(
                (
                    tree_changes["package"],
                    tree_changes["release"],
                    credentials_changed,
                    runtime_changed,
                    catalog_changed,
                    plugin_changed,
                    launchers_changed,
                    config_changed,
                    state_changed,
                )
            ),
            "restart_required": (
                "Restart Codex to load or refresh the Roblox_Studio_v2 MCP "
                "registration; restart/reload Studio to load a newly installed "
                "or repaired plugin."
            ),
            "lifecycle_stop": lifecycle_stop,
        }

    def doctor(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        lifecycle_status: Optional[Dict[str, Any]] = None

        def record(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        try:
            state = self._load_state(optional=False)
            record("install_state", True, "owned state is valid")
        except InstallError as exc:
            state = None
            record("install_state", False, str(exc))

        package_items = [
            (relative, metadata)
            for relative, metadata in self.manifest_files.items()
        ]
        installed_manifest = (
            self.package_root / PACKAGE_MANIFEST_FILENAME
        )
        package_items.append(
            (
                PACKAGE_MANIFEST_FILENAME,
                {
                    "sha256": _sha256_file(installed_manifest),
                    "size": installed_manifest.stat().st_size,
                    "mode": 0o644,
                },
            )
        )
        release_items = [
            (relative[len("payload/") :], metadata)
            for relative, metadata in self.manifest_files.items()
            if relative.startswith("payload/")
        ]
        record(
            "installed_package",
            self.layout.package.is_dir()
            and _tree_matches(self.layout.package, package_items),
            str(self.layout.package),
        )
        record(
            "installed_release",
            self.layout.release.is_dir()
            and _tree_matches(self.layout.release, release_items),
            str(self.layout.release),
        )

        try:
            runtime = _load_json(self.layout.runtime_config, "runtime.json")
            runtime_ok = runtime == _runtime_value(self.layout)
            record(
                "runtime_config",
                runtime_ok,
                "schema/path pinned" if runtime_ok else "runtime.json drifted",
            )
        except InstallError as exc:
            record("runtime_config", False, str(exc))

        try:
            _validate_secrets(_load_json(self.layout.secrets_config, "secrets.json"))
            permission_ok = (
                stat.S_IMODE(self.layout.secrets_config.stat().st_mode) == 0o600
            )
            record(
                "secrets",
                permission_ok,
                "valid distinct credentials; mode 0600"
                if permission_ok
                else "credentials valid but mode is not 0600",
            )
        except InstallError as exc:
            record("secrets", False, str(exc))

        catalog_details: Dict[str, Any] = {}
        try:
            _, catalog_details, catalog_raw = _validate_durable_contract(
                self.layout.release,
                self.layout.effective_catalog,
                self.layout.compatibility_manifest,
            )
            catalog_hash = _sha256_bytes(catalog_raw)
            state_hash = (
                state.get("catalog", {}).get("sha256")
                if isinstance(state, Mapping)
                and isinstance(state.get("catalog"), Mapping)
                else None
            )
            catalog_ok = bool(catalog_details["compatible"]) and (
                isinstance(state_hash, str)
                and secrets.compare_digest(catalog_hash, state_hash)
            )
            record(
                "catalog",
                catalog_ok,
                json.dumps(catalog_details, sort_keys=True),
            )
        except InstallError as exc:
            record("catalog", False, str(exc))

        expected_block = _expected_codex_block(self.layout)
        try:
            codex_data = self.layout.codex_config.read_bytes()
            table = _find_codex_table(codex_data)
            codex_ok = table is not None and secrets.compare_digest(
                _sha256_bytes(table[2]), _sha256_bytes(expected_block)
            )
            record(
                "codex_config",
                codex_ok,
                "exact owned table present; no secret fields"
                if codex_ok
                else "owned table missing or drifted",
            )
        except (OSError, InstallError) as exc:
            record("codex_config", False, str(exc))

        state_launchers = (
            state.get("launchers") if isinstance(state, Mapping) else {}
        )
        for path in (
            self.layout.launcher,
            self.layout.launcher_bootstrap,
            self.layout.manager,
        ):
            expected_hash = (
                state_launchers.get(path.name)
                if isinstance(state_launchers, Mapping)
                else None
            )
            ok = (
                _regular_file(path)
                and isinstance(expected_hash, str)
                and secrets.compare_digest(_sha256_file(path), expected_hash)
            )
            record("launcher:" + path.name, ok, str(path))

        if _regular_file(self.layout.launcher):
            try:
                lifecycle_status = self._invoke_lifecycle("doctor")
                lifecycle = lifecycle_status.get("lifecycle")
                condition = (
                    lifecycle.get("condition")
                    if isinstance(lifecycle, Mapping)
                    else None
                )
                lifecycle_ok = condition in {"stopped", "healthy_idle"}
                record(
                    "lifecycle",
                    lifecycle_ok and lifecycle_status.get("ok") is True,
                    (
                        "condition="
                        + str(condition)
                        + "; installed_v1_cache="
                        + str(
                            lifecycle_status.get("catalog", {}).get(
                                "installed_v1_cache"
                            )
                            if isinstance(
                                lifecycle_status.get("catalog"), Mapping
                            )
                            else None
                        )
                    ),
                )
            except InstallError as exc:
                record("lifecycle", False, str(exc))
        else:
            record("lifecycle", False, "stable launcher is missing")

        state_plugin = state.get("plugin") if isinstance(state, Mapping) else {}
        plugin_expected = (
            state_plugin.get("sha256")
            if isinstance(state_plugin, Mapping)
            else None
        )
        artifact_expected = (
            state_plugin.get("artifact_sha256")
            if isinstance(state_plugin, Mapping)
            else None
        )
        plugin_ok = (
            _regular_file(self.layout.plugin_target)
            and isinstance(plugin_expected, str)
            and secrets.compare_digest(
                _sha256_file(self.layout.plugin_target), plugin_expected
            )
        )
        artifact_ok = (
            _regular_file(self.layout.plugin_artifact)
            and isinstance(artifact_expected, str)
            and secrets.compare_digest(
                _sha256_file(self.layout.plugin_artifact), artifact_expected
            )
        )
        record("studio_plugin", plugin_ok, str(self.layout.plugin_target))
        record("plugin_artifact", artifact_ok, str(self.layout.plugin_artifact))
        record(
            "v1_fallback_boundary",
            True,
            (
                "installer owns neither mcp_servers.Roblox_Studio nor "
                "MCPStudioPlugin.rbxm"
            ),
        )
        release_update_status: Dict[str, Any] = {}
        try:
            release_update_status = (
                _load_release_updater_module().ReleaseUpdater(self).status()
            )
            record(
                "release_update_state",
                release_update_status.get("ok") is True,
                json.dumps(release_update_status, sort_keys=True),
            )
        except Exception as exc:
            record("release_update_state", False, str(exc))
        return {
            "ok": all(item["ok"] for item in checks),
            "version": VERSION,
            "platform": detect_platform().as_dict(),
            "python": {
                "version": ".".join(str(item) for item in sys.version_info[:3]),
                "minimum": "3.9",
                "supported": sys.version_info[:2] >= (3, 9),
            },
            "support_root": str(self.layout.support_root),
            "catalog": catalog_details,
            "release_updates": release_update_status,
            "lifecycle": lifecycle_status,
            "checks": checks,
        }

    def catalog_status(self) -> Dict[str, Any]:
        _, details, raw = _validate_durable_contract(
            self.layout.release,
            self.layout.effective_catalog,
            self.layout.compatibility_manifest,
        )
        review = _load_release_submodule(
            self.layout.release, "catalog_review"
        )
        try:
            cache_audit = review.audit_installed_v1_cache(
                baseline_path=self.layout.upstream_catalog,
                compatibility_manifest_path=self.layout.compatibility_manifest,
                durable_catalog_path=self.layout.effective_catalog,
            )
        except Exception:
            cache_audit = {
                "available": False,
                "status": "unavailable_or_unsafe",
            }
        receipts = []
        for path in sorted(
            self.layout.config.glob("catalog-import-receipt-*.json"),
            reverse=True,
        ):
            if _regular_file(path):
                receipts.append(str(path))
        return {
            "ok": bool(details["compatible"]),
            "path": str(self.layout.effective_catalog),
            "sha256": _sha256_bytes(raw),
            **details,
            "upstream_snapshot": {
                "path": str(self.layout.upstream_catalog),
                "sha256": _sha256_file(self.layout.upstream_catalog),
            },
            "installed_v1_cache": cache_audit,
            "receipts": receipts,
            "automatic_updates": False,
        }

    def _catalog_candidate(self, candidate: Optional[Path]) -> Path:
        if candidate is not None:
            return Path(candidate).resolve(strict=True)
        review = _load_release_submodule(
            self.layout.release, "catalog_review"
        )
        try:
            return Path(review.installed_v1_cache_candidate())
        except Exception as exc:
            raise InstallError(
                "trusted installed v1 cache is unavailable/unsafe; pass an "
                "explicit --artifact instead: " + str(exc)
            )

    def _prepare_catalog_import(
        self, candidate: Optional[Path]
    ) -> Tuple[Any, Path, Dict[str, Any], bytes, bytes]:
        review = _load_release_submodule(
            self.layout.release, "catalog_review"
        )
        candidate_path = self._catalog_candidate(candidate)
        try:
            prepared = review.prepare_catalog_import(
                self.layout.upstream_catalog,
                candidate_path,
                compatibility_manifest_path=self.layout.compatibility_manifest,
                durable_catalog_path=self.layout.effective_catalog,
                regenerate_durable=True,
            )
            _baseline_payload, baseline_raw = review.load_catalog(
                self.layout.upstream_catalog
            )
            _candidate_payload, candidate_raw = review.load_catalog(
                candidate_path
            )
        except Exception as exc:
            raise InstallError(
                "catalog candidate failed closed review: " + str(exc)
            )
        return review, candidate_path, prepared, baseline_raw, candidate_raw

    def catalog_diff(self, candidate: Optional[Path]) -> Dict[str, Any]:
        (
            _review,
            candidate_path,
            prepared,
            baseline_raw,
            candidate_raw,
        ) = self._prepare_catalog_import(candidate)
        baseline_text = baseline_raw.decode("utf-8").splitlines(keepends=True)
        candidate_text = candidate_raw.decode("utf-8").splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                baseline_text,
                candidate_text,
                fromfile="installed/upstream-known-tool-catalog.json",
                tofile="candidate/" + candidate_path.name,
            )
        )
        return {
            "ok": prepared.get("ready") is True,
            "candidate": {
                "path": str(candidate_path),
                "sha256": _sha256_bytes(candidate_raw),
            },
            "prepared": prepared,
            "diff": diff,
            "import_command_requires_accept_sha256": True,
            "automatic_update": False,
        }

    def _update_catalog_state(self) -> Dict[str, Any]:
        _, details, raw = _validate_durable_contract(
            self.layout.release,
            self.layout.effective_catalog,
            self.layout.compatibility_manifest,
        )
        digest = _sha256_bytes(raw)
        _atomic_write(self.layout.catalog_artifact, raw, 0o600)
        upstream_hash = _sha256_file(self.layout.upstream_catalog)
        _atomic_write(
            self.layout.artifacts / "upstream-known-tool-catalog.json",
            self.layout.upstream_catalog.read_bytes(),
            0o600,
        )
        state = self._load_state(optional=False)
        state["catalog"] = {
            "path": str(self.layout.effective_catalog),
            "sha256": digest,
            "artifact_sha256": digest,
            "catalog_version": details.get("catalog_version"),
            "upstream_version": details.get("upstream_version"),
            "upstream_source_sha256": details.get("upstream_source_sha256"),
            "upstream_compatibility": details.get("upstream_compatibility"),
            "upstream_snapshot_sha256": upstream_hash,
            "compatibility_manifest_sha256": _sha256_file(
                self.layout.compatibility_manifest
            ),
        }
        _atomic_write(self.layout.install_state, _json_bytes(state), 0o600)
        return {"sha256": digest, "details": details}

    def _start_and_verify_catalog(self) -> Dict[str, Any]:
        state = self._load_state(optional=False)
        state_catalog = state.get("catalog")
        owned_raw_digest = (
            state_catalog.get("sha256")
            if isinstance(state_catalog, Mapping)
            else None
        )
        effective_raw_digest = _sha256_file(self.layout.effective_catalog)
        if (
            not isinstance(owned_raw_digest, str)
            or not secrets.compare_digest(
                effective_raw_digest, owned_raw_digest
            )
        ):
            raise InstallError(
                "effective catalog bytes do not match the owned state digest"
            )
        lifecycle_module = _load_release_submodule(
            self.layout.release, "lifecycle"
        )
        try:
            expected_runtime_digest = lifecycle_module._catalog_digest(
                lifecycle_module.ToolCatalog.from_file(
                    self.layout.effective_catalog
                )
            )
        except Exception as exc:
            raise InstallError(
                "unable to derive the exact effective catalog runtime digest: "
                + str(exc)
            )
        started = self._invoke_lifecycle("start", timeout=30)
        diagnosed = self._invoke_lifecycle("doctor", timeout=20)
        broker = started.get("broker")
        catalog = diagnosed.get("catalog")
        broker_digest = (
            broker.get("catalog_sha256")
            if isinstance(broker, Mapping)
            else None
        )
        doctor_digest = (
            catalog.get("catalog_sha256")
            if isinstance(catalog, Mapping)
            else None
        )
        if (
            not isinstance(broker_digest, str)
            or not isinstance(doctor_digest, str)
            or not secrets.compare_digest(broker_digest, doctor_digest)
            or not secrets.compare_digest(
                broker_digest, expected_runtime_digest
            )
        ):
            raise InstallError(
                "restarted broker did not prove the active catalog digest"
            )
        return {
            "start": started,
            "doctor_catalog_sha256": doctor_digest,
            "effective_file_sha256": effective_raw_digest,
            "effective_runtime_catalog_sha256": expected_runtime_digest,
        }

    def catalog_import(
        self, candidate: Optional[Path], accept_sha256: str
    ) -> Dict[str, Any]:
        (
            review,
            candidate_path,
            prepared,
            _baseline_raw,
            candidate_raw,
        ) = self._prepare_catalog_import(candidate)
        digest = _sha256_bytes(candidate_raw)
        if not isinstance(accept_sha256, str) or not secrets.compare_digest(
            digest, accept_sha256.lower()
        ):
            raise InstallError(
                "candidate checksum acknowledgement does not match; run "
                "catalog diff and review it first"
            )
        if prepared.get("ready") is not True:
            raise InstallError("catalog preparation did not return a ready result")
        self._safe_stop_lifecycle()
        receipt = None
        old_state = self.layout.install_state.read_bytes()
        try:
            imported = review.import_reviewed_catalog(
                self.layout.upstream_catalog,
                candidate_path,
                approve_reviewed_changes=True,
                compatibility_manifest_path=self.layout.compatibility_manifest,
                durable_catalog_path=self.layout.effective_catalog,
                regenerate_durable=True,
                expected_candidate_sha256=digest,
            )
            receipt_value = imported.get("receipt")
            if not isinstance(receipt_value, str):
                raise InstallError("catalog import did not return a rollback receipt")
            receipt = Path(receipt_value)
            ownership = self._update_catalog_state()
            health = self._start_and_verify_catalog()
        except Exception as exc:
            if receipt is not None:
                try:
                    # Verification may have started the new broker before
                    # failing. Never rewrite its catalog out from underneath
                    # it: restoration requires a second authenticated safe stop.
                    self._safe_stop_lifecycle()
                except InstallError as stop_exc:
                    raise InstallError(
                        "catalog import was applied but post-start verification "
                        "failed; rollback was not attempted because the new "
                        "broker refused safe stop. Disk/state still match that "
                        "broker: " + str(stop_exc)
                    ) from exc
                try:
                    review.rollback_catalog_import(receipt)
                    _atomic_write(
                        self.layout.catalog_artifact,
                        self.layout.effective_catalog.read_bytes(),
                        0o600,
                    )
                    _atomic_write(
                        self.layout.artifacts
                        / "upstream-known-tool-catalog.json",
                        self.layout.upstream_catalog.read_bytes(),
                        0o600,
                    )
                    _atomic_write(self.layout.install_state, old_state, 0o600)
                except Exception as rollback_exc:
                    raise InstallError(
                        "catalog import failed and rollback also failed: "
                        + str(rollback_exc)
                    ) from exc
            try:
                self._start_and_verify_catalog()
            except Exception:
                pass
            if isinstance(exc, InstallError):
                raise
            raise InstallError("reviewed catalog import failed: " + str(exc))
        return {
            "ok": True,
            "changed": True,
            "candidate_sha256": digest,
            "prepared": prepared,
            "import": imported,
            "active_catalog": ownership,
            "verified_runtime": health,
        }

    def catalog_rollback(
        self,
        accept_current_sha256: str,
        *,
        receipt: Optional[Path] = None,
    ) -> Dict[str, Any]:
        _, _details, current_raw = _validate_durable_contract(
            self.layout.release,
            self.layout.effective_catalog,
            self.layout.compatibility_manifest,
        )
        current_hash = _sha256_bytes(current_raw)
        if not isinstance(accept_current_sha256, str) or not secrets.compare_digest(
            current_hash, accept_current_sha256.lower()
        ):
            raise InstallError(
                "current catalog checksum acknowledgement does not match"
            )
        if receipt is None:
            matching_receipts = []
            current_hashes = {
                self.layout.effective_catalog.name: _sha256_file(
                    self.layout.effective_catalog
                ),
                self.layout.upstream_catalog.name: _sha256_file(
                    self.layout.upstream_catalog
                ),
            }
            for path in self.layout.config.glob(
                "catalog-import-receipt-*.json"
            ):
                if not _regular_file(path):
                    continue
                try:
                    payload = _load_json(path, "catalog import receipt")
                    entries = payload.get("entries")
                    if (
                        payload.get("format")
                        != "studio-mcp-v2-catalog-import-receipt"
                        or not isinstance(entries, list)
                    ):
                        continue
                    receipt_hashes = {
                        item.get("target"): item.get("installed_sha256")
                        for item in entries
                        if isinstance(item, dict)
                    }
                    if all(
                        secrets.compare_digest(
                            receipt_hashes.get(name, ""), digest
                        )
                        for name, digest in current_hashes.items()
                    ):
                        matching_receipts.append(path)
                except (InstallError, TypeError):
                    continue
            if not matching_receipts:
                raise InstallError(
                    "no catalog receipt matches both active catalog hashes"
                )
            if len(matching_receipts) > 1:
                raise InstallError(
                    "multiple receipts match the active catalogs; pass "
                    "--receipt explicitly"
                )
            receipt = matching_receipts[0]
        receipt = Path(receipt).resolve(strict=True)
        if receipt.parent != self.layout.config.resolve(strict=True):
            raise InstallError("rollback receipt is outside the owned config directory")

        review = _load_release_submodule(
            self.layout.release, "catalog_review"
        )
        self._safe_stop_lifecycle()
        old_state = self.layout.install_state.read_bytes()
        old_effective = self.layout.effective_catalog.read_bytes()
        old_upstream = self.layout.upstream_catalog.read_bytes()
        old_catalog_artifact = self.layout.catalog_artifact.read_bytes()
        old_upstream_artifact = (
            self.layout.artifacts / "upstream-known-tool-catalog.json"
        ).read_bytes()
        try:
            result = review.rollback_catalog_import(receipt)
            ownership = self._update_catalog_state()
            health = self._start_and_verify_catalog()
        except Exception as exc:
            try:
                # A failed post-rollback start may have started a broker using
                # the restored catalog. Fence it before returning every owned
                # catalog/state byte to the pre-command snapshot.
                self._safe_stop_lifecycle()
                _atomic_write(
                    self.layout.effective_catalog, old_effective, 0o600
                )
                _atomic_write(
                    self.layout.upstream_catalog, old_upstream, 0o600
                )
                _atomic_write(
                    self.layout.catalog_artifact,
                    old_catalog_artifact,
                    0o600,
                )
                _atomic_write(
                    self.layout.artifacts
                    / "upstream-known-tool-catalog.json",
                    old_upstream_artifact,
                    0o600,
                )
                _atomic_write(self.layout.install_state, old_state, 0o600)
                self._start_and_verify_catalog()
            except InstallError as stop_or_restoration_exc:
                raise InstallError(
                    "catalog rollback changed the reviewed catalogs but "
                    "post-start verification failed; pre-command restoration "
                    "was not performed unless the broker was safely stopped: "
                    + str(stop_or_restoration_exc)
                ) from exc
            except Exception as restoration_exc:
                raise InstallError(
                    "catalog rollback failed and pre-command restoration "
                    "could not be verified: " + str(restoration_exc)
                ) from exc
            if isinstance(exc, InstallError):
                raise
            raise InstallError("catalog rollback failed: " + str(exc))
        return {
            "ok": True,
            "changed": True,
            "rollback": result,
            "active_catalog": ownership,
            "verified_runtime": health,
        }

    def lifecycle(self, command: str) -> Dict[str, Any]:
        return self._invoke_lifecycle(command, timeout=30)

    def update(
        self,
        *,
        tag: str,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        archive: Optional[Path] = None,
        checksum_file: Optional[Path] = None,
        expected_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            require_supported_runtime()
            require_supported_platform()
        except UnsupportedPlatformError as exc:
            raise InstallError(str(exc)) from exc
        updater = _load_release_updater_module()
        try:
            return updater.ReleaseUpdater(self).update(
                tag=tag,
                owner=owner,
                repo=repo,
                archive=archive,
                checksum_file=checksum_file,
                expected_sha256=expected_sha256,
            )
        except updater.UpdateError as exc:
            raise InstallError(str(exc)) from exc

    def rollback_release(
        self,
        *,
        to_version: str,
        accept_current_version: str,
    ) -> Dict[str, Any]:
        try:
            require_supported_runtime()
            require_supported_platform()
        except UnsupportedPlatformError as exc:
            raise InstallError(str(exc)) from exc
        updater = _load_release_updater_module()
        try:
            return updater.ReleaseUpdater(self).rollback(
                to_version=to_version,
                accept_current_version=accept_current_version,
            )
        except updater.UpdateError as exc:
            raise InstallError(str(exc)) from exc

    def uninstall(self, *, skip_stop: bool = False) -> Dict[str, Any]:
        try:
            require_supported_platform()
            require_supported_runtime()
        except UnsupportedPlatformError as exc:
            raise InstallError(str(exc)) from exc
        state = self._load_state(optional=False)
        expected_block = _expected_codex_block(self.layout)
        if not _regular_file(self.layout.codex_config):
            raise InstallError("Codex config is missing; refusing partial uninstall")
        config_data = self.layout.codex_config.read_bytes()
        table = _find_codex_table(config_data)
        if table is None or not secrets.compare_digest(
            _sha256_bytes(table[2]), _sha256_bytes(expected_block)
        ):
            raise InstallError(
                "owned Codex v2 table is missing/drifted; refusing partial uninstall"
            )
        plugin_hash = state.get("plugin", {}).get("sha256")
        if (
            not _regular_file(self.layout.plugin_target)
            or not isinstance(plugin_hash, str)
            or not secrets.compare_digest(
                _sha256_file(self.layout.plugin_target), plugin_hash
            )
        ):
            raise InstallError(
                "owned Studio v2 plugin is missing/drifted; refusing partial uninstall"
            )

        start, end, _ = table
        separator = _owned_codex_separator(
            self.layout, state, config_data, start
        )
        remove_start = start - len(separator)

        stop_result: Any = "skipped"
        if not skip_stop:
            stop_result = self._safe_stop_lifecycle()

        _backup_file(
            self.layout.codex_config,
            self.layout.backups / "codex",
            "config.toml-pre-uninstall",
        )
        _atomic_write(
            self.layout.codex_config,
            config_data[:remove_start] + config_data[end:],
            stat.S_IMODE(self.layout.codex_config.stat().st_mode),
        )

        plugin_recovery = (
            self.layout.backups
            / "plugin"
            / (PLUGIN_FILENAME + ".uninstalled." + _utc_stamp())
        )
        _ensure_private_directory(plugin_recovery.parent)
        os.replace(self.layout.plugin_target, plugin_recovery)
        _fsync_directory(self.layout.plugin_target.parent)
        _fsync_directory(plugin_recovery.parent)

        root = self.layout.support_root
        recovery = root.parent / (root.name + ".uninstalled." + _utc_stamp())
        if recovery.exists() or recovery.is_symlink():
            raise InstallError("uninstall recovery target unexpectedly exists")
        # Store the plugin recovery inside the support root that is moved as one
        # recoverable unit.  No recursive deletion is performed.
        os.replace(root, recovery)
        _fsync_directory(root.parent)
        return {
            "ok": True,
            "version": VERSION,
            "stop": stop_result,
            "support_recovery": str(recovery),
            "v1_fallback": "untouched",
        }


def _format_result(value: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if "checks" in value:
        print(("HEALTHY" if value.get("ok") else "UNHEALTHY") + " — " + VERSION)
        for item in value["checks"]:
            marker = "ok" if item["ok"] else "FAIL"
            print("[" + marker + "] " + item["name"] + ": " + item["detail"])
        return
    if "diff" in value:
        candidate = value["candidate"]
        prepared = value.get("prepared", {})
        print("Candidate review ready=" + str(prepared.get("ready")))
        print("candidate sha256: " + str(candidate.get("sha256")))
        print(value["diff"], end="" if value["diff"].endswith("\n") else "\n")
        return
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install and manage the side-by-side Roblox Studio MCP v2. "
            "V1 is never an owned target."
        )
    )
    parser.add_argument(
        "--prefix",
        type=Path,
        help=(
            "explicit support root for simulation/recovery; normal installs "
            "derive the stable per-user path"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install")
    install.add_argument("--replace-owned-config", action="store_true")
    install.add_argument("--rotate-secrets", action="store_true")

    repair = subparsers.add_parser("repair")
    repair.add_argument("--replace-owned-config", action="store_true")
    repair.add_argument("--rotate-secrets", action="store_true")

    status = subparsers.add_parser("status")
    doctor = subparsers.add_parser("doctor")
    start = subparsers.add_parser("start")
    stop = subparsers.add_parser("stop")

    update = subparsers.add_parser("update")
    update.add_argument("--tag", required=True)
    update.add_argument("--owner")
    update.add_argument("--repo")
    update.add_argument("--archive", type=Path)
    update.add_argument("--checksum-file", type=Path)
    update.add_argument("--expected-sha256")

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--to-version", required=True)
    rollback.add_argument("--accept-current-version", required=True)

    uninstall = subparsers.add_parser("uninstall")

    catalog = subparsers.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(
        dest="catalog_command", required=True
    )
    catalog_commands.add_parser("status")
    catalog_diff = catalog_commands.add_parser("diff")
    catalog_diff.add_argument(
        "--artifact",
        type=Path,
        help="explicit local upstream catalog; default is the trusted v1 cache",
    )
    catalog_import = catalog_commands.add_parser("import")
    catalog_import.add_argument(
        "--artifact",
        type=Path,
        help="explicit local upstream catalog; default is the trusted v1 cache",
    )
    catalog_import.add_argument("--accept-sha256", required=True)
    catalog_rollback = catalog_commands.add_parser("rollback")
    catalog_rollback.add_argument("--accept-current-sha256", required=True)
    catalog_rollback.add_argument("--receipt", type=Path)
    for command_parser in (
        install,
        repair,
        status,
        doctor,
        start,
        stop,
        update,
        rollback,
        uninstall,
        catalog,
    ):
        command_parser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="emit JSON",
        )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    package_root = Path(__file__).resolve().parent
    try:
        layout = InstallLayout.for_user(prefix=args.prefix)
        installer = Installer(package_root, layout)
        if args.command == "install":
            result = installer.install(
                replace_owned_config=args.replace_owned_config,
                rotate_secrets=args.rotate_secrets,
            )
        elif args.command == "repair":
            result = installer.install(
                repair=True,
                replace_owned_config=args.replace_owned_config,
                rotate_secrets=args.rotate_secrets,
            )
        elif args.command in ("status", "doctor"):
            result = installer.doctor()
        elif args.command in ("start", "stop"):
            result = installer.lifecycle(args.command)
        elif args.command == "update":
            result = installer.update(
                tag=args.tag,
                owner=args.owner,
                repo=args.repo,
                archive=args.archive,
                checksum_file=args.checksum_file,
                expected_sha256=args.expected_sha256,
            )
        elif args.command == "rollback":
            result = installer.rollback_release(
                to_version=args.to_version,
                accept_current_version=args.accept_current_version,
            )
        elif args.command == "uninstall":
            result = installer.uninstall()
        elif args.command == "catalog":
            if args.catalog_command == "status":
                result = installer.catalog_status()
            elif args.catalog_command == "diff":
                result = installer.catalog_diff(args.artifact)
            elif args.catalog_command == "import":
                result = installer.catalog_import(
                    args.artifact, args.accept_sha256
                )
            else:
                result = installer.catalog_rollback(
                    args.accept_current_sha256,
                    receipt=args.receipt,
                )
        else:
            raise InstallError("unsupported command")
        _format_result(result, getattr(args, "json", False))
        if not result.get("ok", False):
            raise SystemExit(1)
    except InstallError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            sys.stderr.write("Studio MCP v2 installer refused: " + str(exc) + "\n")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
