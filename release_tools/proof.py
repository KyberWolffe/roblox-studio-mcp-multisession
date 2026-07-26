"""Isolated, non-network release installation proof.

This module validates the exact portable archive and exercises its installer
inside a temporary synthetic home.  Broker lifecycle calls are replaced with
bounded acknowledgements so the proof cannot bind a port or affect a running
Studio/Codex installation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tarfile
import tempfile
import types
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional, Tuple

from .audit import AuditReport, audit_archive


class ProofError(RuntimeError):
    """The isolated release proof failed closed."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        details = path.lstat()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(details.st_mode)).encode("ascii"))
        digest.update(b"\0")
        if path.is_symlink():
            raise ProofError("isolated installed tree unexpectedly contains a symlink")
        if path.is_file():
            digest.update(path.read_bytes())
        elif not path.is_dir():
            raise ProofError(
                "isolated installed tree contains a non-regular filesystem entry"
            )
        digest.update(b"\0")
    return digest.hexdigest()


def _checksum_from_file(path: Path, expected_name: str) -> str:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProofError("unable to read SHA-256 checksum file") from exc
    matches = []
    for line in lines:
        pieces = line.strip().split()
        if len(pieces) == 2 and pieces[1].lstrip("*") == expected_name:
            digest = pieces[0].lower()
            if len(digest) == 64 and all(
                character in "0123456789abcdef" for character in digest
            ):
                matches.append(digest)
    if len(matches) != 1:
        raise ProofError(
            "checksum file must contain exactly one valid entry for the archive"
        )
    return matches[0]


