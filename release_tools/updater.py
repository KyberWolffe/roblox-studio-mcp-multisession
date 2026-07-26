"""Verified tagged-release update and retained-version rollback transactions."""

from __future__ import annotations

import contextlib
import datetime as _datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

try:  # Source-tree import.
    from . import bootstrap
except ImportError:  # Portable archive root import.
    import bootstrap  # type: ignore

from platform_support import (
    UnsupportedPlatformError,
    require_supported_platform,
    require_supported_runtime,
)


RECEIPT_FORMAT = "roblox-studio-mcp-v2-release-transaction"
RECEIPT_VERSION = 1
LATEST_RECEIPT_FILENAME = "release-transaction-latest.json"
PENDING_TRANSACTION_FILENAME = "release-transaction-pending.json"
_SAFE_VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?$"
)
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NONCE = re.compile(r"^[0-9a-f]{32}$")
_ACTIVE_VALIDATION_NONCES = set()
_ACTIVE_RECOVERY_NONCES = set()
_INSTALL_STATE_FORMAT = "roblox-studio-mcp-v2-install-state"


class UpdateError(RuntimeError):
    """A release update or rollback was rejected or restored safely."""


def _utc_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


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
        raise UpdateError(
            "unable to open directory for durability sync: " + str(path)
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise UpdateError(
                "durability sync target is not a directory: " + str(path)
            )
        os.fsync(descriptor)
    except OSError as exc:
        raise UpdateError(
            "unable to durability-sync directory: " + str(path)
        ) from exc
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise UpdateError("expected a non-symlink directory: " + str(path))
    else:
        _ensure_parent_directory(path.parent)
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    os.chmod(path, 0o700)
    _fsync_directory(path)


def _ensure_parent_directory(path: Path) -> None:
    """Create missing parents privately without chmoding shared parents."""

    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise UpdateError(
            "parent path is not a non-symlink directory: " + str(current)
        )
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _fsync_directory(directory.parent)


def _atomic_write(path: Path, value: bytes, mode: int = 0o600) -> None:
    _ensure_parent_directory(path.parent)
    descriptor, name = tempfile.mkstemp(
        prefix="." + path.name + ".tmp-", dir=str(path.parent)
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
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


def _safe_version(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
        raise UpdateError(label + " is invalid")
    return value


def _load_json(path: Path, label: str) -> Dict[str, Any]:
    if not _regular_file(path):
        raise UpdateError(label + " must be a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError(label + " is invalid JSON: " + str(exc)) from exc
    if not isinstance(value, dict):
        raise UpdateError(label + " must contain an object")
    return value


def _copy_regular_tree(source: Path, destination: Path) -> Dict[str, Any]:
    if source.is_symlink() or not source.is_dir():
        raise UpdateError("transaction source is not a safe directory: " + str(source))
    _ensure_parent_directory(destination.parent)
    destination.mkdir(mode=0o700, exist_ok=False)
    _fsync_directory(destination.parent)
    entries: Dict[str, Any] = {}
    for root, directory_names, file_names in os.walk(source, followlinks=False):
        root_path = Path(root)
        for directory_name in directory_names:
            directory = root_path / directory_name
            if directory.is_symlink():
                raise UpdateError(
                    "transaction source contains a symlink: " + str(directory)
                )
            relative = directory.relative_to(source)
            copied_directory = destination / relative
            if not copied_directory.exists():
                _ensure_parent_directory(copied_directory.parent)
                copied_directory.mkdir(mode=0o700)
                _fsync_directory(copied_directory.parent)
        for file_name in file_names:
            item = root_path / file_name
            if not _regular_file(item):
                raise UpdateError(
                    "transaction source contains a non-regular file: " + str(item)
                )
            relative = item.relative_to(source)
            target = destination / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = item.read_bytes()
            mode = stat.S_IMODE(item.stat().st_mode)
            _atomic_write(target, data, mode)
            entries[str(relative)] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": mode,
                "size": len(data),
            }
    return entries


def _restore_regular_tree_in_place(
    source: Path,
    target: Path,
    entries: Mapping[str, Any],
) -> None:
    """Restore a tree without ever removing its directory or stable files."""

    _verify_regular_tree(source, entries)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise UpdateError(
                "cannot restore over an unsafe shared directory: "
                + str(target)
            )
    else:
        _ensure_parent_directory(target.parent)
        target.mkdir(mode=0o700)
        _fsync_directory(target.parent)

    for item in target.rglob("*"):
        if item.is_symlink():
            raise UpdateError(
                "cannot restore a shared tree containing a symlink: "
                + str(item)
            )

    required_directories = set()
    for relative in entries:
        relative_path = Path(relative)
        for parent in relative_path.parents:
            if str(parent) != ".":
                required_directories.add(parent)
    for relative in sorted(
        required_directories, key=lambda value: len(value.parts)
    ):
        directory = target / relative
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise UpdateError(
                    "cannot restore over an unsafe tree entry: "
                    + str(directory)
                )
            continue
        directory.mkdir(mode=0o700)
        _fsync_directory(directory.parent)

    # Every expected file, including the stable manager, is atomically
    # replaced before stale files are removed. A crash therefore leaves a
    # callable old-or-new manager path for the next repair attempt.
    for relative, metadata in sorted(entries.items()):
        source_path = source / relative
        mode = int(metadata["mode"])
        _atomic_write(target / relative, source_path.read_bytes(), mode)

    expected_files = set(entries)
    actual_files = []
    actual_directories = []
    for item in target.rglob("*"):
        relative = str(item.relative_to(target))
        if item.is_symlink():
            raise UpdateError("restored shared tree contains a symlink")
        if item.is_file():
            actual_files.append((item, relative))
        elif item.is_dir():
            actual_directories.append(item)
        else:
            raise UpdateError("restored shared tree contains an unsafe entry")
    for item, relative in actual_files:
        if relative not in expected_files:
            item.unlink()
            _fsync_directory(item.parent)
    required_directory_names = {
        str(value) for value in required_directories
    }
    for directory in sorted(
        actual_directories,
        key=lambda value: len(value.relative_to(target).parts),
        reverse=True,
    ):
        if str(directory.relative_to(target)) not in required_directory_names:
            directory.rmdir()
            _fsync_directory(directory.parent)
    _verify_regular_tree(target, entries)


def _verify_regular_tree(root: Path, entries: Mapping[str, Any]) -> None:
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise UpdateError("transaction snapshot contains a symlink")
        if path.is_file():
            actual.add(str(path.relative_to(root)))
        elif not path.is_dir():
            raise UpdateError(
                "transaction tree contains a non-regular entry"
            )
    if actual != set(entries):
        raise UpdateError("transaction snapshot file set changed")
    for relative, metadata in entries.items():
        path = root / relative
        if (
            not _regular_file(path)
            or not isinstance(metadata, Mapping)
            or path.stat().st_size != metadata.get("size")
            or not secrets.compare_digest(
                _sha256_file(path), str(metadata.get("sha256", ""))
            )
        ):
            raise UpdateError(
                "transaction snapshot failed verification: " + relative
            )


class _OwnedSnapshot:
    """Hash-fenced snapshot of the shared bytes an update may replace."""

    DIRECTORY_NAMES = ("config", "artifacts", "state", "bin")

    def __init__(self, layout: Any, root: Path):
        self.layout = layout
        self.root = root
        self.manifest_path = self.root / "snapshot-manifest.json"

    @classmethod
    def capture(cls, layout: Any) -> "_OwnedSnapshot":
        updates = layout.backups / "release-updates"
        _private_directory(updates)
        root = updates / (
            "transaction-"
            + _datetime.datetime.now(_datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%S.%fZ"
            )
            + "-"
            + uuid.uuid4().hex
        )
        root.mkdir(mode=0o700)
        _fsync_directory(updates)
        snapshot = cls(layout, root)
        directory_entries: Dict[str, Any] = {}
        for name in cls.DIRECTORY_NAMES:
            source = layout.support_root / name
            if source.is_symlink() or not source.is_dir():
                raise UpdateError(
                    "installed shared directory is missing/unsafe: " + str(source)
                )
            directory_entries[name] = _copy_regular_tree(
                source, root / "support" / name
            )
        external_entries: Dict[str, Any] = {}
        for name, source in (
            ("codex-config", layout.codex_config),
            ("studio-plugin", layout.plugin_target),
        ):
            if not _regular_file(source):
                raise UpdateError(
                    "installed external file is missing/unsafe: " + str(source)
                )
            data = source.read_bytes()
            mode = stat.S_IMODE(source.stat().st_mode)
            target = root / "external" / name
            _atomic_write(target, data, mode)
            external_entries[name] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": mode,
                "size": len(data),
            }
        manifest = {
            "format": RECEIPT_FORMAT + "-snapshot",
            "schema_version": RECEIPT_VERSION,
            "created_at": _utc_iso(),
            "directories": directory_entries,
            "external": external_entries,
        }
        _atomic_write(snapshot.manifest_path, _json_bytes(manifest))
        snapshot.verify()
        return snapshot

    def verify(self) -> Dict[str, Any]:
        manifest = _load_json(self.manifest_path, "release snapshot manifest")
        if (
            manifest.get("format") != RECEIPT_FORMAT + "-snapshot"
            or manifest.get("schema_version") != RECEIPT_VERSION
        ):
            raise UpdateError("release snapshot identity/schema is invalid")
        directories = manifest.get("directories")
        external = manifest.get("external")
        if (
            not isinstance(directories, Mapping)
            or set(directories) != set(self.DIRECTORY_NAMES)
            or not isinstance(external, Mapping)
            or set(external) != {"codex-config", "studio-plugin"}
        ):
            raise UpdateError("release snapshot scope is invalid")
        for name, entries in directories.items():
            if not isinstance(entries, Mapping):
                raise UpdateError("release snapshot directory metadata is invalid")
            _verify_regular_tree(self.root / "support" / name, entries)
        for name, metadata in external.items():
            path = self.root / "external" / name
            if (
                not _regular_file(path)
                or not isinstance(metadata, Mapping)
                or path.stat().st_size != metadata.get("size")
                or not secrets.compare_digest(
                    _sha256_file(path), str(metadata.get("sha256", ""))
                )
            ):
                raise UpdateError("release snapshot external file changed: " + name)
        return manifest

    def restore(self) -> None:
        manifest = self.verify()
        failed_root = self.root / (
            "failed-current-" + uuid.uuid4().hex
        )
        failed_root.mkdir(mode=0o700)
        _fsync_directory(self.root)
        for name in self.DIRECTORY_NAMES:
            target = self.layout.support_root / name
            if target.exists() and (
                target.is_symlink() or not target.is_dir()
            ):
                raise UpdateError(
                    "cannot restore over an unsafe shared directory: "
                    + str(target)
                )
        for target in (
            self.layout.codex_config,
            self.layout.plugin_target,
        ):
            if target.exists() and not _regular_file(target):
                raise UpdateError(
                    "cannot restore over an unsafe external file: " + str(target)
                )
        for name in self.DIRECTORY_NAMES:
            target = self.layout.support_root / name
            source = self.root / "support" / name
            if name == "bin":
                if target.exists():
                    _copy_regular_tree(
                        target, failed_root / name
                    )
                _restore_regular_tree_in_place(
                    source, target, manifest["directories"][name]
                )
                continue
            restored = target.parent / (
                "." + target.name + ".restore-" + uuid.uuid4().hex
            )
            _copy_regular_tree(source, restored)
            _verify_regular_tree(restored, manifest["directories"][name])
            if target.exists():
                os.replace(target, failed_root / name)
                _fsync_directory(target.parent)
                _fsync_directory(failed_root)
            os.replace(restored, target)
            _fsync_directory(target.parent)
        external_targets = {
            "codex-config": self.layout.codex_config,
            "studio-plugin": self.layout.plugin_target,
        }
        for name, target in external_targets.items():
            metadata = manifest["external"][name]
            _atomic_write(
                target,
                (self.root / "external" / name).read_bytes(),
                int(metadata["mode"]),
            )


@contextlib.contextmanager
def _exclusive_update_lock(layout: Any) -> Iterator[None]:
    run = layout.run
    _private_directory(run)
    lock_path = run / "release-update.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise UpdateError("unable to open the release update lock: " + str(exc))
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another release update/rollback is already running") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _module_from_package(package_root: Path) -> Any:
    install_script = package_root / "install.py"
    if not _regular_file(install_script):
        raise UpdateError("retained release installer is missing")
    name = "_roblox_studio_mcp_v2_release_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, install_script)
    if spec is None or spec.loader is None:
        raise UpdateError("unable to load the verified release installer")
    module = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    sys.path.insert(0, str(package_root))
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise UpdateError("verified release installer failed to load: " + str(exc))
    finally:
        sys.path[:] = previous_path
        sys.modules.pop(name, None)
    return module


def _preverify_candidate_package(
    package_root: Path,
    expected_version: str,
    *,
    expected_manifest_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify a retained package with trusted current code before importing it."""

    candidate = Path(package_root)
    if candidate.is_symlink() or not candidate.is_dir():
        raise UpdateError("verified release package root is missing/unsafe")
    try:
        root = candidate.resolve(strict=True)
        manifest = bootstrap.verify_extracted_package(
            root, expected_version=expected_version
        )
    except (OSError, bootstrap.BootstrapError) as exc:
        raise UpdateError(
            "verified release package failed trusted pre-execution checks: "
            + str(exc)
        ) from exc
    manifest_path = root / "release-manifest.json"
    if expected_manifest_sha256 is not None:
        if (
            _SAFE_SHA256.fullmatch(expected_manifest_sha256) is None
            or not secrets.compare_digest(
                _sha256_file(manifest_path), expected_manifest_sha256
            )
        ):
            raise UpdateError(
                "verified release package manifest does not match ownership state"
            )
    expected_files = {"release-manifest.json"}
    expected_files.update(
        str(item["path"]) for item in manifest["files"]
    )
    actual_files = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise UpdateError(
                "verified release package contains a symlink"
            )
        if item.is_dir():
            continue
        if not _regular_file(item):
            raise UpdateError(
                "verified release package contains an unsafe entry"
            )
        actual_files.add(str(item.relative_to(root)))
    if actual_files != expected_files:
        raise UpdateError(
            "verified release package file set differs from its manifest"
        )
    return manifest


def _candidate_installer(
    current_installer: Any,
    package_root: Path,
    expected_version: str,
) -> Any:
    expected_manifest_sha256: Optional[str] = None
    try:
        current_state = current_installer._load_state(optional=False)
    except Exception:
        current_state = None
    if (
        isinstance(current_state, Mapping)
        and current_state.get("version") == expected_version
        and isinstance(
            current_state.get("release_manifest_sha256"), str
        )
    ):
        expected_manifest_sha256 = current_state[
            "release_manifest_sha256"
        ]
    _preverify_candidate_package(
        package_root,
        expected_version,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    module = _module_from_package(package_root)
    if hasattr(module, "_load_release_updater_module"):
        # Keep candidate/rollback doctor checks on the already verified
        # updater that owns the active transaction nonce.
        module._load_release_updater_module = lambda: sys.modules[__name__]
    if getattr(module, "VERSION", None) != expected_version:
        raise UpdateError("verified installer version does not match its package")
    try:
        module.verify_release_package(package_root)
        layout = module.InstallLayout(
            home=current_installer.layout.home,
            support_root=current_installer.layout.support_root,
            codex_config=current_installer.layout.codex_config,
            studio_plugins=current_installer.layout.studio_plugins,
        )
        return module.Installer(
            package_root,
            layout,
            python_executable=current_installer.python_executable,
        )
    except Exception as exc:
        raise UpdateError("verified release installer rejected its package: " + str(exc))


def _retained_package_version(package_root: Path) -> str:
    manifest = _load_json(
        package_root / "release-manifest.json", "retained release manifest"
    )
    version = _safe_version(manifest.get("version"), "retained release version")
    if (
        manifest.get("format") != bootstrap.PACKAGE_FORMAT
        or manifest.get("product") != bootstrap.PRODUCT
    ):
        raise UpdateError("retained release manifest identity is invalid")
    return version


class ReleaseUpdater:
    """Orchestrate one verified update or one-step retained-version rollback."""

    def __init__(
        self,
        current_installer: Any,
        *,
        downloader: Callable[[str, Path, int], None] = bootstrap.download_file,
        candidate_factory: Callable[[Any, Path, str], Any] = _candidate_installer,
        platform_check: Callable[[], Any] = require_supported_platform,
        runtime_check: Callable[[], Any] = require_supported_runtime,
    ):
        self.current = current_installer
        self.layout = current_installer.layout
        self.downloader = downloader
        self.candidate_factory = candidate_factory
        self.platform_check = platform_check
        self.runtime_check = runtime_check

    def _require_platform(self) -> None:
        try:
            self.runtime_check()
            self.platform_check()
        except UnsupportedPlatformError as exc:
            raise UpdateError(str(exc)) from exc

    def _state_version(self) -> Tuple[Dict[str, Any], str]:
        try:
            state = self.current._load_state(optional=False)
        except Exception as exc:
            raise UpdateError("installed ownership state is invalid: " + str(exc))
        if not isinstance(state, dict):
            raise UpdateError("installed ownership state is missing")
        return state, _safe_version(state.get("version"), "installed version")

    def _receipt_path(self) -> Path:
        return self.layout.state / LATEST_RECEIPT_FILENAME

    def _pending_path(self) -> Path:
        # Deliberately outside the snapshotted state tree. It survives a
        # process death during restore and is cleared only after verification.
        return (
            self.layout.backups
            / "release-updates"
            / PENDING_TRANSACTION_FILENAME
        )

    def _begin_pending_validation(
        self,
        *,
        action: str,
        previous_version: str,
        current_version: str,
        snapshot: _OwnedSnapshot,
    ) -> Dict[str, Any]:
        if action not in {"update", "rollback"}:
            raise UpdateError("release transaction action is invalid")
        previous_version = _safe_version(
            previous_version, "previous installed version"
        )
        current_version = _safe_version(
            current_version, "candidate version"
        )
        path = self._pending_path()
        if path.exists() or path.is_symlink():
            raise UpdateError(
                "a prior release transaction is incomplete; run repair "
                "before another update"
            )
        value = {
            "format": RECEIPT_FORMAT + "-pending",
            "schema_version": RECEIPT_VERSION,
            "action": action,
            "phase": "candidate_doctor",
            "pid": os.getpid(),
            "nonce": secrets.token_hex(16),
            "created_at": _utc_iso(),
            "previous_version": previous_version,
            "current_version": current_version,
            "snapshot": str(snapshot.root),
            "snapshot_manifest_sha256": _sha256_file(
                snapshot.manifest_path
            ),
        }
        _atomic_write(path, _json_bytes(value))
        _ACTIVE_VALIDATION_NONCES.add(value["nonce"])
        return value

    def _clear_pending_validation(self, expected_nonce: str) -> None:
        path = self._pending_path()
        if not _regular_file(path):
            raise UpdateError(
                "release transaction pending record is missing or unsafe"
            )
        value = _load_json(path, "pending release transaction")
        nonce = value.get("nonce")
        if (
            not isinstance(nonce, str)
            or not secrets.compare_digest(nonce, expected_nonce)
        ):
            raise UpdateError(
                "release transaction pending nonce changed during validation"
            )
        raw = path.read_bytes()
        path.unlink()
        try:
            _fsync_directory(path.parent)
        except Exception as exc:
            # Keep recovery possible if metadata durability could not be
            # confirmed. Atomic rewrite either restores a complete marker or
            # fails while leaving the just-written complete file in place.
            try:
                _atomic_write(path, raw)
            except Exception as restore_exc:
                raise UpdateError(
                    "pending marker unlink durability failed and marker "
                    "restoration could not be confirmed: "
                    + str(restore_exc)
                ) from exc
            raise UpdateError(
                "pending marker unlink was not durable; marker was restored"
            ) from exc

    def _validate_interrupted_transaction(
        self,
    ) -> Tuple[Dict[str, Any], _OwnedSnapshot, Dict[str, Any]]:
        """Validate a stale pending record and its exact pre-switch snapshot."""

        pending = _load_json(
            self._pending_path(), "pending release transaction"
        )
        expected_fields = {
            "format",
            "schema_version",
            "action",
            "phase",
            "pid",
            "nonce",
            "created_at",
            "previous_version",
            "current_version",
            "snapshot",
            "snapshot_manifest_sha256",
        }
        if set(pending) != expected_fields:
            raise UpdateError("pending release transaction fields are invalid")
        previous_version = _safe_version(
            pending.get("previous_version"), "pending previous version"
        )
        current_version = _safe_version(
            pending.get("current_version"), "pending candidate version"
        )
        nonce = pending.get("nonce")
        pid = pending.get("pid")
        manifest_digest = pending.get("snapshot_manifest_sha256")
        if (
            pending.get("format") != RECEIPT_FORMAT + "-pending"
            or pending.get("schema_version") != RECEIPT_VERSION
            or pending.get("action") not in {"update", "rollback"}
            or pending.get("phase") != "candidate_doctor"
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(nonce, str)
            or _SAFE_NONCE.fullmatch(nonce) is None
            or not isinstance(pending.get("created_at"), str)
            or not pending["created_at"]
            or previous_version == current_version
            or not isinstance(manifest_digest, str)
            or _SAFE_SHA256.fullmatch(manifest_digest) is None
        ):
            raise UpdateError(
                "pending release transaction identity/fence is invalid"
            )
        snapshot_value = pending.get("snapshot")
        if not isinstance(snapshot_value, str):
            raise UpdateError("pending release snapshot path is invalid")
        claimed_snapshot_root = Path(snapshot_value)
        if (
            not claimed_snapshot_root.is_absolute()
            or claimed_snapshot_root.is_symlink()
        ):
            raise UpdateError("pending release snapshot location is invalid")
        try:
            allowed_root = (
                self.layout.backups / "release-updates"
            ).resolve(strict=True)
            snapshot_root = claimed_snapshot_root.resolve(strict=True)
            relative = snapshot_root.relative_to(allowed_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise UpdateError(
                "pending release snapshot is outside the owned backup root"
            ) from exc
        if (
            len(relative.parts) != 1
            or not relative.name.startswith("transaction-")
            or snapshot_root.is_symlink()
            or not snapshot_root.is_dir()
        ):
            raise UpdateError("pending release snapshot location is invalid")
        snapshot = _OwnedSnapshot(self.layout, snapshot_root)
        snapshot.verify()
        if not secrets.compare_digest(
            _sha256_file(snapshot.manifest_path), manifest_digest
        ):
            raise UpdateError("pending release snapshot manifest hash changed")
        snapshot_state_path = (
            snapshot.root
            / "support"
            / "state"
            / "install-state.json"
        )
        snapshot_state = _load_json(
            snapshot_state_path, "pre-switch install state"
        )
        if (
            snapshot_state.get("format") != _INSTALL_STATE_FORMAT
            or snapshot_state.get("schema_version") != 1
            or snapshot_state.get("product") != bootstrap.PRODUCT
            or snapshot_state.get("version") != previous_version
            or Path(str(snapshot_state.get("support_root", "")))
            != self.layout.support_root
            or not isinstance(
                snapshot_state.get("release_manifest_sha256"), str
            )
            or _SAFE_SHA256.fullmatch(
                snapshot_state["release_manifest_sha256"]
            )
            is None
        ):
            raise UpdateError(
                "pre-switch snapshot ownership/version fence is invalid"
            )
        return pending, snapshot, snapshot_state

    def _verify_pre_switch_release(
        self,
        previous_version: str,
        snapshot_state: Mapping[str, Any],
    ) -> Path:
        """Hash-check the retained runtime before executing its stop command."""

        package_root = self.layout.packages / previous_version
        release_root = self.layout.releases / previous_version
        if (
            package_root.is_symlink()
            or not package_root.is_dir()
            or release_root.is_symlink()
            or not release_root.is_dir()
        ):
            raise UpdateError("pre-switch retained package/release is missing")
        manifest_path = package_root / "release-manifest.json"
        if (
            not _regular_file(manifest_path)
            or not secrets.compare_digest(
                _sha256_file(manifest_path),
                str(snapshot_state["release_manifest_sha256"]),
            )
        ):
            raise UpdateError("pre-switch retained manifest hash changed")
        manifest = _load_json(manifest_path, "pre-switch release manifest")
        if (
            manifest.get("format") != bootstrap.PACKAGE_FORMAT
            or manifest.get("product") != bootstrap.PRODUCT
            or manifest.get("version") != previous_version
        ):
            raise UpdateError("pre-switch retained manifest identity is invalid")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise UpdateError("pre-switch retained manifest files are invalid")
        lifecycle_seen = False
        payload_entries: Dict[str, Mapping[str, Any]] = {}
        for item in files:
            if not isinstance(item, Mapping):
                raise UpdateError(
                    "pre-switch retained manifest entry is invalid"
                )
            relative = item.get("path")
            digest = item.get("sha256")
            size = item.get("size")
            if (
                not isinstance(relative, str)
                or not relative.startswith("payload/")
                or not isinstance(digest, str)
                or _SAFE_SHA256.fullmatch(digest) is None
                or not isinstance(size, int)
                or size < 0
            ):
                if isinstance(relative, str) and not relative.startswith(
                    "payload/"
                ):
                    continue
                raise UpdateError(
                    "pre-switch retained payload metadata is invalid"
                )
            destination_relative = relative[len("payload/") :]
            destination_path = Path(destination_relative)
            if (
                not destination_relative
                or destination_path.is_absolute()
                or ".." in destination_path.parts
                or "." in destination_path.parts
            ):
                raise UpdateError(
                    "pre-switch retained payload path is unsafe"
                )
            destination = release_root / destination_relative
            if destination_relative in payload_entries:
                raise UpdateError(
                    "pre-switch retained manifest contains duplicate payload paths"
                )
            payload_entries[destination_relative] = item
            if (
                not _regular_file(destination)
                or destination.stat().st_size != size
                or not secrets.compare_digest(
                    _sha256_file(destination), digest
                )
            ):
                raise UpdateError(
                    "pre-switch retained release hash mismatch: "
                    + destination_relative
                )
            if destination_relative == "studio_mcp_v2/lifecycle.py":
                lifecycle_seen = True
        if not lifecycle_seen:
            raise UpdateError(
                "pre-switch retained release lacks its lifecycle module"
            )
        try:
            _verify_regular_tree(release_root, payload_entries)
        except UpdateError as exc:
            raise UpdateError(
                "pre-switch retained release exact file-set verification "
                "failed: "
                + str(exc)
            ) from exc
        return release_root

    def _stop_from_pre_switch_snapshot(
        self,
        snapshot: _OwnedSnapshot,
        snapshot_state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        previous_version = str(snapshot_state["version"])
        self._verify_pre_switch_release(previous_version, snapshot_state)
        bootstrap_path = (
            snapshot.root
            / "support"
            / "bin"
            / self.layout.launcher_bootstrap.name
        )
        if not _regular_file(bootstrap_path):
            raise UpdateError("pre-switch lifecycle bootstrap is missing")
        configured_python = Path(str(self.current.python_executable))
        try:
            python_executable = configured_python.resolve(strict=True)
            running_python = Path(sys.executable).resolve(strict=True)
        except (OSError, RuntimeError):
            raise UpdateError("current Python executable is unavailable")
        if (
            not configured_python.is_absolute()
            or python_executable != running_python
            or not python_executable.is_file()
            or not os.access(python_executable, os.X_OK)
        ):
            raise UpdateError("current Python executable is unavailable")
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR"):
            value = os.environ.get(key)
            if value:
                environment[key] = value
        try:
            process = subprocess.run(
                [
                    str(python_executable),
                    "-I",
                    "-B",
                    str(bootstrap_path),
                    "stop",
                    "--json",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(
                "pre-switch lifecycle stop could not run: " + str(exc)
            ) from exc
        if len(process.stdout) > 1_000_000 or len(process.stderr) > 1_000_000:
            raise UpdateError("pre-switch lifecycle stop output was oversized")
        try:
            payload = json.loads(process.stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UpdateError(
                "pre-switch lifecycle stop returned invalid JSON"
            ) from exc
        if (
            process.returncode != 0
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
            or payload.get("running") is not False
            or payload.get("stopped") not in {True, False}
        ):
            raise UpdateError(
                "pre-switch lifecycle stop was refused safely"
            )
        return payload

    def _write_receipt(
        self,
        *,
        action: str,
        previous_version: str,
        current_version: str,
        archive_sha256: Optional[str],
        snapshot: _OwnedSnapshot,
    ) -> Dict[str, Any]:
        receipt = {
            "format": RECEIPT_FORMAT,
            "schema_version": RECEIPT_VERSION,
            "action": action,
            "created_at": _utc_iso(),
            "previous_version": previous_version,
            "current_version": current_version,
            "archive_sha256": archive_sha256,
            "snapshot": str(snapshot.root),
            "previous_package": str(self.layout.packages / previous_version),
            "current_package": str(self.layout.packages / current_version),
        }
        archived = self.layout.state / (
            "release-transaction-"
            + _datetime.datetime.now(_datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%S.%fZ"
            )
            + "-"
            + uuid.uuid4().hex
            + ".json"
        )
        raw = _json_bytes(receipt)
        _atomic_write(archived, raw)
        _atomic_write(self._receipt_path(), raw)
        return {**receipt, "receipt": str(archived)}

    def interrupted_update_status(self) -> Dict[str, Any]:
        path = self._pending_path()
        if not path.exists() and not path.is_symlink():
            return {
                "present": False,
                "active": False,
                "recoverable": False,
                "repair_command": None,
            }
        try:
            pending, snapshot, _snapshot_state = (
                self._validate_interrupted_transaction()
            )
            nonce = pending["nonce"]
            active = (
                nonce in _ACTIVE_VALIDATION_NONCES
                or nonce in _ACTIVE_RECOVERY_NONCES
            )
            return {
                "present": True,
                "active": active,
                "recoverable": not active,
                "action": pending["action"],
                "previous_version": pending["previous_version"],
                "candidate_version": pending["current_version"],
                "snapshot": str(snapshot.root),
                "snapshot_manifest_sha256": pending[
                    "snapshot_manifest_sha256"
                ],
                "pending_sha256": _sha256_file(path),
                "repair_command": None if active else "repair",
            }
        except UpdateError as exc:
            return {
                "present": True,
                "active": False,
                "recoverable": False,
                "error": str(exc),
                "repair_command": None,
            }

    def status(self) -> Dict[str, Any]:
        recovery = self.interrupted_update_status()
        try:
            _state, current_version = self._state_version()
        except UpdateError as exc:
            return {
                "ok": False,
                "installed_version": None,
                "installed_state_error": str(exc),
                "retained_releases": [],
                "latest_transaction": None,
                "pending_validation": None,
                "recovery": recovery,
                "rollback": {
                    "available": False,
                    "target_version": None,
                    "requires_accept_current_version": None,
                },
                "automatic_updates": False,
            }
        retained = []
        if self.layout.packages.is_dir() and not self.layout.packages.is_symlink():
            for path in sorted(self.layout.packages.iterdir()):
                if (
                    path.is_dir()
                    and not path.is_symlink()
                    and _SAFE_VERSION.fullmatch(path.name) is not None
                ):
                    try:
                        version = _retained_package_version(path)
                    except UpdateError:
                        retained.append(
                            {"version": path.name, "valid": False}
                        )
                    else:
                        retained.append(
                            {"version": version, "valid": True}
                        )
        receipt_value: Optional[Dict[str, Any]] = None
        receipt_path = self._receipt_path()
        receipt_ok = True
        if receipt_path.exists() or receipt_path.is_symlink():
            try:
                receipt_value = _load_json(
                    receipt_path, "latest release transaction receipt"
                )
                receipt_ok = (
                    receipt_value.get("format") == RECEIPT_FORMAT
                    and receipt_value.get("schema_version") == RECEIPT_VERSION
                    and receipt_value.get("action") in {"update", "rollback"}
                    and receipt_value.get("current_version")
                    == current_version
                )
            except UpdateError:
                receipt_ok = False
        pending_value: Optional[Dict[str, Any]] = None
        pending_ok = False
        pending_path = self._pending_path()
        pending_present = pending_path.exists() or pending_path.is_symlink()
        if pending_present:
            try:
                (
                    pending_value,
                    _pending_snapshot,
                    _pending_snapshot_state,
                ) = self._validate_interrupted_transaction()
                pending_previous = pending_value.get("previous_version")
                pending_current = pending_value.get("current_version")
                pending_action = pending_value.get("action")
                pending_nonce = pending_value.get("nonce")
                receipt_identity_ok = (
                    receipt_value is None
                    or (
                        isinstance(receipt_value, Mapping)
                        and receipt_value.get("format") == RECEIPT_FORMAT
                        and receipt_value.get("schema_version")
                        == RECEIPT_VERSION
                        and receipt_value.get("action")
                        in {"update", "rollback"}
                    )
                )
                candidate_receipt_version = (
                    pending_current
                    if pending_action == "rollback"
                    else pending_previous
                )
                candidate_receipt_matches = (
                    receipt_identity_ok
                    and (
                        receipt_value is None
                        or receipt_value.get("current_version")
                        == candidate_receipt_version
                    )
                )
                recovery_receipt_matches = (
                    receipt_identity_ok
                    and (
                        receipt_value is None
                        or receipt_value.get("current_version")
                        == pending_previous
                    )
                )
                candidate_validation_ok = (
                    pending_value.get("format")
                    == RECEIPT_FORMAT + "-pending"
                    and pending_value.get("schema_version")
                    == RECEIPT_VERSION
                    and pending_value.get("phase") == "candidate_doctor"
                    and pending_action in {"update", "rollback"}
                    and pending_value.get("pid") == os.getpid()
                    and isinstance(pending_nonce, str)
                    and pending_nonce in _ACTIVE_VALIDATION_NONCES
                    and pending_value.get("current_version")
                    == current_version
                    and isinstance(pending_previous, str)
                    and _SAFE_VERSION.fullmatch(pending_previous) is not None
                    and candidate_receipt_matches
                )
                recovery_validation_ok = (
                    isinstance(pending_nonce, str)
                    and pending_nonce in _ACTIVE_RECOVERY_NONCES
                    and pending_previous == current_version
                    and recovery_receipt_matches
                )
                pending_ok = candidate_validation_ok or recovery_validation_ok
            except UpdateError:
                pending_ok = False
        # Any on-disk pending record must be proven live by this exact
        # in-memory transaction nonce. A stale/crashed record never inherits
        # health from an otherwise valid older receipt.
        effective_ok = pending_ok if pending_present else receipt_ok
        rollback_target = (
            receipt_value.get("previous_version")
            if receipt_ok
            and not pending_present
            and isinstance(receipt_value, Mapping)
            else None
        )
        return {
            "ok": effective_ok,
            "installed_version": current_version,
            "retained_releases": retained,
            "latest_transaction": receipt_value,
            "pending_validation": pending_value,
            "recovery": recovery,
            "rollback": {
                "available": isinstance(rollback_target, str),
                "target_version": rollback_target,
                "requires_accept_current_version": current_version,
            },
            "automatic_updates": False,
        }

    def authorizes_cross_version_install(
        self, candidate_version: str
    ) -> bool:
        """Permit only the exact live update transaction's candidate install."""

        try:
            expected_candidate = _safe_version(
                candidate_version, "candidate install version"
            )
            pending, _snapshot, _snapshot_state = (
                self._validate_interrupted_transaction()
            )
            _state, installed_version = self._state_version()
        except UpdateError:
            return False
        nonce = pending.get("nonce")
        return bool(
            pending.get("action") == "update"
            and pending.get("phase") == "candidate_doctor"
            and pending.get("pid") == os.getpid()
            and isinstance(nonce, str)
            and nonce in _ACTIVE_VALIDATION_NONCES
            and pending.get("previous_version") == installed_version
            and pending.get("current_version") == expected_candidate
            and installed_version != expected_candidate
        )

    def recover_interrupted_update(
        self,
        *,
        accept_pending_sha256: Optional[str] = None,
        accept_candidate_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Repair one crash-interrupted release switch back to its snapshot."""

        self._require_platform()
        path = self._pending_path()
        if not path.exists() and not path.is_symlink():
            return {
                "ok": True,
                "changed": False,
                "recovered": False,
                "reason": "no interrupted release transaction",
            }
        with _exclusive_update_lock(self.layout):
            pending, snapshot, snapshot_state = (
                self._validate_interrupted_transaction()
            )
            pending_sha256 = _sha256_file(self._pending_path())
            if accept_pending_sha256 is not None:
                accepted_sha = accept_pending_sha256.lower()
                if (
                    _SAFE_SHA256.fullmatch(accepted_sha) is None
                    or not secrets.compare_digest(
                        accepted_sha, pending_sha256
                    )
                ):
                    raise UpdateError(
                        "pending transaction SHA-256 acknowledgement does not match"
                    )
            if (
                accept_candidate_version is not None
                and not secrets.compare_digest(
                    accept_candidate_version,
                    str(pending["current_version"]),
                )
            ):
                raise UpdateError(
                    "candidate version acknowledgement does not match"
                )
            if pending["nonce"] in _ACTIVE_VALIDATION_NONCES:
                raise UpdateError(
                    "release transaction is still active; recovery was refused"
                )
            stop = self._stop_from_pre_switch_snapshot(
                snapshot, snapshot_state
            )
            snapshot.restore()
            _restored_state, restored_version = self._state_version()
            if restored_version != pending["previous_version"]:
                raise UpdateError(
                    "recovered install state does not match pre-switch version"
                )
            nonce = str(pending["nonce"])
            _ACTIVE_RECOVERY_NONCES.add(nonce)
            try:
                package_root = self.layout.packages / restored_version
                prior_installer = self.candidate_factory(
                    self.current, package_root, restored_version
                )
                doctor = prior_installer.doctor()
                if (
                    not isinstance(doctor, Mapping)
                    or doctor.get("ok") is not True
                ):
                    raise UpdateError(
                        "restored pre-switch release failed real doctor checks"
                    )
                # The marker is intentionally cleared last. A crash during
                # stop, restore, or doctor remains safely resumable.
                self._clear_pending_validation(nonce)
            finally:
                _ACTIVE_RECOVERY_NONCES.discard(nonce)
            return {
                "ok": True,
                "changed": True,
                "recovered": True,
                "action": "repair-interrupted-release-transaction",
                "interrupted_action": pending["action"],
                "discarded_candidate_version": pending["current_version"],
                "version": restored_version,
                "snapshot": str(snapshot.root),
                "pending_sha256": pending_sha256,
                "stop": stop,
                "doctor": dict(doctor),
                "restart_required": (
                    "Restart Codex and reload/restart open Roblox Studio "
                    "windows; the interrupted release transition was aborted "
                    "back to its exact pre-switch version."
                ),
            }

    @staticmethod
    def _apply_and_validate(candidate: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        install_result = candidate.install()
        if not isinstance(install_result, Mapping) or install_result.get("ok") is not True:
            raise UpdateError("candidate installer did not acknowledge success")
        doctor = candidate.doctor()
        if not isinstance(doctor, Mapping) or doctor.get("ok") is not True:
            raise UpdateError(
                "candidate failed installed-path doctor checks: "
                + json.dumps(doctor, sort_keys=True, default=str)
            )
        return dict(install_result), dict(doctor)

    def _transactional_switch(
        self,
        *,
        candidate: Any,
        previous_version: str,
        current_version: str,
        action: str,
        archive_sha256: Optional[str],
    ) -> Dict[str, Any]:
        snapshot = _OwnedSnapshot.capture(self.layout)
        candidate_started = False
        pending_nonce: Optional[str] = None
        try:
            pending = self._begin_pending_validation(
                action=action,
                previous_version=previous_version,
                current_version=current_version,
                snapshot=snapshot,
            )
            pending_nonce = str(pending["nonce"])
            candidate_started = True
            install_result, doctor = self._apply_and_validate(candidate)
            receipt = self._write_receipt(
                action=action,
                previous_version=previous_version,
                current_version=current_version,
                archive_sha256=archive_sha256,
                snapshot=snapshot,
            )
            self._clear_pending_validation(pending_nonce)
        except Exception as exc:
            if candidate_started:
                try:
                    snapshot.restore()
                    if pending_nonce is None:
                        raise UpdateError(
                            "release switch lost its pending recovery nonce"
                        )
                    _ACTIVE_RECOVERY_NONCES.add(pending_nonce)
                    try:
                        prior_package = (
                            self.layout.packages / previous_version
                        )
                        prior_installer = self.candidate_factory(
                            self.current,
                            prior_package,
                            previous_version,
                        )
                        prior_doctor = prior_installer.doctor()
                        if (
                            not isinstance(prior_doctor, Mapping)
                            or prior_doctor.get("ok") is not True
                        ):
                            raise UpdateError(
                                "restored pre-switch release failed real "
                                "doctor checks"
                            )
                        self._clear_pending_validation(pending_nonce)
                    finally:
                        _ACTIVE_RECOVERY_NONCES.discard(pending_nonce)
                except Exception as restoration_exc:
                    raise UpdateError(
                        "release switch failed and the exact v2-owned snapshot "
                        "could not be restored and verified; run repair: "
                        + str(restoration_exc)
                    ) from exc
            raise UpdateError(
                (
                    "release switch failed; prior v2-owned bytes were restored: "
                    if candidate_started
                    else "release switch was refused before candidate mutation: "
                )
                + str(exc)
            ) from exc
        finally:
            if pending_nonce is not None:
                _ACTIVE_VALIDATION_NONCES.discard(pending_nonce)
        return {
            "ok": True,
            "changed": True,
            "action": action,
            "previous_version": previous_version,
            "version": current_version,
            "archive_sha256": archive_sha256,
            "install": install_result,
            "doctor": doctor,
            "transaction": receipt,
            "restart_required": (
                "Restart Codex to refresh MCP tools and reload/restart open "
                "Roblox Studio windows to load the switched plugin."
            ),
        }

    def _transactional_snapshot_rollback(
        self,
        *,
        candidate: Any,
        previous_version: str,
        current_version: str,
        prior_snapshot: _OwnedSnapshot,
    ) -> Dict[str, Any]:
        """Restore the exact pre-update bytes, not a best-effort reinstall."""

        current_snapshot = _OwnedSnapshot.capture(self.layout)
        restore_started = False
        pending_nonce: Optional[str] = None
        try:
            pending = self._begin_pending_validation(
                action="rollback",
                previous_version=previous_version,
                current_version=current_version,
                snapshot=current_snapshot,
            )
            pending_nonce = str(pending["nonce"])
            try:
                stop = self.current._safe_stop_lifecycle()
            except Exception as exc:
                raise UpdateError(
                    "current v2 lifecycle refused the rollback stop: " + str(exc)
                ) from exc
            if (
                not isinstance(stop, Mapping)
                or stop.get("running") is not False
                or stop.get("stopped") not in {True, False}
            ):
                raise UpdateError(
                    "current v2 lifecycle returned an invalid stop acknowledgement"
                )
            restore_started = True
            prior_snapshot.restore()
            _restored_state, restored_version = self._state_version()
            if restored_version != current_version:
                raise UpdateError(
                    "restored snapshot does not contain the rollback target version"
                )
            doctor = candidate.doctor()
            if not isinstance(doctor, Mapping) or doctor.get("ok") is not True:
                raise UpdateError(
                    "restored release failed installed-path doctor checks: "
                    + json.dumps(doctor, sort_keys=True, default=str)
                )
            receipt = self._write_receipt(
                action="rollback",
                previous_version=previous_version,
                current_version=current_version,
                archive_sha256=None,
                snapshot=current_snapshot,
            )
            self._clear_pending_validation(pending_nonce)
        except Exception as exc:
            if restore_started:
                try:
                    current_snapshot.restore()
                    if pending_nonce is None:
                        raise UpdateError(
                            "release rollback lost its pending recovery nonce"
                        )
                    _ACTIVE_RECOVERY_NONCES.add(pending_nonce)
                    try:
                        current_package = (
                            self.layout.packages / previous_version
                        )
                        current_installer = self.candidate_factory(
                            self.current,
                            current_package,
                            previous_version,
                        )
                        current_doctor = current_installer.doctor()
                        if (
                            not isinstance(current_doctor, Mapping)
                            or current_doctor.get("ok") is not True
                        ):
                            raise UpdateError(
                                "restored current release failed real "
                                "doctor checks"
                            )
                        self._clear_pending_validation(pending_nonce)
                    finally:
                        _ACTIVE_RECOVERY_NONCES.discard(pending_nonce)
                except Exception as restoration_exc:
                    raise UpdateError(
                        "release rollback failed and the exact current-version "
                        "snapshot could not be restored and verified; run "
                        "repair: "
                        + str(restoration_exc)
                    ) from exc
                raise UpdateError(
                    "release rollback failed; current-version bytes were "
                    "restored: "
                    + str(exc)
                ) from exc
            if pending_nonce is not None:
                try:
                    self._clear_pending_validation(pending_nonce)
                except Exception as cleanup_exc:
                    raise UpdateError(
                        "release rollback was refused before restore but its "
                        "durable marker could not be cleared; run repair: "
                        + str(cleanup_exc)
                    ) from exc
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("release rollback was refused: " + str(exc)) from exc
        finally:
            if pending_nonce is not None:
                _ACTIVE_VALIDATION_NONCES.discard(pending_nonce)
        return {
            "ok": True,
            "changed": True,
            "action": "rollback",
            "previous_version": previous_version,
            "version": current_version,
            "archive_sha256": None,
            "doctor": dict(doctor),
            "transaction": receipt,
            "restart_required": (
                "Restart Codex to refresh MCP tools and reload/restart open "
                "Roblox Studio windows to load the restored plugin."
            ),
        }

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
        self._require_platform()
        candidate_version = bootstrap.version_from_tag(tag)
        _state, previous_version = self._state_version()
        if candidate_version == previous_version:
            raise UpdateError(
                "selected release is already installed; use repair instead"
            )
        with _exclusive_update_lock(self.layout):
            _locked_state, locked_previous = self._state_version()
            if locked_previous != previous_version:
                raise UpdateError(
                    "installed version changed while waiting for the update lock"
                )
            with tempfile.TemporaryDirectory(
                prefix="roblox-studio-mcp-v2-update-"
            ) as temporary:
                try:
                    package_root, digest, _manifest = bootstrap.acquire_release(
                        Path(temporary) / "release",
                        tag=tag,
                        owner=owner,
                        repo=repo,
                        archive=archive,
                        checksum_file=checksum_file,
                        expected_sha256=expected_sha256,
                        downloader=self.downloader,
                    )
                except bootstrap.BootstrapError as exc:
                    raise UpdateError(str(exc)) from exc
                candidate = self.candidate_factory(
                    self.current, package_root, candidate_version
                )
                return self._transactional_switch(
                    candidate=candidate,
                    previous_version=previous_version,
                    current_version=candidate_version,
                    action="update",
                    archive_sha256=digest,
                )

    def rollback(
        self,
        *,
        to_version: str,
        accept_current_version: str,
    ) -> Dict[str, Any]:
        self._require_platform()
        target_version = _safe_version(to_version, "rollback target version")
        accepted = _safe_version(
            accept_current_version, "accepted current version"
        )
        _state, current_version = self._state_version()
        if not secrets.compare_digest(current_version, accepted):
            raise UpdateError(
                "installed version changed; --accept-current-version does not match"
            )
        with _exclusive_update_lock(self.layout):
            _locked_state, locked_current = self._state_version()
            if locked_current != current_version:
                raise UpdateError(
                    "installed version changed while waiting for the rollback lock"
                )
            receipt = _load_json(
                self._receipt_path(), "latest release transaction receipt"
            )
            if (
                receipt.get("format") != RECEIPT_FORMAT
                or receipt.get("schema_version") != RECEIPT_VERSION
                or receipt.get("action") not in {"update", "rollback"}
                or receipt.get("current_version") != current_version
                or receipt.get("previous_version") != target_version
            ):
                raise UpdateError(
                    "latest release receipt does not authorize that one-step rollback"
                )
            snapshot_value = receipt.get("snapshot")
            if not isinstance(snapshot_value, str):
                raise UpdateError("latest release receipt lacks its exact snapshot")
            try:
                snapshot_root = Path(snapshot_value).resolve(strict=True)
                allowed_root = (
                    self.layout.backups / "release-updates"
                ).resolve(strict=True)
                snapshot_root.relative_to(allowed_root)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise UpdateError(
                    "latest release snapshot is outside the owned backup root"
                ) from exc
            prior_snapshot = _OwnedSnapshot(self.layout, snapshot_root)
            prior_snapshot.verify()
            package_root = self.layout.packages / target_version
            if package_root.is_symlink() or not package_root.is_dir():
                raise UpdateError("retained rollback package is missing/unsafe")
            if _retained_package_version(package_root) != target_version:
                raise UpdateError("retained rollback package version is invalid")
            if self.candidate_factory is _candidate_installer:
                target_state = _load_json(
                    prior_snapshot.root
                    / "support"
                    / "state"
                    / "install-state.json",
                    "retained rollback snapshot install state",
                )
                target_manifest_sha256 = target_state.get(
                    "release_manifest_sha256"
                )
                if (
                    target_state.get("version") != target_version
                    or Path(str(target_state.get("support_root", "")))
                    != self.layout.support_root
                    or not isinstance(target_manifest_sha256, str)
                    or _SAFE_SHA256.fullmatch(
                        target_manifest_sha256
                    )
                    is None
                ):
                    raise UpdateError(
                        "retained rollback snapshot ownership state is invalid"
                    )
                _preverify_candidate_package(
                    package_root,
                    target_version,
                    expected_manifest_sha256=target_manifest_sha256,
                )
            candidate = self.candidate_factory(
                self.current, package_root, target_version
            )
            return self._transactional_snapshot_rollback(
                candidate=candidate,
                previous_version=current_version,
                current_version=target_version,
                prior_snapshot=prior_snapshot,
            )
