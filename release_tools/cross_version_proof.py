"""Exact isolated rc.4 -> candidate -> rc.4 rollback proof.

Unlike the generic release proof, this module runs both verified portable
installers. Lifecycle subprocesses are replaced with bounded acknowledgements,
so no broker, network listener, Studio process, or live installation is used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

from .audit import audit_archive
from .proof import (
    ProofError,
    _assert_doctor_healthy,
    _checksum_from_file,
    _extract_audited_archive,
    _load_installer,
    _sha256_file,
)


RC4_VERSION = "0.3.0-rc.4"
RC4_ARCHIVE_SHA256 = (
    "e92d75f16c1607c820c852264e30985e2292187fcb3e47decc87530f891fb0c3"
)
RC4_BOOTSTRAP_SHA256 = (
    "96d602fff3acb610dda09e1c0769c7864707267ac018004b9b6aa4c6f6f7a750"
)
RC4_LIVE_PLUGIN_SHA256 = (
    "a9b0c4aaad7b9ca9d8fe9b8714f3545ff977fcaa678fab49bd0eae62d3cd9e1f"
)
RC4_EFFECTIVE_CATALOG_SHA256 = (
    "a147b054be5a33d2c8be575c9a82ae3b65bb8f58b0d84c0caad491a3d87281f7"
)
RC4_UPSTREAM_CATALOG_SHA256 = (
    "6b305e81c82d11f0fa7657cd81f1664f4fc455564f4594e29bc9a12d7748f2a7"
)
RC4_COMPATIBILITY_SHA256 = (
    "bff9b66f3f7dc6fd956ae27c6d05c6cd6f31ff3c48713fb4e41957911ee6ddcf"
)
RC4_RESTORE_MANIFEST_SHA256 = (
    "30e053bbb91f9a5de9ce4a132afb635e149930abe6a4e30bf85fa76f2cfc0478"
)
RC4_SOURCE_COMMIT = "2329c9523cfde4a53fde922d01d0144f31e0adfd"
RC4_SOURCE_TREE = "7cb0540145a6ccfbec865e8aa128872e444397d2"
RC4_TAG_OBJECT = "7ec127a26f02154ae327dbe2f236bd507172fdfb"
MULTISESSION_PRIOR_VERSION = "0.4.0-rc.4"
MULTISESSION_CANDIDATE_VERSION = "0.4.0-rc.5"
MULTISESSION_PRIOR_ARCHIVE_SHA256 = (
    "21e75b1fa74fdc7463d29fde45dffaa35323cb5017e47b85b29289619988adf8"
)
MULTISESSION_PRIOR_MANIFEST_SHA256 = (
    "ce926e9e81ab0803c028831cf41614050e29016f11ac2ac07325556e63ab44cd"
)
MULTISESSION_PRIOR_INSTALLER_SHA256 = (
    "714fa7758f4ca6f43b18898e713117b15c40adcf6d9f6046c8272ba56fb03f11"
)
MULTISESSION_PRIOR_UPDATER_SHA256 = (
    "ecc8ec2db2ffda1f4d1c64ddc35db7b8f2735878bdbfa52de2df0bc4aa756fbe"
)
MULTISESSION_PRIOR_BOOTSTRAP_SHA256 = RC4_BOOTSTRAP_SHA256
_PORTABLE_FORMAT = "roblox-studio-mcp-v2-portable-release"
_PORTABLE_PRODUCT = "RobloxStudioMCPv2"
_FORMER_SERVER_NAME = "Roblox_Studio_v2"
_CANONICAL_SERVER_NAME = "Roblox_Studio_Multisession"
_FORMER_SERVER_HEADER = (
    b"[mcp_servers." + _FORMER_SERVER_NAME.encode("ascii") + b"]"
)
_CANONICAL_SERVER_HEADER = (
    b"[mcp_servers." + _CANONICAL_SERVER_NAME.encode("ascii") + b"]"
)
_SHA256 = frozenset("0123456789abcdef")


class _ProofPlatform:
    def as_dict(self) -> Dict[str, Any]:
        return {
            "system": "Darwin",
            "machine": "arm64",
            "rosetta_translated": False,
            "supported": True,
            "target": "macos-arm64",
        }


def _safe_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256 for character in value)
    ):
        raise ProofError(label + " is not a lowercase SHA-256")
    return value


def _read_checksum_manifest(path: Path) -> Dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProofError("restore checksum manifest is unreadable") from exc
    entries: Dict[str, str] = {}
    for line in lines:
        pieces = line.strip().split(maxsplit=1)
        if len(pieces) != 2:
            raise ProofError("restore checksum manifest has a malformed line")
        digest = _safe_digest(
            pieces[0].lower(), "restore checksum manifest digest"
        )
        rendered = pieces[1].lstrip("*")
        if rendered.startswith("./"):
            rendered = rendered[2:]
        relative = PurePosixPath(rendered)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or rendered in entries
        ):
            raise ProofError("restore checksum manifest path is unsafe")
        entries[rendered] = digest
    if not entries:
        raise ProofError("restore checksum manifest is empty")
    return entries


def _verify_restore_bundle(root: Path) -> Dict[str, Any]:
    bundle = Path(root).resolve(strict=True)
    if bundle.is_symlink() or not bundle.is_dir():
        raise ProofError("rc.4 restore bundle is not a safe directory")
    manifest = bundle / "SHA256SUMS.restore"
    if _sha256_file(manifest) != RC4_RESTORE_MANIFEST_SHA256:
        raise ProofError("rc.4 restore checksum manifest changed")
    manifest_checksum = bundle / "SHA256SUMS.restore.sha256"
    if (
        _checksum_from_file(manifest_checksum, manifest.name)
        != RC4_RESTORE_MANIFEST_SHA256
    ):
        raise ProofError("rc.4 restore checksum-of-checksum changed")
    entries = _read_checksum_manifest(manifest)
    for relative, expected in entries.items():
        target = bundle.joinpath(*PurePosixPath(relative).parts)
        try:
            target.resolve(strict=True).relative_to(bundle)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ProofError(
                "rc.4 restore manifest target is missing or escapes"
            ) from exc
        if target.is_symlink() or not target.is_file():
            raise ProofError("rc.4 restore manifest target is unsafe")
        if _sha256_file(target) != expected:
            raise ProofError("rc.4 restore bundle checksum mismatch")

    provenance_path = bundle / "evidence" / "PROVENANCE.json"
    try:
        provenance = json.loads(
            provenance_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofError("rc.4 restore provenance is invalid") from exc
    source = provenance.get("source")
    release = provenance.get("release")
    ownership = provenance.get("installed_ownership")
    if (
        provenance.get("format")
        != "roblox-studio-mcp-v2-restore-provenance"
        or provenance.get("version") != RC4_VERSION
        or not isinstance(source, Mapping)
        or source.get("commit") != RC4_SOURCE_COMMIT
        or source.get("tree") != RC4_SOURCE_TREE
        or source.get("tag") != "v" + RC4_VERSION
        or source.get("tag_object") != RC4_TAG_OBJECT
        or not isinstance(release, Mapping)
        or release.get("archive_sha256") != RC4_ARCHIVE_SHA256
        or release.get("bootstrap_sha256") != RC4_BOOTSTRAP_SHA256
        or not isinstance(ownership, Mapping)
        or ownership.get("effective_catalog_sha256")
        != RC4_EFFECTIVE_CATALOG_SHA256
        or ownership.get("upstream_snapshot_sha256")
        != RC4_UPSTREAM_CATALOG_SHA256
        or ownership.get("compatibility_map_sha256")
        != RC4_COMPATIBILITY_SHA256
        or ownership.get("installed_plugin_sha256")
        != RC4_LIVE_PLUGIN_SHA256
    ):
        raise ProofError("rc.4 restore provenance identity drifted")
    return {
        "root": bundle,
        "file_count": len(entries),
        "upstream": (
            bundle
            / "evidence"
            / "upstream-known-tool-catalog.json"
        ),
    }


def _archive_manifest(
    archive: Path,
    *,
    checksum_file: Path,
    expected_sha256: str,
    expected_version: str,
) -> Dict[str, Any]:
    source = Path(archive).resolve(strict=True)
    digest = _sha256_file(source)
    expected = _safe_digest(
        expected_sha256.lower(), "accepted archive SHA-256"
    )
    if (
        digest != expected
        or _checksum_from_file(
            Path(checksum_file).resolve(strict=True), source.name
        )
        != expected
    ):
        raise ProofError("release archive SHA-256 verification failed")
    audit = audit_archive(source)
    if not audit.ok:
        codes = sorted({item.code for item in audit.findings})
        raise ProofError(
            "release archive audit failed: " + ", ".join(codes)
        )
    with tarfile.open(source, "r:gz") as package:
        candidates = [
            member
            for member in package.getmembers()
            if member.isfile()
            and PurePosixPath(member.name).name
            == "release-manifest.json"
            and len(PurePosixPath(member.name).parts) == 2
        ]
        if len(candidates) != 1:
            raise ProofError(
                "release archive has no unique root manifest"
            )
        extracted = package.extractfile(candidates[0])
        if extracted is None:
            raise ProofError("release archive manifest is unreadable")
        try:
            manifest = json.loads(extracted.read().decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProofError("release archive manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != expected_version
        or not isinstance(manifest.get("files"), list)
    ):
        raise ProofError("release archive version identity is wrong")
    return {
        "source": source,
        "sha256": digest,
        "manifest": manifest,
        "files_checked": audit.files_checked,
        "bytes_checked": audit.bytes_checked,
    }


def _file_fingerprint(path: Path) -> Tuple[str, int, int]:
    if path.is_symlink() or not path.is_file():
        raise ProofError("active rollback scope contains an unsafe file")
    details = path.stat()
    return (
        _sha256_file(path),
        stat.S_IMODE(details.st_mode),
        details.st_size,
    )


def _tree_fingerprint(root: Path) -> Dict[str, Tuple[str, int, int]]:
    if root.is_symlink() or not root.is_dir():
        raise ProofError("active rollback scope contains an unsafe directory")
    result: Dict[str, Tuple[str, int, int]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ProofError("active rollback scope contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProofError(
                "active rollback scope contains a non-regular entry"
            )
        relative = path.relative_to(root).as_posix()
        result[relative] = _file_fingerprint(path)
    return result


def _active_fingerprint(layout: Any) -> Dict[str, Tuple[str, int, int]]:
    result: Dict[str, Tuple[str, int, int]] = {}
    for name in ("config", "artifacts", "bin"):
        root = layout.support_root / name
        for relative, value in _tree_fingerprint(root).items():
            result["support/" + name + "/" + relative] = value
    for name, path in (
        ("support/state/install-state.json", layout.install_state),
        ("external/codex-config", layout.codex_config),
        ("external/studio-plugin", layout.plugin_target),
    ):
        result[name] = _file_fingerprint(path)
    return result


def _catalog_digest_for_lifecycle(module: Any, layout: Any) -> str:
    lifecycle = module._load_release_submodule(
        layout.release, "lifecycle"
    )
    catalog = lifecycle.ToolCatalog.from_file(
        layout.effective_catalog
    )
    return lifecycle._catalog_digest(catalog)


def _proof_installer(
    module: Any,
    package_root: Path,
    layout: Any,
    counters: Dict[str, int],
) -> Any:
    # The archive version/platform surfaces are already verified before the
    # synthetic home exists. Avoid even the local sysctl subprocess while the
    # proof's fail-closed subprocess boundary is active.
    module.require_supported_platform = lambda: _ProofPlatform()
    module.require_supported_runtime = lambda: tuple(
        sys.version_info[:3]
    )
    module.detect_platform = lambda: _ProofPlatform()
    base = getattr(module, "Installer", None)
    if not isinstance(base, type):
        raise ProofError("portable package lacks the Installer API")

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
                return {
                    "ok": True,
                    "running": False,
                    "stopped": False,
                }
            if command in {"start", "doctor", "status"}:
                catalog_sha256 = _catalog_digest_for_lifecycle(
                    module, self.layout
                )
                if command == "start":
                    return {
                        "ok": True,
                        "broker": {
                            "catalog_sha256": catalog_sha256
                        },
                    }
                return {
                    "ok": True,
                    "lifecycle": {"condition": "stopped"},
                    "catalog": {
                        "installed_v1_cache": None,
                        "catalog_sha256": catalog_sha256,
                    },
                }
            raise ProofError(
                "rollback proof refused an unexpected lifecycle command"
            )

    return ProofInstaller(
        package_root,
        layout,
        python_executable=sys.executable,
    )


def _candidate_factory(
    updater_module: Any,
    counters: Dict[str, int],
) -> Callable[[Any, Path, str], Any]:
    def create(
        current_installer: Any,
        package_root: Path,
        expected_version: str,
    ) -> Any:
        expected_manifest_sha256: Optional[str] = None
        try:
            current_state = current_installer._load_state(
                optional=False
            )
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
        updater_module._preverify_candidate_package(
            package_root,
            expected_version,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        module = updater_module._module_from_package(package_root)
        module._load_release_updater_module = lambda: updater_module
        if getattr(module, "VERSION", None) != expected_version:
            raise ProofError(
                "verified installer version does not match its package"
            )
        module.verify_release_package(package_root)
        layout = module.InstallLayout(
            home=current_installer.layout.home,
            support_root=current_installer.layout.support_root,
            codex_config=current_installer.layout.codex_config,
            studio_plugins=current_installer.layout.studio_plugins,
        )
        return _proof_installer(
            module, package_root, layout, counters
        )

    return create


@contextlib.contextmanager
def _bounded_subprocesses(
    proof_root: Path,
    counters: Dict[str, int],
) -> Iterator[None]:
    original = subprocess.run
    resolved_root = proof_root.resolve(strict=True)

    def dispatch(
        command: Any,
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        del args, kwargs
        normalized = (
            list(command)
            if isinstance(command, (list, tuple))
            else []
        )
        if (
            len(normalized) == 6
            and normalized[1:3] == ["-I", "-B"]
            and normalized[4:] == ["stop", "--json"]
        ):
            bootstrap = Path(str(normalized[3]))
            try:
                bootstrap.resolve(strict=True).relative_to(
                    resolved_root
                )
            except (FileNotFoundError, OSError, ValueError):
                pass
            else:
                counters["subprocess_stop_calls"] += 1
                payload = json.dumps(
                    {
                        "ok": True,
                        "running": False,
                        "stopped": False,
                    }
                ).encode("utf-8")
                return subprocess.CompletedProcess(
                    command, 0, stdout=payload, stderr=b""
                )
        raise ProofError(
            "rollback proof refused an unexpected subprocess"
        )

    subprocess.run = dispatch
    try:
        yield
    finally:
        subprocess.run = original


def _assert_hash(path: Path, expected: str, label: str) -> None:
    observed = _sha256_file(path)
    if observed != expected:
        raise ProofError(
            label
            + " bytes do not match rc.4 provenance (observed "
            + observed
            + ")"
        )


def _assert_rc4_catalog_contract(
    layout: Any,
    *,
    expected_effective_sha256: Optional[str] = None,
) -> str:
    effective_sha256 = _sha256_file(layout.effective_catalog)
    if (
        _sha256_file(layout.catalog_artifact)
        != effective_sha256
        or (
            expected_effective_sha256 is not None
            and effective_sha256 != expected_effective_sha256
        )
    ):
        raise ProofError(
            "rc.4 effective catalog mirrors or rollback bytes differ"
        )
    _assert_hash(
        layout.upstream_catalog,
        RC4_UPSTREAM_CATALOG_SHA256,
        "upstream catalog",
    )
    _assert_hash(
        layout.artifacts / "upstream-known-tool-catalog.json",
        RC4_UPSTREAM_CATALOG_SHA256,
        "upstream catalog artifact",
    )
    _assert_hash(
        layout.compatibility_manifest,
        RC4_COMPATIBILITY_SHA256,
        "compatibility manifest",
    )
    return effective_sha256


def _assert_candidate_catalog_contract(
    layout: Any,
    package_root: Path,
) -> None:
    expected = {
        layout.effective_catalog: (
            package_root
            / "payload"
            / "config"
            / "durable-tool-catalog.json"
        ),
        layout.catalog_artifact: (
            package_root
            / "payload"
            / "config"
            / "durable-tool-catalog.json"
        ),
        layout.upstream_catalog: (
            package_root
            / "payload"
            / "config"
            / "tool-catalog.json"
        ),
        layout.artifacts / "upstream-known-tool-catalog.json": (
            package_root
            / "payload"
            / "config"
            / "tool-catalog.json"
        ),
        layout.compatibility_manifest: (
            package_root
            / "payload"
            / "config"
            / "upstream-compatibility-map.json"
        ),
    }
    for installed, packaged in expected.items():
        if installed.read_bytes() != packaged.read_bytes():
            raise ProofError(
                "candidate catalog contract did not reset to package bytes"
            )


def prove_multisession_migration_rollback(
    *,
    prior_archive: Path,
    prior_checksum_file: Path,
    candidate_archive: Path,
    candidate_checksum_file: Path,
    candidate_expected_sha256: str,
    candidate_version: str,
    source_commit: str,
    source_tree: str,
    temporary_parent: Optional[Path] = None,
) -> Dict[str, Any]:
    """Prove the exact 0.4 rc.4 public-name migration and rollback."""

    if candidate_version != MULTISESSION_CANDIDATE_VERSION:
        raise ProofError(
            "multisession migration proof requires candidate "
            + MULTISESSION_CANDIDATE_VERSION
        )
    for label, value in (
        ("candidate source commit", source_commit),
        ("candidate source tree", source_tree),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in _SHA256 for character in value)
        ):
            raise ProofError(label + " is not a lowercase Git object ID")

    prior = _archive_manifest(
        prior_archive,
        checksum_file=prior_checksum_file,
        expected_sha256=MULTISESSION_PRIOR_ARCHIVE_SHA256,
        expected_version=MULTISESSION_PRIOR_VERSION,
    )
    candidate = _archive_manifest(
        candidate_archive,
        checksum_file=candidate_checksum_file,
        expected_sha256=candidate_expected_sha256,
        expected_version=candidate_version,
    )
    for label, release in (
        ("prior", prior),
        ("candidate", candidate),
    ):
        manifest = release["manifest"]
        if (
            manifest.get("format") != _PORTABLE_FORMAT
            or manifest.get("product") != _PORTABLE_PRODUCT
        ):
            raise ProofError(
                label + " archive compatibility identity is invalid"
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
        "candidate_mutation_phase_entries": 0,
    }
    phases: Dict[str, bool] = {
        "prior_archive_verified": True,
        "candidate_archive_verified": True,
    }
    active_file_count = 0
    dual_collision_error = ""
    alias_collision_error = ""
    launcher_different_collision_error = ""
    launcher_identical_collision_error = ""
    migration_backup_sha256 = ""
    with tempfile.TemporaryDirectory(
        prefix="studio-mcp-multisession-migration-proof-",
        dir=None if parent is None else str(parent),
    ) as temporary:
        root = Path(temporary)
        prior_package = _extract_audited_archive(
            prior["source"], root / "prior-package"
        )
        candidate_package = _extract_audited_archive(
            candidate["source"], root / "candidate-package"
        )
        for relative, expected in (
            (
                "release-manifest.json",
                MULTISESSION_PRIOR_MANIFEST_SHA256,
            ),
            ("install.py", MULTISESSION_PRIOR_INSTALLER_SHA256),
            (
                "release_updater.py",
                MULTISESSION_PRIOR_UPDATER_SHA256,
            ),
            (
                "bootstrap.py",
                MULTISESSION_PRIOR_BOOTSTRAP_SHA256,
            ),
        ):
            if _sha256_file(prior_package / relative) != expected:
                raise ProofError(
                    "immutable 0.4.0-rc.4 " + relative + " changed"
                )
        phases["prior_package_identity_verified"] = True

        prior_module = _load_installer(prior_package)
        candidate_module = _load_installer(candidate_package)
        if (
            getattr(prior_module, "VERSION", None)
            != MULTISESSION_PRIOR_VERSION
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
                or installed.get("version")
                != MULTISESSION_PRIOR_VERSION
            ):
                raise ProofError("actual 0.4.0-rc.4 install failed")
            _assert_doctor_healthy(
                prior_manager.doctor(), "0.4.0-rc.4 pre-update"
            )
            baseline_config = config_path.read_bytes()
            former_block = prior_module._expected_codex_block(layout)
            if (
                baseline_config.count(former_block) != 1
                or baseline_config.count(_FORMER_SERVER_HEADER) != 1
                or baseline_config.count(_CANONICAL_SERVER_HEADER) != 0
            ):
                raise ProofError(
                    "0.4.0-rc.4 did not establish one exact former "
                    "registration"
                )
            prior_state = json.loads(
                layout.install_state.read_text(encoding="utf-8")
            )
            if (
                prior_state.get("version")
                != MULTISESSION_PRIOR_VERSION
                or prior_state.get("codex", {}).get("table")
                != "[mcp_servers." + _FORMER_SERVER_NAME + "]"
                or prior_state.get("codex", {}).get(
                    "block_sha256"
                )
                != hashlib.sha256(former_block).hexdigest()
            ):
                raise ProofError(
                    "0.4.0-rc.4 former registration ownership is invalid"
                )
            baseline = _active_fingerprint(layout)
            active_file_count = len(baseline)
            phases["prior_install_doctor_and_identity"] = True

            candidate_layout = candidate_module.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            canonical_block = (
                candidate_module._expected_codex_block(
                    candidate_layout
                )
            )
            if (
                canonical_block.count(_CANONICAL_SERVER_HEADER) != 1
                or canonical_block.count(_FORMER_SERVER_HEADER) != 0
            ):
                raise ProofError(
                    "candidate canonical registration block is invalid"
                )

            collision_cases = (
                (
                    baseline_config + b"\n" + canonical_block,
                    "both",
                    "dual",
                ),
                (
                    baseline_config.replace(
                        former_block, canonical_block, 1
                    ),
                    "install-state registration identity",
                    "alias",
                ),
            )
            collision_errors = []
            for collision_config, fragment, label in collision_cases:
                config_path.write_bytes(collision_config)
                protected = _active_fingerprint(layout)
                try:
                    candidate_module._preflight_codex(
                        candidate_layout,
                        prior_state,
                        canonical_block,
                        replace_owned_config=False,
                        allow_legacy_registration_migration=True,
                    )
                except candidate_module.InstallError as exc:
                    rendered = str(exc)
                    if fragment not in rendered:
                        raise ProofError(
                            label
                            + " registration collision returned the wrong "
                            "refusal: "
                            + rendered
                        ) from exc
                    collision_errors.append(rendered)
                else:
                    raise ProofError(
                        label
                        + " registration collision was not refused"
                    )
                if _active_fingerprint(layout) != protected:
                    raise ProofError(
                        label
                        + " registration preflight mutated installed bytes"
                    )
            dual_collision_error, alias_collision_error = (
                collision_errors
            )
            config_path.write_bytes(baseline_config)
            if _active_fingerprint(layout) != baseline:
                raise ProofError(
                    "collision probes did not restore the exact baseline"
                )
            phases["unowned_registration_collisions_refused"] = True

            base_candidate_factory = _candidate_factory(
                prior_updater_module, counters
            )

            def tracked_candidate_factory(
                current_installer: Any,
                package_root: Path,
                expected_version: str,
            ) -> Any:
                candidate_installer = base_candidate_factory(
                    current_installer,
                    package_root,
                    expected_version,
                )
                mutation_phase = getattr(
                    candidate_installer, "_prepare_directories", None
                )
                if not callable(mutation_phase):
                    raise ProofError(
                        "candidate lacks its first mutation phase"
                    )

                def tracked_mutation_phase() -> Any:
                    counters[
                        "candidate_mutation_phase_entries"
                    ] += 1
                    return mutation_phase()

                candidate_installer._prepare_directories = (
                    tracked_mutation_phase
                )
                return candidate_installer

            forward_updater = prior_updater_module.ReleaseUpdater(
                prior_manager,
                candidate_factory=tracked_candidate_factory,
                platform_check=lambda: None,
                runtime_check=lambda: None,
            )
            canonical_launcher = candidate_layout.launcher
            if canonical_launcher.exists() or canonical_launcher.is_symlink():
                raise ProofError(
                    "rc.4 unexpectedly owns the canonical launcher path"
                )
            exact_launcher = candidate_module._shell_exec(
                sys.executable,
                candidate_layout.launcher_bootstrap,
            )
            launcher_cases = (
                (
                    b"#!/bin/sh\nexit 73\n",
                    "different",
                ),
                (
                    exact_launcher,
                    "identical",
                ),
            )
            launcher_errors = []
            for launcher_bytes, label in launcher_cases:
                canonical_launcher.write_bytes(launcher_bytes)
                os.chmod(canonical_launcher, 0o700)
                expected_collision_hash = _sha256_file(
                    canonical_launcher
                )
                protected = _active_fingerprint(layout)
                mutation_entries_before = counters[
                    "candidate_mutation_phase_entries"
                ]
                try:
                    forward_updater.update(
                        tag="v" + candidate_version,
                        archive=candidate["source"],
                        checksum_file=Path(
                            candidate_checksum_file
                        ).resolve(strict=True),
                        expected_sha256=candidate["sha256"],
                    )
                except prior_updater_module.UpdateError as exc:
                    rendered = str(exc)
                    if (
                        "exists without exact ownership" not in rendered
                        or canonical_launcher.name not in rendered
                    ):
                        raise ProofError(
                            label
                            + " canonical launcher collision returned the "
                            "wrong refusal: "
                            + rendered
                        ) from exc
                    launcher_errors.append(rendered)
                else:
                    raise ProofError(
                        label
                        + " canonical launcher collision was not refused"
                    )
                if _active_fingerprint(layout) != protected:
                    raise ProofError(
                        label
                        + " launcher update changed active installed bytes"
                    )
                if (
                    counters["candidate_mutation_phase_entries"]
                    != mutation_entries_before
                ):
                    raise ProofError(
                        label
                        + " launcher collision reached candidate mutation"
                    )
                if (
                    forward_updater.interrupted_update_status().get(
                        "present"
                    )
                    is not False
                ):
                    raise ProofError(
                        label
                        + " launcher collision left a pending transaction"
                    )
                if (
                    not canonical_launcher.is_file()
                    or canonical_launcher.is_symlink()
                    or _sha256_file(canonical_launcher)
                    != expected_collision_hash
                ):
                    raise ProofError(
                        label
                        + " synthetic launcher collision changed before "
                        "cleanup"
                    )
                canonical_launcher.unlink()
                if _active_fingerprint(layout) != baseline:
                    raise ProofError(
                        label
                        + " launcher collision cleanup did not restore rc.4"
                    )
            (
                launcher_different_collision_error,
                launcher_identical_collision_error,
            ) = launcher_errors
            if counters["candidate_mutation_phase_entries"] != 0:
                raise ProofError(
                    "launcher collision probes entered candidate mutation"
                )
            phases[
                "unowned_canonical_launcher_collisions_refused"
            ] = True

            forward = forward_updater.update(
                tag="v" + candidate_version,
                archive=candidate["source"],
                checksum_file=Path(
                    candidate_checksum_file
                ).resolve(strict=True),
                expected_sha256=candidate["sha256"],
            )
            install_result = forward.get("install", {})
            transaction = forward.get("transaction", {})
            if (
                forward.get("ok") is not True
                or forward.get("previous_version")
                != MULTISESSION_PRIOR_VERSION
                or forward.get("version") != candidate_version
                or forward.get("archive_sha256")
                != candidate["sha256"]
                or install_result.get("registration_migrated")
                is not True
                or install_result.get("codex_server")
                != _CANONICAL_SERVER_NAME
                or install_result.get("former_codex_server")
                != _FORMER_SERVER_NAME
                or transaction.get("action") != "update"
                or transaction.get("previous_version")
                != MULTISESSION_PRIOR_VERSION
                or transaction.get("current_version")
                != candidate_version
            ):
                raise ProofError(
                    "rc.4 to multisession transition receipt is invalid"
                )
            if counters["candidate_mutation_phase_entries"] != 1:
                raise ProofError(
                    "successful update did not enter one mutation phase"
                )

            expected_forward_config = baseline_config.replace(
                former_block, canonical_block, 1
            )
            if (
                config_path.read_bytes() != expected_forward_config
                or expected_forward_config.count(
                    _CANONICAL_SERVER_HEADER
                )
                != 1
                or expected_forward_config.count(
                    _FORMER_SERVER_HEADER
                )
                != 0
            ):
                raise ProofError(
                    "public registration did not migrate exactly once"
                )
            candidate_state = json.loads(
                layout.install_state.read_text(encoding="utf-8")
            )
            migration = candidate_state.get("codex", {}).get(
                "registration_migration"
            )
            if (
                candidate_state.get("version") != candidate_version
                or candidate_state.get("codex", {}).get("table")
                != "[mcp_servers." + _CANONICAL_SERVER_NAME + "]"
                or not isinstance(migration, Mapping)
                or migration.get("from") != _FORMER_SERVER_NAME
                or migration.get("to") != _CANONICAL_SERVER_NAME
                or migration.get("source_block_sha256")
                != hashlib.sha256(former_block).hexdigest()
                or migration.get("source_config_sha256")
                != hashlib.sha256(baseline_config).hexdigest()
            ):
                raise ProofError(
                    "candidate registration migration receipt is invalid"
                )
            migration_backup = (
                candidate_module._validate_registration_migration_backup(
                    candidate_layout, migration
                )
            )
            if migration_backup.read_bytes() != baseline_config:
                raise ProofError(
                    "registration migration backup changed former config"
                )
            migration_backup_sha256 = _sha256_file(
                migration_backup
            )
            phases["forward_registration_migration_receipted"] = True

            _assert_candidate_catalog_contract(
                layout, candidate_package
            )
            candidate_retained = (
                layout.packages / candidate_version
            )
            retained_module = _load_installer(candidate_retained)
            retained_layout = retained_module.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            candidate_manager = _proof_installer(
                retained_module,
                candidate_retained,
                retained_layout,
                counters,
            )
            candidate_doctor = candidate_manager.doctor()
            _assert_doctor_healthy(
                candidate_doctor, "multisession post-update"
            )
            candidate_updater_module = (
                retained_module._load_release_updater_module()
            )
            candidate_updater = (
                candidate_updater_module.ReleaseUpdater(
                    candidate_manager,
                    candidate_factory=_candidate_factory(
                        candidate_updater_module, counters
                    ),
                    platform_check=lambda: None,
                    runtime_check=lambda: None,
                )
            )
            status = candidate_updater.status()
            if (
                status.get("ok") is not True
                or status.get("installed_version")
                != candidate_version
                or status.get("rollback", {}).get("available")
                is not True
                or status.get("rollback", {}).get(
                    "target_version"
                )
                != MULTISESSION_PRIOR_VERSION
                or status.get("rollback", {}).get(
                    "requires_accept_current_version"
                )
                != candidate_version
            ):
                raise ProofError(
                    "candidate does not expose exact rc.4 rollback"
                )
            phases["candidate_doctor_and_rollback_target"] = True

            reverse = candidate_updater.rollback(
                to_version=MULTISESSION_PRIOR_VERSION,
                accept_current_version=candidate_version,
            )
            reverse_transaction = reverse.get("transaction", {})
            if (
                reverse.get("ok") is not True
                or reverse.get("previous_version")
                != candidate_version
                or reverse.get("version")
                != MULTISESSION_PRIOR_VERSION
                or reverse_transaction.get("action") != "rollback"
                or reverse_transaction.get("previous_version")
                != candidate_version
                or reverse_transaction.get("current_version")
                != MULTISESSION_PRIOR_VERSION
                or candidate_updater.interrupted_update_status().get(
                    "present"
                )
                is not False
            ):
                raise ProofError(
                    "multisession to rc.4 rollback receipt is invalid"
                )
            phases["rollback_receipted"] = True

            if _active_fingerprint(layout) != baseline:
                raise ProofError(
                    "rollback did not restore exact active bytes and modes"
                )
            if (
                config_path.read_bytes() != baseline_config
                or config_path.read_bytes().count(
                    _FORMER_SERVER_HEADER
                )
                != 1
                or config_path.read_bytes().count(
                    _CANONICAL_SERVER_HEADER
                )
                != 0
                or _file_fingerprint(v1_plugin) != v1_fingerprint
            ):
                raise ProofError(
                    "rollback did not restore the exact former boundary"
                )
            phases["active_bytes_modes_and_former_name_restored"] = True

            prior_retained = (
                layout.packages / MULTISESSION_PRIOR_VERSION
            )
            candidate_updater_module._preverify_candidate_package(
                prior_retained,
                MULTISESSION_PRIOR_VERSION,
                expected_manifest_sha256=(
                    MULTISESSION_PRIOR_MANIFEST_SHA256
                ),
            )
            restored_module = _load_installer(prior_retained)
            restored_layout = restored_module.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            restored_manager = _proof_installer(
                restored_module,
                prior_retained,
                restored_layout,
                counters,
            )
            restored_doctor = restored_manager.doctor()
            _assert_doctor_healthy(
                restored_doctor, "restored 0.4.0-rc.4"
            )
            if (
                restored_doctor.get("version")
                != MULTISESSION_PRIOR_VERSION
            ):
                raise ProofError(
                    "restored rc.4 doctor version drifted"
                )
            phases["restored_prior_package_and_doctor"] = True

            uninstalled = restored_manager.uninstall()
            if uninstalled.get("ok") is not True:
                raise ProofError("restored rc.4 uninstall failed")
            recovery = Path(
                str(uninstalled.get("support_recovery", ""))
            )
            try:
                recovery.resolve(strict=True).relative_to(
                    root.resolve(strict=True)
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ProofError(
                    "uninstall recovery escaped the disposable root"
                ) from exc
            if (
                config_path.read_bytes() != initial_config
                or _file_fingerprint(v1_plugin) != v1_fingerprint
                or layout.plugin_target.exists()
                or layout.support_root.exists()
            ):
                raise ProofError(
                    "uninstall did not restore the original boundary"
                )
            phases["uninstall_and_v1_restore"] = True

    return {
        "ok": True,
        "format": (
            "roblox-studio-mcp-multisession-migration-rollback-proof"
        ),
        "schema_version": 1,
        "source": {
            "commit": source_commit,
            "tree": source_tree,
        },
        "baseline": {
            "version": MULTISESSION_PRIOR_VERSION,
            "archive_sha256": prior["sha256"],
            "manifest_sha256": MULTISESSION_PRIOR_MANIFEST_SHA256,
            "installer_sha256": MULTISESSION_PRIOR_INSTALLER_SHA256,
            "updater_sha256": MULTISESSION_PRIOR_UPDATER_SHA256,
            "bootstrap_sha256": MULTISESSION_PRIOR_BOOTSTRAP_SHA256,
            "registration": _FORMER_SERVER_NAME,
        },
        "candidate": {
            "version": candidate_version,
            "archive_sha256": candidate["sha256"],
            "manifest_file_count": len(
                candidate["manifest"]["files"]
            ),
            "registration": _CANONICAL_SERVER_NAME,
        },
        "transition": {
            "forward": (
                MULTISESSION_PRIOR_VERSION
                + "->"
                + candidate_version
            ),
            "rollback": (
                candidate_version
                + "->"
                + MULTISESSION_PRIOR_VERSION
            ),
            "active_file_count": active_file_count,
            "exact_bytes_and_modes_restored": True,
            "single_registration_migrated": True,
            "former_registration_restored": True,
            "migration_backup_sha256": migration_backup_sha256,
        },
        "negative_tests": {
            "dual_registration_refused_before_mutation": True,
            "canonical_alias_reuse_refused_before_mutation": True,
            "different_launcher_refused_before_mutation": True,
            "identical_launcher_refused_before_mutation": True,
            "launcher_collision_mutation_phase_entries": 0,
            "dual_refusal": dual_collision_error,
            "alias_refusal": alias_collision_error,
            "different_launcher_refusal": (
                launcher_different_collision_error
            ),
            "identical_launcher_refusal": (
                launcher_identical_collision_error
            ),
        },
        "phases": phases,
        "isolation": {
            "temporary_home_removed": True,
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
            "candidate_mutation_phase_entries": counters[
                "candidate_mutation_phase_entries"
            ],
        },
    }


def prove_cross_version_rollback(
    *,
    restore_bundle: Path,
    prior_archive: Path,
    prior_checksum_file: Path,
    candidate_archive: Path,
    candidate_checksum_file: Path,
    candidate_expected_sha256: str,
    candidate_version: str,
    source_commit: str,
    source_tree: str,
    temporary_parent: Optional[Path] = None,
) -> Dict[str, Any]:
    """Prove exact active-byte rollback with both real portable releases."""

    if candidate_version == RC4_VERSION:
        raise ProofError("candidate version must differ from rc.4")
    for label, value in (
        ("candidate source commit", source_commit),
        ("candidate source tree", source_tree),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in _SHA256 for character in value)
        ):
            raise ProofError(label + " is not a lowercase Git object ID")

    restore = _verify_restore_bundle(restore_bundle)
    prior = _archive_manifest(
        prior_archive,
        checksum_file=prior_checksum_file,
        expected_sha256=RC4_ARCHIVE_SHA256,
        expected_version=RC4_VERSION,
    )
    candidate = _archive_manifest(
        candidate_archive,
        checksum_file=candidate_checksum_file,
        expected_sha256=candidate_expected_sha256,
        expected_version=candidate_version,
    )
    bundled_prior = (
        restore["root"]
        / "artifacts"
        / (
            "roblox-studio-mcp-v2-"
            + RC4_VERSION
            + "-macos-arm64.tar.gz"
        )
    )
    if _sha256_file(bundled_prior) != prior["sha256"]:
        raise ProofError("selected prior archive is not the restore archive")

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
        "restore_bundle_verified": True,
        "prior_archive_verified": True,
        "candidate_archive_verified": True,
    }
    active_file_count = 0
    catalog_receipt_count = 0
    disposable_rc4_catalog_sha256 = ""
    with tempfile.TemporaryDirectory(
        prefix="studio-mcp-v2-cross-version-proof-",
        dir=None if parent is None else str(parent),
    ) as temporary:
        root = Path(temporary)
        prior_package = _extract_audited_archive(
            prior["source"], root / "prior-package"
        )
        candidate_package = _extract_audited_archive(
            candidate["source"], root / "candidate-package"
        )
        prior_module = _load_installer(prior_package)
        if getattr(prior_module, "VERSION", None) != RC4_VERSION:
            raise ProofError("rc.4 installer version drifted")
        layout_type = getattr(prior_module, "InstallLayout", None)
        if layout_type is None:
            raise ProofError("rc.4 package lacks InstallLayout")

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
        )
        config_path = codex / "config.toml"
        config_path.write_bytes(initial_config)
        v1_plugin = plugins / "MCPStudioPlugin.rbxm"
        v1_plugin.write_bytes(b"synthetic v1 fallback sentinel\n")
        v1_fingerprint = _file_fingerprint(v1_plugin)

        layout = layout_type.for_user(home=home)
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
                or installed.get("version") != RC4_VERSION
            ):
                raise ProofError("actual rc.4 isolated install failed")
            _assert_doctor_healthy(
                prior_manager.doctor(), "rc.4 pre-update"
            )
            phases["rc4_install_and_doctor"] = True

            upstream = Path(restore["upstream"])
            imported = prior_manager.catalog_import(
                upstream, _sha256_file(upstream)
            )
            if imported.get("ok") is not True:
                raise ProofError(
                    "actual rc.4 catalog import did not acknowledge success"
                )
            disposable_rc4_catalog_sha256 = (
                _assert_rc4_catalog_contract(layout)
            )
            receipts = {
                path.name: _file_fingerprint(path)
                for path in sorted(
                    layout.config.glob(
                        "catalog-import-receipt-*.json"
                    )
                )
            }
            if not receipts:
                raise ProofError(
                    "rc.4 baseline import did not retain an audit receipt"
                )
            catalog_receipt_count = len(receipts)
            phases["rc4_catalog_state_recreated"] = True

            baseline = _active_fingerprint(layout)
            active_file_count = len(baseline)
            if _file_fingerprint(v1_plugin) != v1_fingerprint:
                raise ProofError("rc.4 install changed the v1 plugin")

            forward_factory = _candidate_factory(
                prior_updater_module, counters
            )
            forward_updater = prior_updater_module.ReleaseUpdater(
                prior_manager,
                candidate_factory=forward_factory,
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
            if (
                forward.get("ok") is not True
                or forward.get("previous_version") != RC4_VERSION
                or forward.get("version") != candidate_version
                or forward.get("archive_sha256")
                != candidate["sha256"]
            ):
                raise ProofError(
                    "rc.4 to candidate receipt identity is invalid"
                )
            _assert_candidate_catalog_contract(
                layout, candidate_package
            )
            for name, fingerprint in receipts.items():
                if (
                    _file_fingerprint(layout.config / name)
                    != fingerprint
                ):
                    raise ProofError(
                        "candidate update changed the rc.4 import receipt"
                    )
            phases["forward_update_receipted"] = True

            candidate_retained = (
                layout.packages / candidate_version
            )
            candidate_module = _load_installer(candidate_retained)
            candidate_layout = candidate_module.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            candidate_manager = _proof_installer(
                candidate_module,
                candidate_retained,
                candidate_layout,
                counters,
            )
            candidate_doctor = candidate_manager.doctor()
            _assert_doctor_healthy(
                candidate_doctor, "candidate post-update"
            )
            if candidate_doctor.get("version") != candidate_version:
                raise ProofError("candidate doctor version drifted")
            candidate_updater_module = (
                candidate_module._load_release_updater_module()
            )
            candidate_updater = (
                candidate_updater_module.ReleaseUpdater(
                    candidate_manager,
                    candidate_factory=_candidate_factory(
                        candidate_updater_module, counters
                    ),
                    platform_check=lambda: None,
                    runtime_check=lambda: None,
                )
            )
            forward_status = candidate_updater.status()
            if (
                forward_status.get("ok") is not True
                or forward_status.get("installed_version")
                != candidate_version
                or forward_status.get("rollback", {}).get(
                    "available"
                )
                is not True
                or forward_status.get("rollback", {}).get(
                    "target_version"
                )
                != RC4_VERSION
                or forward_status.get("rollback", {}).get(
                    "requires_accept_current_version"
                )
                != candidate_version
            ):
                raise ProofError(
                    "candidate status does not expose exact rc.4 rollback"
                )
            phases["candidate_doctor_and_rollback_target"] = True

            reverse = candidate_updater.rollback(
                to_version=RC4_VERSION,
                accept_current_version=candidate_version,
            )
            if (
                reverse.get("ok") is not True
                or reverse.get("previous_version")
                != candidate_version
                or reverse.get("version") != RC4_VERSION
            ):
                raise ProofError(
                    "candidate to rc.4 rollback receipt is invalid"
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

            if _active_fingerprint(layout) != baseline:
                raise ProofError(
                    "rollback did not restore exact active bytes and modes"
                )
            if (
                config_path.read_bytes()[: len(initial_config)]
                != initial_config
                or _file_fingerprint(v1_plugin) != v1_fingerprint
            ):
                raise ProofError(
                    "rollback changed the original v1 boundary"
                )
            _assert_rc4_catalog_contract(
                layout,
                expected_effective_sha256=(
                    disposable_rc4_catalog_sha256
                ),
            )
            if (
                layout.plugin_artifact.read_bytes()
                != layout.plugin_target.read_bytes()
            ):
                raise ProofError(
                    "restored plugin artifact and target differ"
                )
            phases["active_bytes_and_modes_restored"] = True

            candidate_updater_module._preverify_candidate_package(
                layout.packages / RC4_VERSION, RC4_VERSION
            )
            candidate_updater_module._preverify_candidate_package(
                layout.packages / candidate_version,
                candidate_version,
            )
            stable_lock = (
                candidate_updater_module._stable_update_lock_path(
                    layout
                )
            )
            if (
                stable_lock.is_symlink()
                or not stable_lock.is_file()
                or stat.S_IMODE(stable_lock.stat().st_mode) != 0o600
            ):
                raise ProofError(
                    "stable release lock is not a private regular file"
                )
            phases["retained_packages_and_lock_verified"] = True

            restored_module = _load_installer(
                layout.packages / RC4_VERSION
            )
            restored_layout = restored_module.InstallLayout(
                home=layout.home,
                support_root=layout.support_root,
                codex_config=layout.codex_config,
                studio_plugins=layout.studio_plugins,
            )
            restored_manager = _proof_installer(
                restored_module,
                layout.packages / RC4_VERSION,
                restored_layout,
                counters,
            )
            restored_doctor = restored_manager.doctor()
            _assert_doctor_healthy(
                restored_doctor, "restored rc.4"
            )
            if restored_doctor.get("version") != RC4_VERSION:
                raise ProofError("restored rc.4 doctor version drifted")
            phases["restored_rc4_doctor"] = True

            uninstalled = restored_manager.uninstall()
            if uninstalled.get("ok") is not True:
                raise ProofError("restored rc.4 uninstall failed")
            recovery = Path(
                str(uninstalled.get("support_recovery", ""))
            )
            try:
                recovery.resolve(strict=True).relative_to(
                    root.resolve(strict=True)
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ProofError(
                    "uninstall recovery escaped the disposable root"
                ) from exc
            if (
                config_path.read_bytes() != initial_config
                or _file_fingerprint(v1_plugin) != v1_fingerprint
                or layout.plugin_target.exists()
                or layout.support_root.exists()
            ):
                raise ProofError(
                    "uninstall did not restore the exact original boundary"
                )
            phases["uninstall_and_v1_restore"] = True

    return {
        "ok": True,
        "format": (
            "roblox-studio-mcp-v2-cross-version-rollback-proof"
        ),
        "schema_version": 1,
        "source": {
            "commit": source_commit,
            "tree": source_tree,
        },
        "baseline": {
            "version": RC4_VERSION,
            "archive_sha256": prior["sha256"],
            "bootstrap_sha256": RC4_BOOTSTRAP_SHA256,
            "live_plugin_sha256": RC4_LIVE_PLUGIN_SHA256,
            "source_commit": RC4_SOURCE_COMMIT,
            "source_tree": RC4_SOURCE_TREE,
            "tag_object": RC4_TAG_OBJECT,
            "restore_manifest_sha256": (
                RC4_RESTORE_MANIFEST_SHA256
            ),
            "restore_file_count": restore["file_count"],
            "effective_catalog_sha256": (
                RC4_EFFECTIVE_CATALOG_SHA256
            ),
            "upstream_catalog_sha256": (
                RC4_UPSTREAM_CATALOG_SHA256
            ),
            "compatibility_sha256": RC4_COMPATIBILITY_SHA256,
            "disposable_effective_catalog_sha256": (
                disposable_rc4_catalog_sha256
            ),
        },
        "candidate": {
            "version": candidate_version,
            "archive_sha256": candidate["sha256"],
            "manifest_file_count": len(
                candidate["manifest"]["files"]
            ),
        },
        "transition": {
            "forward": RC4_VERSION + "->" + candidate_version,
            "rollback": candidate_version + "->" + RC4_VERSION,
            "active_file_count": active_file_count,
            "catalog_receipt_count": catalog_receipt_count,
            "exact_bytes_and_modes_restored": True,
        },
        "phases": phases,
        "isolation": {
            "temporary_home_removed": True,
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