def _extract_audited_archive(archive: Path, destination: Path) -> Path:
    """Extract regular audited members without tarfile.extractall."""

    roots = set()
    created = set()
    with tarfile.open(archive, "r:gz") as package:
        for member in package.getmembers():
            path = PurePosixPath(member.name)
            if (
                not member.isfile()
                or path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ProofError("archive changed after audit or contains unsafe entries")
            roots.add(path.parts[0])
            target = destination.joinpath(*path.parts)
            resolved_parent = target.parent.resolve()
            try:
                resolved_parent.relative_to(destination.resolve())
            except ValueError as exc:
                raise ProofError("archive target escapes the proof directory") from exc
            if target in created or target.exists() or target.is_symlink():
                raise ProofError("archive extraction target is duplicated")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = package.extractfile(member)
            if source is None:
                raise ProofError("archive member cannot be read")
            data = source.read()
            if len(data) != member.size:
                raise ProofError("archive member size changed during extraction")
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                member.mode,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(data)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.chmod(target, member.mode)
            created.add(target)
    if len(roots) != 1:
        raise ProofError("audited archive does not have one package root")
    package_root = destination / next(iter(roots))
    if not package_root.is_dir():
        raise ProofError("extracted package root is missing")
    return package_root


def _load_installer(package_root: Path) -> types.ModuleType:
    path = package_root / "install.py"
    if not path.is_file() or path.is_symlink():
        raise ProofError("portable package is missing install.py")
    module_name = "_isolated_release_installer_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ProofError("unable to load the portable installer")
    module = importlib.util.module_from_spec(spec)
    old_path = list(sys.path)
    portable_module_names = ("bootstrap", "platform_support", "release_updater")
    prior_modules = {
        name: sys.modules.get(name) for name in portable_module_names
    }
    sys.modules[module_name] = module
    try:
        # Running install.py normally places its package root on sys.path. The
        # proof mirrors that behavior for root-level platform/update helpers.
        sys.path.insert(0, str(package_root))
        spec.loader.exec_module(module)
        updater_loader = getattr(module, "_load_release_updater_module", None)
        if callable(updater_loader):
            # Preload the archive-owned updater while the archive root is on
            # sys.path, then pin the installer to that verified module object.
            updater_module = updater_loader()
            module._load_release_updater_module = lambda: updater_module
    except Exception as exc:
        raise ProofError("portable installer could not be loaded: " + str(exc)) from exc
    finally:
        sys.path[:] = old_path
        sys.modules.pop(module_name, None)
        for name, prior in prior_modules.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior
    return module


def _safe_installer(
    module: types.ModuleType,
    package_root: Path,
    layout: Any,
) -> Tuple[Any, Dict[str, int]]:
    counters = {"lifecycle_calls": 0, "stop_calls": 0}
    base = getattr(module, "Installer", None)
    if not isinstance(base, type):
        raise ProofError("portable installer lacks the Installer API")

    class ProofInstaller(base):  # type: ignore[misc, valid-type]
        def _invoke_lifecycle(
            self,
            command: str,
            *,
            require_ok: bool = True,
            timeout: int = 20,
            allow_ephemeral_repair_launcher: bool = False,
        ) -> Dict[str, Any]:
            del require_ok, timeout, allow_ephemeral_repair_launcher
            counters["lifecycle_calls"] += 1
            if command == "stop":
                counters["stop_calls"] += 1
                return {"ok": True, "running": False, "stopped": False}
            if command in {"doctor", "status"}:
                return {
                    "ok": True,
                    "lifecycle": {"condition": "stopped"},
                    "catalog": {"installed_v1_cache": None},
                }
            if command == "start":
                return {
                    "ok": True,
                    "broker": {"catalog_sha256": "0" * 64},
                }
            raise ProofError("proof refused an unexpected lifecycle command")

    return (
        ProofInstaller(
            package_root,
            layout,
            python_executable=sys.executable,
        ),
        counters,
    )


def _assert_doctor_healthy(report: Mapping[str, Any], phase: str) -> None:
    if report.get("ok") is not True:
        failed = [
            str(item.get("name"))
            for item in report.get("checks", [])
            if isinstance(item, Mapping) and item.get("ok") is not True
        ]
        raise ProofError(
            phase + " doctor failed" + (": " + ", ".join(failed) if failed else "")
        )


def _prove_offline_update_rollback(
    module: types.ModuleType,
    manager: Any,
    layout: Any,
    *,
    archive: Path,
    checksum_file: Optional[Path],
    archive_sha256: str,
) -> Dict[str, Any]:
    """Exercise verified acquisition and transactional switching in isolation."""

    updater_module = module._load_release_updater_module()
    updater_type = getattr(updater_module, "ReleaseUpdater", None)
    if not isinstance(updater_type, type):
        raise ProofError("portable package lacks the ReleaseUpdater API")
    current_version = str(getattr(module, "VERSION", ""))
    previous_version = "0.0.0-proof"
    if not current_version or current_version == previous_version:
        raise ProofError("portable installer version is unavailable for update proof")

    baseline_state = json.loads(
        layout.install_state.read_text(encoding="utf-8")
    )
    baseline_config = layout.codex_config.read_bytes()
    baseline_plugin = layout.plugin_target.read_bytes()
    prior_state = dict(baseline_state)
    prior_state["version"] = previous_version
    prior_state_bytes = (
        json.dumps(
            prior_state,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    layout.install_state.write_bytes(prior_state_bytes)
    os.chmod(layout.install_state, 0o600)

    retained_prior = layout.packages / previous_version
    retained_prior.mkdir(mode=0o700)
    retained_manifest = {
        "format": "roblox-studio-mcp-v2-portable-release",
        "product": "RobloxStudioMCPv2",
        "version": previous_version,
    }
    (retained_prior / "release-manifest.json").write_text(
        json.dumps(retained_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    class SyntheticCandidate:
        def __init__(self, version: str):
            self.version = version

        def install(self) -> Dict[str, Any]:
            if self.version == current_version:
                state = dict(prior_state)
                state["version"] = current_version
                layout.install_state.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                layout.codex_config.write_bytes(
                    baseline_config + b"# synthetic verified update marker\n"
                )
                layout.plugin_target.write_bytes(
                    b"synthetic updated plugin marker\n"
                )
            elif self.version == previous_version:
                layout.install_state.write_bytes(prior_state_bytes)
                layout.codex_config.write_bytes(baseline_config)
                layout.plugin_target.write_bytes(baseline_plugin)
            else:
                raise ProofError("update proof received an unexpected version")
            os.chmod(layout.install_state, 0o600)
            os.chmod(layout.plugin_target, 0o600)
            return {"ok": True, "version": self.version}

        def doctor(self) -> Dict[str, Any]:
            return {"ok": True, "version": self.version}

    requested_versions = []

    def candidate_factory(
        current_installer: Any,
        package_root: Path,
        version: str,
    ) -> SyntheticCandidate:
        del current_installer
        if not package_root.is_dir() or package_root.is_symlink():
            raise ProofError("updater candidate package is missing or unsafe")
        requested_versions.append(version)
        return SyntheticCandidate(version)

    release_updater = updater_type(
        manager,
        candidate_factory=candidate_factory,
        platform_check=lambda: None,
        runtime_check=lambda: None,
    )
    if checksum_file is None:
        # prove_release has already verified the explicit hash. Generate a
        # local two-column checksum solely for bootstrap.acquire_release.
        checksum_path = layout.run / "synthetic-update.sha256"
        checksum_path.write_text(
            archive_sha256 + "  " + archive.name + "\n",
            encoding="ascii",
        )
    else:
        checksum_path = checksum_file
    updated = release_updater.update(
        tag="v" + current_version,
        archive=archive,
        checksum_file=checksum_path,
        expected_sha256=archive_sha256,
    )
    if (
        updated.get("ok") is not True
        or updated.get("previous_version") != previous_version
        or updated.get("version") != current_version
    ):
        raise ProofError("offline release update did not produce the expected receipt")
    state_after_update = json.loads(
        layout.install_state.read_text(encoding="utf-8")
    )
    if state_after_update.get("version") != current_version:
        raise ProofError("offline update did not switch the installed version")
    updated_status = release_updater.status()
    if (
        updated_status.get("ok") is not True
        or updated_status.get("rollback", {}).get("target_version")
        != previous_version
    ):
        raise ProofError("updated release status did not expose one-step rollback")

    rolled_back = release_updater.rollback(
        to_version=previous_version,
        accept_current_version=current_version,
    )
    if (
        rolled_back.get("ok") is not True
        or rolled_back.get("previous_version") != current_version
        or rolled_back.get("version") != previous_version
    ):
        raise ProofError("one-step release rollback did not succeed")
    state_after_rollback = json.loads(
        layout.install_state.read_text(encoding="utf-8")
    )
    if state_after_rollback.get("version") != previous_version:
        raise ProofError("rollback did not restore the accepted prior version")
    if (
        layout.codex_config.read_bytes() != baseline_config
        or layout.plugin_target.read_bytes() != baseline_plugin
    ):
        raise ProofError("rollback did not restore shared external bytes")
    if requested_versions != [current_version, previous_version]:
        raise ProofError("update proof did not select the exact verified versions")
    return {
        "tested": True,
        "offline_archive_verified": True,
        "update_receipted": True,
        "rollback_receipted": True,
        "shared_bytes_restored": True,
    }


def prove_release(
    archive: Path,
    *,
    checksum_file: Optional[Path] = None,
    expected_sha256: Optional[str] = None,
    temporary_parent: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the full isolated install/repair/uninstall release proof."""

    source = Path(archive).resolve(strict=True)
    digest = _sha256_file(source)
    accepted = expected_sha256
    if checksum_file is not None:
        checksum_value = _checksum_from_file(
            Path(checksum_file).resolve(strict=True), source.name
        )
        if accepted is not None and accepted.lower() != checksum_value:
            raise ProofError("explicit and file SHA-256 values disagree")
        accepted = checksum_value
    if accepted is None:
        raise ProofError("an expected SHA-256 or checksum file is required")
    if accepted.lower() != digest:
        raise ProofError("release archive SHA-256 verification failed")

    archive_report: AuditReport = audit_archive(source)
    if not archive_report.ok:
        codes = sorted({item.code for item in archive_report.findings})
        raise ProofError("release archive audit failed: " + ", ".join(codes))

    parent = None
    if temporary_parent is not None:
        parent = Path(temporary_parent).resolve(strict=True)
        if not parent.is_dir():
            raise ProofError("temporary parent is not a directory")

    phases: Dict[str, Any] = {
        "checksum_verified": True,
        "archive_audit": True,
    }
    with tempfile.TemporaryDirectory(
        prefix="studio-mcp-v2-release-proof-",
        dir=None if parent is None else str(parent),
    ) as temporary:
        root = Path(temporary)
        package_root = _extract_audited_archive(source, root / "package")
        module = _load_installer(package_root)
        layout_type = getattr(module, "InstallLayout", None)
        if layout_type is None or not callable(
            getattr(layout_type, "for_user", None)
        ):
            raise ProofError("portable installer lacks the InstallLayout API")

        home = root / "synthetic-home"
        codex = home / ".codex"
        plugins = home / "Documents" / "Roblox" / "Plugins"
        codex.mkdir(mode=0o755, parents=True)
        plugins.mkdir(mode=0o755, parents=True)
        initial_config = (
            b"# synthetic pre-existing user configuration\n"
            b"[mcp_servers.Roblox_Studio]\n"
            b'command = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"\n'
            b"args = []\n"
        )
        config_path = codex / "config.toml"
        config_path.write_bytes(initial_config)
        v1_plugin = plugins / "MCPStudioPlugin.rbxm"
        v1_plugin.write_bytes(b"synthetic v1 fallback sentinel\n")
        v1_hash = _sha256_file(v1_plugin)

        layout = layout_type.for_user(home=home)
        manager, counters = _safe_installer(module, package_root, layout)
        installed = manager.install()
        if installed.get("ok") is not True or installed.get("changed") is not True:
            raise ProofError("first isolated install did not make the expected change")
        if config_path.read_bytes()[: len(initial_config)] != initial_config:
            raise ProofError("install changed pre-existing Codex configuration bytes")
        if _sha256_file(v1_plugin) != v1_hash:
            raise ProofError("install changed the v1 fallback plugin")
        secrets_value = json.loads(layout.secrets_config.read_text(encoding="utf-8"))
        if (
            secrets_value.get("client_token") == secrets_value.get("studio_token")
            or not secrets_value.get("client_token")
            or not secrets_value.get("studio_token")
        ):
            raise ProofError("install did not generate distinct machine credentials")
        if (
            secrets_value["client_token"].encode() in config_path.read_bytes()
            or secrets_value["studio_token"].encode() in config_path.read_bytes()
        ):
            raise ProofError("Codex configuration contains a generated credential")
        phases["install"] = True

        doctor = manager.doctor()
        _assert_doctor_healthy(doctor, "post-install")
        phases["doctor"] = True
        status = manager.doctor()
        _assert_doctor_healthy(status, "status")
        phases["status"] = True

        before_noop = _tree_digest(home)
        no_op = manager.install(repair=True)
        if no_op.get("changed") is not False:
            raise ProofError("no-op repair unexpectedly changed installed state")
        if _tree_digest(home) != before_noop:
            raise ProofError("no-op repair changed isolated home bytes or modes")
        phases["no_op_repair"] = True

        layout.launcher.unlink()
        repaired_missing = manager.install(repair=True)
        if (
            repaired_missing.get("changed") is not True
            or not layout.launcher.is_file()
        ):
            raise ProofError("repair did not restore a missing owned launcher")
        _assert_doctor_healthy(manager.doctor(), "missing-component repair")
        phases["missing_component_repair"] = True

        expected_plugin = layout.plugin_artifact.read_bytes()
        layout.plugin_target.write_bytes(b"synthetic corrupt plugin\n")
        repaired_corrupt = manager.install(repair=True)
        if (
            repaired_corrupt.get("changed") is not True
            or layout.plugin_target.read_bytes() != expected_plugin
        ):
            raise ProofError("repair did not restore a corrupt owned plugin")
        if not any((layout.backups / "plugin").iterdir()):
            raise ProofError("corrupt component repair did not retain a backup")
        _assert_doctor_healthy(manager.doctor(), "corrupt-component repair")
        phases["corrupt_component_repair"] = True

        update_rollback = _prove_offline_update_rollback(
            module,
            manager,
            layout,
            archive=source,
            checksum_file=(
                None
                if checksum_file is None
                else Path(checksum_file).resolve(strict=True)
            ),
            archive_sha256=digest,
        )
        phases["offline_update_and_rollback"] = True

        uninstalled = manager.uninstall()
        if uninstalled.get("ok") is not True:
            raise ProofError("isolated uninstall did not succeed")
        if config_path.read_bytes() != initial_config:
            raise ProofError("uninstall did not restore exact Codex configuration")
        if _sha256_file(v1_plugin) != v1_hash:
            raise ProofError("uninstall changed the v1 fallback plugin")
        if layout.plugin_target.exists() or layout.support_root.exists():
            raise ProofError("uninstall left an owned active path in place")
        recovery = Path(str(uninstalled.get("support_recovery", "")))
        try:
            recovery.resolve(strict=True).relative_to(root.resolve())
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ProofError("uninstall recovery is missing or outside the proof root") from exc
        phases["uninstall_and_config_restore"] = True

        version = str(installed.get("version"))
        lifecycle_calls = counters["lifecycle_calls"]
        stop_calls = counters["stop_calls"]

    return {
        "ok": True,
        "archive": source.name,
        "archive_sha256": digest,
        "version": version,
        "phases": phases,
        "isolation": {
            "temporary_home_removed": True,
            "network_or_broker_started": False,
            "live_codex_config_touched": False,
            "live_studio_plugins_touched": False,
            "simulated_lifecycle_calls": lifecycle_calls,
            "simulated_stop_calls": stop_calls,
        },
        "update_rollback": update_rollback,
    }
