"""Exact isolated Multisession rc.5 -> rc.7 -> rc.5 package proof.

This proof is deliberately separate from the immutable rc.4 migration proof.
It executes both real portable installers in a disposable synthetic home while
replacing lifecycle subprocesses with bounded stopped acknowledgements.  It
does not start a broker, contact the network, inspect Studio, or resolve any
live installation path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .cross_version_proof import (
    _active_fingerprint,
    _archive_manifest,
    _bounded_subprocesses,
    _candidate_factory,
    _extract_audited_archive,
    _file_fingerprint,
    _load_installer,
    _proof_installer,
    _sha256_file,
)
from .proof import ProofError, _assert_doctor_healthy


PRIOR_VERSION = "0.4.0-rc.5"
CANDIDATE_VERSION = "0.4.0-rc.7"
PRIOR_ARCHIVE_SHA256 = (
    "d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d"
)
PRIOR_MANIFEST_SHA256 = (
    "3deb48919dc549c2695dd14621579a6f02ac05b301b697865aba1393a53372ef"
)
PRIOR_INSTALLER_SHA256 = (
    "93ed2e076e92faa7863ddb975b41b1f6954890d0eb6180f868647c35d1ac28b0"
)
PRIOR_UPDATER_SHA256 = (
    "ecc8ec2db2ffda1f4d1c64ddc35db7b8f2735878bdbfa52de2df0bc4aa756fbe"
)
PRIOR_BOOTSTRAP_SHA256 = (
    "e4f35d878024a3c73d6276bc512236e1cad8637c98894da976b233d556cd346b"
)
PRIOR_SOURCE_COMMIT = "923422254e95050f0fe66bacc0114e9ace2789c5"
PRIOR_SOURCE_TREE = "3e3713045821412b6a6bbe0a4db9e27ab7bb58e3"
PORTABLE_FORMAT = "roblox-studio-mcp-v2-portable-release"
PORTABLE_PRODUCT = "RobloxStudioMCPv2"
CANONICAL_SERVER_NAME = "Roblox_Studio_Multisession"
CANONICAL_SERVER_HEADER = (
    "[mcp_servers." + CANONICAL_SERVER_NAME + "]"
)
LEGACY_SERVER_HEADER = "[mcp_servers.Roblox_Studio_v2]"
_HEX = frozenset("0123456789abcdef")
_SOURCE_REPOSITORY = Path(__file__).resolve().parent.parent
_GIT_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
_MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024

# This is the same closed source-to-archive mapping used by the deterministic
# portable builder. Keeping the binding allowlist here makes a newly packaged
# path fail closed until the release proof is deliberately reviewed with it.
_CANDIDATE_SOURCE_MAP: Tuple[Tuple[str, str, int], ...] = (
    ("release_tools/PORTABLE_INSTALL.md", "INSTALL.md", 0o644),
    ("release_tools/bootstrap.py", "bootstrap.py", 0o755),
    ("release_tools/installer.py", "install.py", 0o755),
    ("release_tools/runtime_launcher.py", "launcher-template.py", 0o644),
    ("release_tools/updater.py", "release_updater.py", 0o644),
    ("platform_support.py", "platform_support.py", 0o644),
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
        "scripts/durable_operation_handlers.luau",
        "payload/scripts/durable_operation_handlers.luau",
        0o644,
    ),
    (
        "scripts/play_server_bridge.luau",
        "payload/scripts/play_server_bridge.luau",
        0o644,
    ),
    (
        "scripts/render_studio_plugin.py",
        "payload/scripts/render_studio_plugin.py",
        0o644,
    ),
    (
        "scripts/review_upstream_catalog.py",
        "payload/scripts/review_upstream_catalog.py",
        0o755,
    ),
    (
        "scripts/studio_plugin_template.luau",
        "payload/scripts/studio_plugin_template.luau",
        0o644,
    ),
    *tuple(
        (
            "studio_mcp_v2/" + module,
            "payload/studio_mcp_v2/" + module,
            0o644,
        )
        for module in (
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
            "schema_validation.py",
            "service.py",
            "session.py",
            "validation.py",
        )
    ),
)


def _git_object_id(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in _HEX for character in value)
    ):
        raise ProofError(label + " is not a lowercase Git object ID")
    return value


def _git_output(
    repository: Path,
    arguments: Sequence[str],
    *,
    max_output_bytes: int = _MAX_GIT_OUTPUT_BYTES,
) -> bytes:
    executable_value = shutil.which("git", path=_GIT_PATH)
    if executable_value is None:
        raise ProofError("Git is unavailable for candidate source binding")
    executable = Path(executable_value).resolve(strict=True)
    if (
        executable.is_symlink()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ProofError("Git executable identity is unsafe")
    environment = {
        "PATH": _GIT_PATH,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        process = subprocess.run(
            [
                str(executable),
                "--no-replace-objects",
                "-C",
                str(repository),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProofError(
            "candidate source Git validation could not run"
        ) from exc
    if (
        process.returncode != 0
        or len(process.stdout) > max_output_bytes
        or len(process.stderr) > 64 * 1024
    ):
        raise ProofError("candidate source Git validation failed")
    return process.stdout


def _candidate_source_binding(
    manifest: Mapping[str, Any],
    *,
    repository: Path,
    source_commit: str,
    source_tree: str,
) -> Dict[str, Any]:
    root = Path(repository).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ProofError("candidate source repository is unsafe")
    top_level = _git_output(
        root, ("rev-parse", "--show-toplevel"), max_output_bytes=4096
    )
    try:
        observed_root = Path(
            top_level.decode("utf-8").strip()
        ).resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        raise ProofError("candidate source repository identity is invalid") from exc
    if observed_root != root:
        raise ProofError("candidate source repository is not its Git root")
    if (
        _git_output(
            root,
            ("cat-file", "-t", source_commit),
            max_output_bytes=64,
        ).strip()
        != b"commit"
    ):
        raise ProofError("candidate source commit object is not a commit")
    if (
        _git_output(
            root,
            ("cat-file", "-t", source_tree),
            max_output_bytes=64,
        ).strip()
        != b"tree"
    ):
        raise ProofError("candidate source tree object is not a tree")
    observed_tree = _git_output(
        root,
        ("rev-parse", source_commit + "^{tree}"),
        max_output_bytes=128,
    ).decode("ascii").strip()
    if observed_tree != source_tree:
        raise ProofError(
            "candidate source tree is not the exact commit tree"
        )

    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise ProofError("candidate build manifest lacks its file receipt")
    entries: Dict[str, Mapping[str, Any]] = {}
    for value in raw_entries:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"path", "sha256", "size", "mode"}
            or not isinstance(value.get("path"), str)
            or value["path"] in entries
        ):
            raise ProofError("candidate build manifest file receipt is invalid")
        entries[value["path"]] = value
    expected = {
        archive_path: (source_path, mode)
        for source_path, archive_path, mode in _CANDIDATE_SOURCE_MAP
    }
    if set(entries) != set(expected):
        raise ProofError(
            "candidate build manifest differs from the reviewed source map"
        )

    receipt = []
    for archive_path in sorted(expected):
        source_path, expected_mode = expected[archive_path]
        value = entries[archive_path]
        data = _git_output(
            root,
            ("cat-file", "blob", source_tree + ":" + source_path),
        )
        digest = hashlib.sha256(data).hexdigest()
        if (
            value.get("sha256") != digest
            or value.get("size") != len(data)
            or value.get("mode") != expected_mode
        ):
            raise ProofError(
                "candidate archive is not bound to source tree path "
                + source_path
            )
        receipt.append(
            {
                "archive_path": archive_path,
                "source_path": source_path,
                "sha256": digest,
                "size": len(data),
                "mode": expected_mode,
            }
        )
    return {
        "verified": True,
        "packaged_source_file_count": len(receipt),
        "source_map_sha256": _fingerprint_sha256(
            {"files": receipt}
        ),
    }


def _fingerprint_sha256(
    value: Mapping[str, Any],
) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _assert_portable_identity(
    archive: Mapping[str, Any],
    *,
    label: str,
) -> None:
    manifest = archive.get("manifest")
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("format") != PORTABLE_FORMAT
        or manifest.get("product") != PORTABLE_PRODUCT
    ):
        raise ProofError(label + " portable product identity is invalid")


def _assert_transaction(
    result: Mapping[str, Any],
    *,
    action: str,
    previous_version: str,
    current_version: str,
    archive_sha256: Optional[str],
) -> Mapping[str, Any]:
    transaction = result.get("transaction")
    if (
        result.get("ok") is not True
        or result.get("action") != action
        or result.get("previous_version") != previous_version
        or result.get("version") != current_version
        or result.get("archive_sha256") != archive_sha256
        or not isinstance(transaction, Mapping)
        or transaction.get("action") != action
        or transaction.get("previous_version") != previous_version
        or transaction.get("current_version") != current_version
        or transaction.get("archive_sha256") != archive_sha256
        or not isinstance(transaction.get("receipt"), str)
    ):
        raise ProofError(action + " transaction receipt identity is invalid")
    return transaction


def _registration_evidence(
    module: Any,
    layout: Any,
    *,
    expected_block: Optional[bytes] = None,
) -> Dict[str, Any]:
    block = module._expected_codex_block(layout)
    if expected_block is not None and block != expected_block:
        raise ProofError(
            "candidate changed the canonical Codex registration block"
        )
    config = layout.codex_config.read_bytes()
    canonical_header = CANONICAL_SERVER_HEADER.encode("ascii")
    legacy_header = LEGACY_SERVER_HEADER.encode("ascii")
    try:
        state = json.loads(
            layout.install_state.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofError("installed registration state is invalid") from exc
    codex = state.get("codex")
    if (
        config.count(block) != 1
        or config.count(canonical_header) != 1
        or config.count(legacy_header) != 0
        or not isinstance(codex, Mapping)
        or codex.get("table") != CANONICAL_SERVER_HEADER
        or codex.get("block_sha256")
        != hashlib.sha256(block).hexdigest()
    ):
        raise ProofError(
            "installed canonical registration identity is invalid"
        )
    config_sha256, config_mode, config_size = _file_fingerprint(
        layout.codex_config
    )
    return {
        "server": CANONICAL_SERVER_NAME,
        "former_server_absent": True,
        "block_sha256": hashlib.sha256(block).hexdigest(),
        "config_sha256": config_sha256,
        "config_mode": config_mode,
        "config_size": config_size,
        "block": block,
    }


def prove_multisession_update_rollback(
    *,
    prior_archive: Path,
    prior_checksum_file: Path,
    candidate_archive: Path,
    candidate_checksum_file: Path,
    candidate_expected_sha256: str,
    candidate_version: str,
    source_commit: str,
    source_tree: str,
    source_repository: Path = _SOURCE_REPOSITORY,
    temporary_parent: Optional[Path] = None,
) -> Dict[str, Any]:
    """Prove exact rc.5 update and byte/mode rollback with real packages."""

    if candidate_version != CANDIDATE_VERSION:
        raise ProofError(
            "Multisession update proof requires candidate "
            + CANDIDATE_VERSION
        )
    source_commit = _git_object_id(
        source_commit, "candidate source commit"
    )
    source_tree = _git_object_id(source_tree, "candidate source tree")

    prior = _archive_manifest(
        prior_archive,
        checksum_file=prior_checksum_file,
        expected_sha256=PRIOR_ARCHIVE_SHA256,
        expected_version=PRIOR_VERSION,
    )
    candidate = _archive_manifest(
        candidate_archive,
        checksum_file=candidate_checksum_file,
        expected_sha256=candidate_expected_sha256,
        expected_version=candidate_version,
    )
    _assert_portable_identity(prior, label=PRIOR_VERSION)
    _assert_portable_identity(candidate, label=candidate_version)
    source_binding = _candidate_source_binding(
        candidate["manifest"],
        repository=source_repository,
        source_commit=source_commit,
        source_tree=source_tree,
    )

    parent = None
    if temporary_parent is not None:
        parent = Path(temporary_parent).resolve(strict=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ProofError("temporary proof parent is unsafe")

    counters = {
        "lifecycle_calls": 0,
        "stop_calls": 0,
        "subprocess_stop_calls": 0,
    }
    phases: Dict[str, bool] = {
        "prior_archive_verified": True,
        "candidate_archive_verified": True,
    }
    report: Dict[str, Any] = {}
    proof_root: Optional[Path] = None
    with tempfile.TemporaryDirectory(
        prefix="studio-mcp-multisession-update-proof-",
        dir=None if parent is None else str(parent),
    ) as temporary:
        root = Path(temporary)
        proof_root = root
        prior_package = _extract_audited_archive(
            prior["source"], root / "prior-package"
        )
        candidate_package = _extract_audited_archive(
            candidate["source"], root / "candidate-package"
        )
        for relative, expected in (
            ("release-manifest.json", PRIOR_MANIFEST_SHA256),
            ("install.py", PRIOR_INSTALLER_SHA256),
            ("release_updater.py", PRIOR_UPDATER_SHA256),
            ("bootstrap.py", PRIOR_BOOTSTRAP_SHA256),
        ):
            if _sha256_file(prior_package / relative) != expected:
                raise ProofError(
                    "immutable " + PRIOR_VERSION + " " + relative + " changed"
                )
        phases["prior_package_identity_verified"] = True

        prior_module = _load_installer(prior_package)
        candidate_module = _load_installer(candidate_package)
        if (
            getattr(prior_module, "VERSION", None) != PRIOR_VERSION
            or getattr(candidate_module, "VERSION", None)
            != candidate_version
        ):
            raise ProofError("portable installer version identity drifted")

        home = root / "synthetic-home"
        codex = home / ".codex"
        plugins = home / "Documents" / "Roblox" / "Plugins"
        codex.mkdir(mode=0o755, parents=True)
        plugins.mkdir(mode=0o755, parents=True)
        initial_config = (
            b"# synthetic pre-existing user configuration\n"
            b"[mcp_servers.Roblox_Studio]\n"
            b'command = "synthetic-v1-command"\n'
            b"args = []\n"
            b"\n"
            b"[features]\n"
            b"synthetic_user_setting = true\n"
        )
        config_path = codex / "config.toml"
        config_path.write_bytes(initial_config)
        v1_plugin = plugins / "MCPStudioPlugin.rbxm"
        v1_plugin.write_bytes(b"synthetic v1 fallback sentinel\n")
        v1_fingerprint = _file_fingerprint(v1_plugin)

        layout = prior_module.InstallLayout.for_user(home=home)
        prior_manager = _proof_installer(
            prior_module, prior_package, layout, counters
        )
        prior_updater_module = (
            prior_module._load_release_updater_module()
        )
        with _bounded_subprocesses(root, counters):
            installed = prior_manager.install()
            if (
                installed.get("ok") is not True
                or installed.get("version") != PRIOR_VERSION
            ):
                raise ProofError("actual " + PRIOR_VERSION + " install failed")
            _assert_doctor_healthy(
                prior_manager.doctor(), PRIOR_VERSION + " pre-update"
            )
            prior_registration = _registration_evidence(
                prior_module, layout
            )
            baseline = _active_fingerprint(layout)
            baseline_sha256 = _fingerprint_sha256(baseline)
            if _file_fingerprint(v1_plugin) != v1_fingerprint:
                raise ProofError(PRIOR_VERSION + " install changed v1")
            phases["prior_install_doctor_and_registration"] = True

            forward_updater = prior_updater_module.ReleaseUpdater(
                prior_manager,
                candidate_factory=_candidate_factory(
                    prior_updater_module, counters
                ),
                platform_check=lambda: None,
                runtime_check=lambda: None,
            )
            forward = forward_updater.update(
                tag="v" + candidate_version,
                archive=candidate["source"],
                checksum_file=Path(
                    candidate_checksum_file
                ).resolve(strict=True),
                expected_sha256=candidate["sha256"],
            )
            forward_transaction = _assert_transaction(
                forward,
                action="update",
                previous_version=PRIOR_VERSION,
                current_version=candidate_version,
                archive_sha256=candidate["sha256"],
            )
            forward_receipt_path = Path(
                str(forward_transaction["receipt"])
            )
            try:
                forward_receipt_path.resolve(strict=True).relative_to(
                    root.resolve(strict=True)
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ProofError(
                    "forward transaction receipt escaped the disposable root"
                ) from exc
            forward_receipt_sha256 = _sha256_file(
                forward_receipt_path
            )
            phases["forward_update_receipted"] = True

            retained_candidate = layout.packages / candidate_version
            candidate_module = _load_installer(retained_candidate)
            candidate_layout = candidate_module.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            candidate_manager = _proof_installer(
                candidate_module,
                retained_candidate,
                candidate_layout,
                counters,
            )
            candidate_doctor = candidate_manager.doctor()
            _assert_doctor_healthy(
                candidate_doctor, candidate_version + " post-update"
            )
            if candidate_doctor.get("version") != candidate_version:
                raise ProofError("candidate doctor version drifted")
            candidate_registration = _registration_evidence(
                candidate_module,
                candidate_layout,
                expected_block=prior_registration["block"],
            )
            for key in (
                "server",
                "former_server_absent",
                "block_sha256",
                "config_sha256",
                "config_mode",
                "config_size",
            ):
                if candidate_registration[key] != prior_registration[key]:
                    raise ProofError(
                        "candidate update changed canonical registration bytes"
                    )
            if _file_fingerprint(v1_plugin) != v1_fingerprint:
                raise ProofError("candidate update changed v1")

            candidate_updater_module = (
                candidate_module._load_release_updater_module()
            )
            candidate_updater = candidate_updater_module.ReleaseUpdater(
                candidate_manager,
                candidate_factory=_candidate_factory(
                    candidate_updater_module, counters
                ),
                platform_check=lambda: None,
                runtime_check=lambda: None,
            )
            status = candidate_updater.status()
            rollback_status = status.get("rollback")
            retained = status.get("retained_releases")
            retained_versions = (
                {
                    item.get("version")
                    for item in retained
                    if isinstance(item, Mapping)
                    and item.get("valid") is True
                }
                if isinstance(retained, list)
                else set()
            )
            if (
                status.get("ok") is not True
                or status.get("installed_version") != candidate_version
                or not isinstance(rollback_status, Mapping)
                or rollback_status.get("available") is not True
                or rollback_status.get("target_version") != PRIOR_VERSION
                or rollback_status.get(
                    "requires_accept_current_version"
                )
                != candidate_version
                or PRIOR_VERSION not in retained_versions
                or candidate_version not in retained_versions
                or candidate_updater.interrupted_update_status().get(
                    "present"
                )
                is not False
            ):
                raise ProofError(
                    "candidate does not expose exact immediate rc.5 rollback"
                )
            phases["candidate_doctor_registration_and_rollback_target"] = True

            reverse = candidate_updater.rollback(
                to_version=PRIOR_VERSION,
                accept_current_version=candidate_version,
            )
            reverse_transaction = _assert_transaction(
                reverse,
                action="rollback",
                previous_version=candidate_version,
                current_version=PRIOR_VERSION,
                archive_sha256=None,
            )
            rollback_receipt_path = Path(
                str(reverse_transaction["receipt"])
            )
            try:
                rollback_receipt_path.resolve(strict=True).relative_to(
                    root.resolve(strict=True)
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ProofError(
                    "rollback transaction receipt escaped the disposable root"
                ) from exc
            rollback_receipt_sha256 = _sha256_file(
                rollback_receipt_path
            )
            if (
                candidate_updater.interrupted_update_status().get(
                    "present"
                )
                is not False
            ):
                raise ProofError(
                    "rollback left a pending transaction marker"
                )
            phases["rollback_receipted"] = True

            restored = _active_fingerprint(layout)
            restored_sha256 = _fingerprint_sha256(restored)
            if restored != baseline or restored_sha256 != baseline_sha256:
                raise ProofError(
                    "rollback did not restore all active bytes and modes"
                )
            restored_registration = _registration_evidence(
                prior_module,
                layout,
                expected_block=prior_registration["block"],
            )
            for key in (
                "server",
                "former_server_absent",
                "block_sha256",
                "config_sha256",
                "config_mode",
                "config_size",
            ):
                if restored_registration[key] != prior_registration[key]:
                    raise ProofError(
                        "rollback did not restore exact registration evidence"
                    )
            if (
                _file_fingerprint(v1_plugin) != v1_fingerprint
                or not config_path.read_bytes().startswith(initial_config)
            ):
                raise ProofError(
                    "rollback changed the unowned config or v1 boundary"
                )
            phases["active_bytes_modes_and_registration_restored"] = True

            prior_from_retained = _load_installer(
                layout.packages / PRIOR_VERSION
            )
            restored_layout = prior_from_retained.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            restored_manager = _proof_installer(
                prior_from_retained,
                layout.packages / PRIOR_VERSION,
                restored_layout,
                counters,
            )
            restored_doctor = restored_manager.doctor()
            _assert_doctor_healthy(
                restored_doctor, "restored " + PRIOR_VERSION
            )
            if restored_doctor.get("version") != PRIOR_VERSION:
                raise ProofError("restored rc.5 doctor version drifted")
            phases["restored_rc5_doctor"] = True

            report = {
                "ok": True,
                "format": (
                    "roblox-studio-mcp-multisession-update-rollback-proof"
                ),
                "schema_version": 1,
                "source": {
                    "commit": source_commit,
                    "tree": source_tree,
                    "archive_binding": source_binding,
                },
                "baseline": {
                    "version": PRIOR_VERSION,
                    "archive_sha256": prior["sha256"],
                    "release_manifest_sha256": PRIOR_MANIFEST_SHA256,
                    "installer_sha256": PRIOR_INSTALLER_SHA256,
                    "updater_sha256": PRIOR_UPDATER_SHA256,
                    "bootstrap_sha256": PRIOR_BOOTSTRAP_SHA256,
                    "source_commit": PRIOR_SOURCE_COMMIT,
                    "source_tree": PRIOR_SOURCE_TREE,
                    "registration": {
                        key: value
                        for key, value in prior_registration.items()
                        if key != "block"
                    },
                },
                "candidate": {
                    "version": candidate_version,
                    "archive_sha256": candidate["sha256"],
                    "release_manifest_sha256": _sha256_file(
                        candidate_package / "release-manifest.json"
                    ),
                    "manifest_file_count": len(
                        candidate["manifest"]["files"]
                    ),
                    "registration": {
                        key: value
                        for key, value in candidate_registration.items()
                        if key != "block"
                    },
                },
                "transition": {
                    "forward": PRIOR_VERSION + "->" + candidate_version,
                    "rollback": candidate_version + "->" + PRIOR_VERSION,
                    "forward_receipt_sha256": (
                        forward_receipt_sha256
                    ),
                    "rollback_receipt_sha256": (
                        rollback_receipt_sha256
                    ),
                    "immediate_rollback_target": PRIOR_VERSION,
                    "active_file_count": len(baseline),
                    "active_fingerprint_sha256": baseline_sha256,
                    "exact_active_bytes_and_modes_restored": True,
                    "exact_registration_restored": True,
                },
                "phases": dict(phases),
                "isolation": {
                    "network_or_broker_started": False,
                    "live_codex_config_touched": False,
                    "live_studio_plugins_touched": False,
                    "lifecycle_calls_simulated": counters[
                        "lifecycle_calls"
                    ],
                    "stop_calls_simulated": counters["stop_calls"],
                    "subprocess_stop_calls_simulated": counters[
                        "subprocess_stop_calls"
                    ],
                },
            }

    if proof_root is None or proof_root.exists():
        raise ProofError("disposable proof root was not removed")
    report["isolation"]["temporary_home_removed"] = True
    return report
