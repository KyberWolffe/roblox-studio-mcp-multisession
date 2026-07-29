#!/usr/bin/env python3
"""Fail-closed side-by-side harness for candidate read-only live gates.

The client API accepts public ``*_v2`` tool names.  Durable remote handler
names are inspected only to audit the candidate catalog; the catalog remains
the sole authority that maps a public request to a Studio dispatch.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import secrets
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.native_compile import (
    DEFAULT_STUDIO_EXECUTABLE,
    NativeCompileError,
    extract_exact_main_source,
    validate_native_compile_receipt,
)
from release_tools.installer import (
    InstallError,
    verify_release_package,
)


PUBLIC_READ_ONLY_TO_REMOTE = {
    "studio_get_state_v2": "studio_get_state",
    "studio_list_tree_v2": "studio_list_tree",
    "studio_search_scripts_v2": "studio_search_scripts",
    "studio_grep_scripts_v2": "studio_grep_scripts",
    "studio_inspect_instance_v2": "studio_inspect_instance",
}
PUBLIC_READ_ONLY_TOOLS = frozenset(PUBLIC_READ_ONLY_TO_REMOTE)
JOB_PUBLIC_TOOLS = frozenset(
    {"start_studio_job_v2", "get_studio_job_v2"}
)
ALLOWED_PUBLIC_SCOPES = PUBLIC_READ_ONLY_TOOLS | JOB_PUBLIC_TOOLS
STATE_FILENAME = "gate-state.json"
NATIVE_RECEIPT_FILENAME = "native-studio-compile-proof.json"
NATIVE_QUALIFICATION_FILENAME = "native-qualification.json"
CLEANUP_IDENTITY_FILENAME = "cleanup-identity.json"
CLEANUP_BROKER_FILENAME = "cleanup-broker.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_TOOL_ARGUMENT_BYTES = 1_000_000
MAX_JOB_TIMEOUT_MS = 120_000
MAX_GATE_RECORD_BYTES = 2_000_000
MAX_RELEASE_FILE_BYTES = 4_000_000
MAX_RELEASE_TOTAL_BYTES = 32_000_000
MAX_RELEASE_FILES = 512
GATE_STATE_FORMAT = "roblox-studio-mcp-v2-live-readonly-gate"
NATIVE_QUALIFICATION_FORMAT = (
    "roblox-studio-mcp-v2-live-readonly-native-qualification"
)
CLEANUP_IDENTITY_FORMAT = (
    "roblox-studio-mcp-v2-live-readonly-cleanup-identity"
)
CLEANUP_BROKER_FORMAT = (
    "roblox-studio-mcp-v2-live-readonly-cleanup-broker"
)
GATE_STATE_KEYS = frozenset(
    {
        "format",
        "version",
        "payload_root",
        "support_root",
        "plugin_path",
        "plugin_sha256",
        "plugin_source_sha256",
        "native_compile_receipt_path",
        "release_manifest_sha256",
        "cleanup_identity_sha256",
        "durable_catalog_file_sha256",
        "runtime_config_sha256",
        "secrets_config_sha256",
        "upstream_catalog_file_sha256",
        "upstream_compatibility_file_sha256",
        "port",
        "run_id",
        "allowed_tools",
        "public_to_remote",
    }
)
CLEANUP_IDENTITY_KEYS = frozenset(
    {
        "format",
        "version",
        "port",
        "run_id",
        "release_manifest_sha256",
        "durable_catalog_file_sha256",
        "runtime_config_sha256",
        "secrets_config_sha256",
    }
)
CLEANUP_BROKER_KEYS = frozenset(
    {
        "format",
        "cleanup_identity_sha256",
        "version",
        "port",
        "run_id",
        "catalog_sha256",
        "broker_instance_id",
        "pid",
        "started_at",
    }
)
NATIVE_QUALIFICATION_KEYS = frozenset(
    {
        "format",
        "gate_state_sha256",
        "plugin_sha256",
        "plugin_source_sha256",
        "receipt_path",
        "receipt_sha256",
        "studio_executable_path",
        "studio_executable_sha256",
    }
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_json_object(
    pairs: Iterable[Tuple[str, Any]],
) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeError(
                "input JSON must not contain duplicate JSON keys"
            )
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise RuntimeError(
        "input JSON must not contain non-finite numbers"
    )


def read_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path.name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} is not a JSON object")
    return value


def read_bounded_record(
    path: Path, label: str, expected_mode: int
) -> Tuple[bytes, Dict[str, Any]]:
    raw = read_regular_bytes(
        path,
        label,
        expected_mode,
        MAX_GATE_RECORD_BYTES,
    )
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(label + " is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(label + " is not a JSON object")
    return raw, value


def read_regular_bytes(
    path: Path,
    label: str,
    expected_mode: int,
    maximum: int,
    *,
    minimum: int = 1,
) -> bytes:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(label + " is missing") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or stat.S_IMODE(details.st_mode) != expected_mode
        or not minimum <= details.st_size <= maximum
    ):
        raise RuntimeError(label + " is not an exact bounded file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != expected_mode
            or not minimum
            <= before.st_size
            <= maximum
        ):
            raise RuntimeError(
                label + " changed filesystem identity"
            )
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(
                descriptor, min(1024 * 1024, remaining)
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(
            getattr(details, field) != getattr(before, field)
            for field in stable_fields
        )
        or
        len(raw) != before.st_size
        or len(raw) > maximum
        or any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        )
    ):
        raise RuntimeError(label + " changed while it was read")
    return raw


def read_private_record(
    path: Path, label: str
) -> Tuple[bytes, Dict[str, Any]]:
    return read_bounded_record(path, label, 0o600)


def validate_explicit_studio_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise RuntimeError(
            "studio_id must be an explicit canonical lowercase UUID"
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise RuntimeError(
            "studio_id must be an explicit canonical lowercase UUID"
        )
    if str(parsed) != value:
        raise RuntimeError(
            "studio_id must be an explicit canonical lowercase UUID"
        )
    return value


def validate_job_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value != value.strip()
    ):
        raise RuntimeError(
            "job_id must be a non-empty routing-safe string"
        )
    return value


def validate_job_timeout(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_JOB_TIMEOUT_MS
    ):
        raise RuntimeError(
            "timeout_ms must be an integer between 1 and 120000"
        )
    return value


def write_new(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(f"short write for {path}")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _real_directory(
    path: Path,
    label: str,
    *,
    allowed_modes: Iterable[int],
) -> Path:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(label + " is missing") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode)
        not in frozenset(allowed_modes)
    ):
        raise RuntimeError(label + " is not an exact real directory")
    resolved = path.resolve(strict=True)
    return resolved


def _candidate_release_root(extracted_root: Path) -> Path:
    root = _real_directory(
        extracted_root,
        "candidate work root",
        allowed_modes=(0o700, 0o755),
    )
    matches = []
    for entry in root.iterdir():
        try:
            details = entry.lstat()
        except FileNotFoundError:
            raise RuntimeError(
                "candidate work root changed during discovery"
            )
        if (
            stat.S_ISDIR(details.st_mode)
            and not entry.is_symlink()
            and (entry / "payload").exists()
        ):
            matches.append(entry)
    if len(matches) != 1:
        raise RuntimeError(
            "candidate extraction lacks one unique real release root"
        )
    return _real_directory(
        matches[0],
        "candidate release root",
        allowed_modes=(0o700, 0o755),
    )


def _validated_candidate_release(
    extracted_root: Path,
    *,
    expected_version: Optional[str] = None,
    expected_manifest_sha256: Optional[str] = None,
) -> Tuple[Path, str]:
    release_root = _candidate_release_root(extracted_root)
    payload = _real_directory(
        release_root / "payload",
        "candidate payload root",
        allowed_modes=(0o700, 0o755),
    )
    manifest_path = release_root / "release-manifest.json"
    manifest_bytes, strict_manifest = read_bounded_record(
        manifest_path,
        "candidate release manifest",
        0o644,
    )
    manifest_sha256 = sha256_bytes(manifest_bytes)
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise RuntimeError(
            "candidate release manifest bytes drifted"
        )
    if (
        frozenset(strict_manifest)
        != {
            "format",
            "manifest_version",
            "version",
            "platform",
            "product",
            "python_requires",
            "source_date_epoch",
            "files",
        }
        or strict_manifest.get("format")
        != "roblox-studio-mcp-v2-portable-release"
        or strict_manifest.get("manifest_version") != 1
        or strict_manifest.get("platform") != "macos-arm64"
        or strict_manifest.get("product") != "RobloxStudioMCPv2"
        or strict_manifest.get("python_requires") != ">=3.9"
        or strict_manifest.get("source_date_epoch") != 0
        or (
            expected_version is not None
            and strict_manifest.get("version")
            != expected_version
        )
    ):
        raise RuntimeError(
            "candidate release manifest identity drifted"
        )
    files = strict_manifest.get("files")
    if (
        not isinstance(files, list)
        or not 1 <= len(files) <= MAX_RELEASE_FILES
    ):
        raise RuntimeError(
            "candidate release manifest file count drifted"
        )
    expected_files = {"release-manifest.json"}
    expected_directories = {"."}
    total_bytes = len(manifest_bytes)
    for item in files:
        if (
            not isinstance(item, dict)
            or frozenset(item)
            != {"path", "sha256", "size", "mode"}
        ):
            raise RuntimeError(
                "candidate release manifest entry drifted"
            )
        relative = item.get("path")
        mode = item.get("mode")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or Path(relative).as_posix() != relative
            or "." in Path(relative).parts
            or ".." in Path(relative).parts
            or mode not in (0o600, 0o644, 0o700, 0o755)
            or type(size) is not int
            or not 0 <= size <= MAX_RELEASE_FILE_BYTES
            or not SHA256_PATTERN.fullmatch(str(digest))
            or relative in expected_files
        ):
            raise RuntimeError(
                "candidate release manifest entry drifted"
            )
        expected_files.add(relative)
        parent = Path(relative).parent
        while str(parent) != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
        source = release_root / relative
        data = read_regular_bytes(
            source,
            "candidate release file " + relative,
            mode,
            MAX_RELEASE_FILE_BYTES,
            minimum=0,
        )
        if len(data) != size or sha256_bytes(data) != digest:
            raise RuntimeError(
                "candidate release file bytes drifted"
            )
        total_bytes += len(data)
        if total_bytes > MAX_RELEASE_TOTAL_BYTES:
            raise RuntimeError(
                "candidate release total bytes exceed the bound"
            )

    observed_files = set()
    observed_directories = {"."}
    pending = [release_root]
    while pending:
        directory = pending.pop()
        for entry in directory.iterdir():
            details = entry.lstat()
            relative = entry.relative_to(
                release_root
            ).as_posix()
            if entry.is_symlink():
                raise RuntimeError(
                    "candidate release contains a symlink"
                )
            if stat.S_ISDIR(details.st_mode):
                if (
                    stat.S_IMODE(details.st_mode)
                    not in (0o700, 0o755)
                ):
                    raise RuntimeError(
                        "candidate release directory mode drifted"
                    )
                observed_directories.add(relative)
                pending.append(entry)
            elif stat.S_ISREG(details.st_mode):
                observed_files.add(relative)
            else:
                raise RuntimeError(
                    "candidate release contains a special file"
                )
    if (
        observed_files != expected_files
        or observed_directories != expected_directories
    ):
        raise RuntimeError(
            "candidate release extracted tree drifted"
        )
    try:
        verified_manifest = verify_release_package(release_root)
    except (InstallError, OSError, ValueError) as exc:
        raise RuntimeError(
            "candidate release package verification failed"
        ) from exc
    if verified_manifest != strict_manifest:
        raise RuntimeError(
            "candidate release manifest parsing drifted"
        )
    return payload, manifest_sha256


def candidate_payload(extracted_root: Path) -> Path:
    payload, _manifest_sha256 = _validated_candidate_release(
        extracted_root
    )
    return payload


def validate_public_tool_name(tool_name: str) -> str:
    if (
        not isinstance(tool_name, str)
        or tool_name not in PUBLIC_READ_ONLY_TOOLS
    ):
        raise RuntimeError(
            "tool must be an approved public read-only *_v2 name"
        )
    return tool_name


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: Iterable[str],
    label: str,
) -> None:
    expected_keys = frozenset(expected)
    observed_keys = frozenset(value)
    if observed_keys != expected_keys:
        raise RuntimeError(f"{label} field set drifted")


def _require_module_from_payload(
    module: Any, payload: Path, label: str
) -> None:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise RuntimeError(f"{label} module origin is invalid")
    try:
        Path(module_file).resolve(strict=True).relative_to(payload)
    except (OSError, ValueError):
        raise RuntimeError(
            f"{label} module did not originate in candidate payload"
        )


def _require_module_from_project(module: Any, label: str) -> None:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise RuntimeError(f"{label} module origin is invalid")
    try:
        Path(module_file).resolve(strict=True).relative_to(
            PROJECT_ROOT.resolve(strict=True)
        )
    except (OSError, ValueError):
        raise RuntimeError(
            f"{label} module did not originate in the gate source"
        )


def _load_isolated_package(
    package_directory: Path,
    label: str,
    submodules: Iterable[str],
) -> Tuple[Any, Dict[str, Any]]:
    package_init = package_directory / "__init__.py"
    read_regular_bytes(
        package_init,
        label + " package initializer",
        0o644,
        MAX_GATE_RECORD_BYTES,
    )
    package_name = (
        "_roblox_studio_mcp_v2_gate_"
        + uuid.uuid4().hex
    )
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        package_init,
        submodule_search_locations=[str(package_directory)],
    )
    if (
        package_spec is None
        or package_spec.loader is None
    ):
        raise RuntimeError(
            label + " package could not be isolated"
        )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    try:
        package_spec.loader.exec_module(package)
        loaded = {}
        for submodule in submodules:
            if (
                not isinstance(submodule, str)
                or not submodule
                or "." in submodule
            ):
                raise RuntimeError(
                    label + " submodule request is invalid"
                )
            loaded[submodule] = importlib.import_module(
                package_name + "." + submodule
            )
        return package, loaded
    except Exception:
        for module_name in list(sys.modules):
            if (
                module_name == package_name
                or module_name.startswith(package_name + ".")
            ):
                sys.modules.pop(module_name, None)
        raise


def _load_isolated_module(path: Path, label: str) -> Any:
    read_regular_bytes(
        path,
        label,
        0o644,
        MAX_GATE_RECORD_BYTES,
    )
    module_name = (
        "_roblox_studio_mcp_v2_gate_module_"
        + uuid.uuid4().hex
    )
    spec = importlib.util.spec_from_file_location(
        module_name, path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(label + " could not be isolated")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _validate_public_target_schema(
    public_definition: Mapping[str, Any],
    raw_schema: Mapping[str, Any],
) -> None:
    raw_properties = raw_schema.get("properties")
    raw_required = raw_schema.get("required")
    if (
        not isinstance(raw_properties, dict)
        or not isinstance(raw_required, list)
        or "studio_id" in raw_properties
        or "studio_id" in raw_required
    ):
        raise RuntimeError(
            "candidate remote schema contains routing identity"
        )

    public_schema = public_definition.get("inputSchema")
    if not isinstance(public_schema, dict):
        raise RuntimeError(
            "candidate public input schema drifted"
        )
    properties = public_schema.get("properties")
    required = public_schema.get("required")
    target = (
        properties.get("studio_id")
        if isinstance(properties, dict)
        else None
    )
    if (
        not isinstance(target, dict)
        or target.get("type") != "string"
        or target.get("format") != "uuid"
        or not isinstance(required, list)
        or required.count("studio_id") != 1
    ):
        raise RuntimeError(
            "candidate public explicit-target schema drifted"
        )


def audit_catalog_contract(catalog: Any) -> Dict[str, str]:
    """Prove the public/remote bijection without using it for dispatch."""

    exposed = {
        item["name"]: item
        for item in catalog.tools_for_mcp()
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
    }
    observed: Dict[str, str] = {}
    for public_name, expected_remote in (
        PUBLIC_READ_ONLY_TO_REMOTE.items()
    ):
        definition = catalog.get(public_name)
        if definition.public_name != public_name:
            raise RuntimeError(
                "candidate catalog public tool identity drifted"
            )
        if definition.remote_name != expected_remote:
            raise RuntimeError(
                "candidate catalog public/remote mapping drifted"
            )
        if catalog.public_for_remote(expected_remote) != public_name:
            raise RuntimeError(
                "candidate catalog public/remote mapping is not bijective"
            )
        public_definition = exposed.get(public_name)
        if not isinstance(public_definition, dict):
            raise RuntimeError(
                "candidate public tool exposure drifted"
            )
        _validate_public_target_schema(
            public_definition, definition.input_schema
        )
        annotations = (
            public_definition.get("annotations")
            if isinstance(public_definition, dict)
            else None
        )
        if (
            not isinstance(annotations, dict)
            or annotations.get("readOnlyHint") is not True
            or annotations.get("destructiveHint") is not False
            or annotations.get("idempotentHint") is not True
            or annotations.get("openWorldHint") is not False
        ):
            raise RuntimeError(
                "candidate read-only annotation contract drifted"
            )
        observed[public_name] = expected_remote
    if frozenset(observed) != PUBLIC_READ_ONLY_TOOLS:
        raise RuntimeError("candidate public read-only scope drifted")
    if any(
        remote_name in exposed
        for remote_name in PUBLIC_READ_ONLY_TO_REMOTE.values()
    ):
        raise RuntimeError(
            "candidate catalog exposed a remote handler name publicly"
        )
    return observed


def build_runtime(port: int, catalog_path: Path) -> Dict[str, Any]:
    if type(port) is not int or not 1 <= port <= 65_535:
        raise RuntimeError("candidate gate port is invalid")
    return {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": port,
        "catalog": str(catalog_path),
        "allowed_studios": ["*"],
        # Authorization applies to public frontend names. Remote handlers
        # never appear in this scope.
        "allowed_tools": sorted(ALLOWED_PUBLIC_SCOPES),
        "startup_timeout_seconds": 10.0,
    }


def _validated_gate_state(
    work_root: Path, state: Mapping[str, Any]
) -> Tuple[Path, Path, Path, Path, Path]:
    _require_exact_keys(state, GATE_STATE_KEYS, STATE_FILENAME)
    if state.get("format") != GATE_STATE_FORMAT:
        raise RuntimeError("gate state format drifted")
    version = state.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError("gate state version is invalid")
    if (
        type(state.get("port")) is not int
        or not 1 <= state["port"] <= 65_535
    ):
        raise RuntimeError("gate state port is invalid")
    if not RUN_ID_PATTERN.fullmatch(str(state.get("run_id", ""))):
        raise RuntimeError("gate state run_id is invalid")
    for field in (
        "plugin_sha256",
        "plugin_source_sha256",
        "release_manifest_sha256",
        "cleanup_identity_sha256",
        "durable_catalog_file_sha256",
        "runtime_config_sha256",
        "secrets_config_sha256",
        "upstream_catalog_file_sha256",
        "upstream_compatibility_file_sha256",
    ):
        if not SHA256_PATTERN.fullmatch(str(state.get(field, ""))):
            raise RuntimeError(f"gate state {field} is invalid")
    if state.get("allowed_tools") != sorted(ALLOWED_PUBLIC_SCOPES):
        raise RuntimeError("gate state public tool scope drifted")
    if state.get("public_to_remote") != PUBLIC_READ_ONLY_TO_REMOTE:
        raise RuntimeError("gate state catalog mapping audit drifted")

    expected_payload, _manifest_sha256 = (
        _validated_candidate_release(
            work_root,
            expected_version=version,
            expected_manifest_sha256=state[
                "release_manifest_sha256"
            ],
        )
    )
    payload = Path(str(state["payload_root"]))
    if payload != expected_payload:
        raise RuntimeError("gate state candidate payload drifted")
    expected_support = _require_private_directory(
        work_root / "support",
        "candidate support root",
    )
    support = Path(str(state["support_root"]))
    if support != expected_support:
        raise RuntimeError("gate state support root drifted")
    expected_plugin = (
        work_root / "StudioMCPv2CandidateReadOnly.rbxmx"
    )
    plugin_path = Path(str(state["plugin_path"]))
    if plugin_path != expected_plugin:
        raise RuntimeError("gate state candidate plugin path drifted")
    package_bytes = read_regular_bytes(
        plugin_path,
        "candidate plugin",
        0o600,
        2_000_000,
    )
    if sha256_bytes(package_bytes) != state["plugin_sha256"]:
        raise RuntimeError("candidate plugin bytes drifted")
    try:
        extracted_package, source_bytes = extract_exact_main_source(
            plugin_path
        )
    except NativeCompileError as exc:
        raise RuntimeError(
            "candidate plugin source contract drifted"
        ) from exc
    if (
        extracted_package != package_bytes
        or
        sha256_bytes(source_bytes)
        != state["plugin_source_sha256"]
    ):
        raise RuntimeError("candidate plugin Main source drifted")

    expected_receipt = work_root / NATIVE_RECEIPT_FILENAME
    supplied_receipt = Path(
        str(state["native_compile_receipt_path"])
    )
    if (
        supplied_receipt.parent != work_root
        or supplied_receipt.name != NATIVE_RECEIPT_FILENAME
        or supplied_receipt != expected_receipt
    ):
        raise RuntimeError(
            "candidate native compile receipt path drifted"
        )

    config_root = _require_private_directory(
        support / "config",
        "candidate config root",
    )
    catalog_path = config_root / "tool-catalog.json"
    config_contracts = (
        (
            catalog_path,
            "candidate runtime catalog",
            0o644,
            "durable_catalog_file_sha256",
        ),
        (
            config_root / "runtime.json",
            "candidate runtime config",
            0o644,
            "runtime_config_sha256",
        ),
        (
            config_root / "secrets.json",
            "candidate secret config",
            0o600,
            "secrets_config_sha256",
        ),
        (
            config_root
            / "upstream-known-tool-catalog.json",
            "candidate upstream catalog",
            0o644,
            "upstream_catalog_file_sha256",
        ),
        (
            config_root
            / "upstream-compatibility-map.json",
            "candidate upstream compatibility map",
            0o644,
            "upstream_compatibility_file_sha256",
        ),
    )
    for path, label, mode, state_field in config_contracts:
        raw, _value = read_bounded_record(path, label, mode)
        if sha256_bytes(raw) != state[state_field]:
            raise RuntimeError(label + " bytes drifted")
    cleanup_identity_bytes, _cleanup_identity = (
        read_private_record(
            work_root / CLEANUP_IDENTITY_FILENAME,
            "candidate cleanup identity",
        )
    )
    if (
        sha256_bytes(cleanup_identity_bytes)
        != state["cleanup_identity_sha256"]
    ):
        raise RuntimeError(
            "candidate cleanup identity bytes drifted"
        )
    return (
        payload,
        support,
        plugin_path,
        catalog_path,
        expected_receipt,
    )


def native_qualification_status(
    work_root: Path,
    state: Mapping[str, Any],
    plugin_path: Path,
    receipt_path: Path,
) -> Dict[str, Any]:
    qualification_path = (
        work_root / NATIVE_QUALIFICATION_FILENAME
    )
    try:
        state_bytes, observed_state = read_private_record(
            work_root / STATE_FILENAME,
            "candidate gate state",
        )
        if observed_state != dict(state):
            raise RuntimeError(
                "candidate gate state changed after validation"
            )
        qualification_bytes, qualification = (
            read_private_record(
                qualification_path,
                "candidate native qualification",
            )
        )
        _require_exact_keys(
            qualification,
            NATIVE_QUALIFICATION_KEYS,
            "candidate native qualification",
        )
        for field in (
            "gate_state_sha256",
            "plugin_sha256",
            "plugin_source_sha256",
            "receipt_sha256",
            "studio_executable_sha256",
        ):
            if not SHA256_PATTERN.fullmatch(
                str(qualification.get(field, ""))
            ):
                raise RuntimeError(
                    "candidate native qualification hash drifted"
                )
        if (
            qualification["format"]
            != NATIVE_QUALIFICATION_FORMAT
            or qualification["gate_state_sha256"]
            != sha256_bytes(state_bytes)
            or qualification["plugin_sha256"]
            != state["plugin_sha256"]
            or qualification["plugin_source_sha256"]
            != state["plugin_source_sha256"]
            or qualification["receipt_path"]
            != str(receipt_path)
        ):
            raise RuntimeError(
                "candidate native qualification identity drifted"
            )
        receipt_bytes, _receipt_record = read_private_record(
            receipt_path,
            "candidate native compile receipt",
        )
        if (
            qualification["receipt_sha256"]
            != sha256_bytes(receipt_bytes)
        ):
            raise RuntimeError(
                "candidate native compile receipt bytes drifted"
            )
        studio_executable = DEFAULT_STUDIO_EXECUTABLE.resolve(
            strict=True
        )
        if (
            qualification["studio_executable_path"]
            != str(studio_executable)
        ):
            raise RuntimeError(
                "candidate native Studio path anchor drifted"
            )
        receipt = validate_native_compile_receipt(
            receipt_path,
            package_path=plugin_path,
            expected_package_sha256=state["plugin_sha256"],
            expected_source_sha256=state[
                "plugin_source_sha256"
            ],
            studio_executable=studio_executable,
        )
        studio = receipt.get("studio")
        if (
            not isinstance(studio, dict)
            or studio.get("executable_path")
            != str(studio_executable.resolve(strict=True))
            or studio.get("executable_sha256")
            != qualification["studio_executable_sha256"]
        ):
            raise RuntimeError(
                "candidate native Studio identity drifted"
            )
        return {
            "ok": True,
            "qualification_path": str(qualification_path),
            "qualification_sha256": sha256_bytes(
                qualification_bytes
            ),
            "receipt_path": str(receipt_path),
            "receipt_sha256": qualification[
                "receipt_sha256"
            ],
            "studio_executable_path": studio[
                "executable_path"
            ],
            "studio_executable_sha256": studio[
                "executable_sha256"
            ],
        }
    except (
        NativeCompileError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        return {
            "ok": False,
            "qualification_path": str(qualification_path),
            "receipt_path": str(receipt_path),
            "error": str(exc),
        }


def require_native_qualification(
    work_root: Path,
    state: Mapping[str, Any],
    plugin_path: Path,
    receipt_path: Path,
) -> Dict[str, Any]:
    result = native_qualification_status(
        work_root,
        state,
        plugin_path,
        receipt_path,
    )
    if result.get("ok") is not True:
        raise RuntimeError(
            "candidate native compilation is not qualified: "
            + str(result.get("error", "unknown failure"))
        )
    return result


def qualify_native(work_root: Path) -> Dict[str, Any]:
    root = _real_directory(
        work_root,
        "candidate work root",
        allowed_modes=(0o700, 0o755),
    )
    state_bytes, state = read_private_record(
        root / STATE_FILENAME,
        "candidate gate state",
    )
    (
        _payload,
        _support,
        plugin_path,
        _catalog_path,
        receipt_path,
    ) = _validated_gate_state(root, state)
    receipt = validate_native_compile_receipt(
        receipt_path,
        package_path=plugin_path,
        expected_package_sha256=state["plugin_sha256"],
        expected_source_sha256=state[
            "plugin_source_sha256"
        ],
    )
    receipt_bytes, _receipt_record = read_private_record(
        receipt_path,
        "candidate native compile receipt",
    )
    studio = receipt.get("studio")
    if not isinstance(studio, dict):
        raise RuntimeError(
            "candidate native compile receipt lacks Studio identity"
        )
    qualification = {
        "format": NATIVE_QUALIFICATION_FORMAT,
        "gate_state_sha256": sha256_bytes(state_bytes),
        "plugin_sha256": state["plugin_sha256"],
        "plugin_source_sha256": state[
            "plugin_source_sha256"
        ],
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "studio_executable_path": studio[
            "executable_path"
        ],
        "studio_executable_sha256": studio[
            "executable_sha256"
        ],
    }
    write_new(
        root / NATIVE_QUALIFICATION_FILENAME,
        (
            json.dumps(
                qualification, indent=2, sort_keys=True
            )
            + "\n"
        ).encode("utf-8"),
        0o600,
    )
    validated = require_native_qualification(
        root,
        state,
        plugin_path,
        receipt_path,
    )
    return {
        "ok": True,
        "qualification": validated,
    }


def load_candidate(work_root: Path):
    work_root = _real_directory(
        work_root,
        "candidate work root",
        allowed_modes=(0o700, 0o755),
    )
    _state_bytes, state = read_private_record(
        work_root / STATE_FILENAME,
        "candidate gate state",
    )
    (
        payload,
        support,
        plugin_path,
        catalog_path,
        receipt_path,
    ) = (
        _validated_gate_state(work_root, state)
    )
    native_qualification = require_native_qualification(
        work_root,
        state,
        plugin_path,
        receipt_path,
    )
    candidate_package, candidate_modules = (
        _load_isolated_package(
            payload / "studio_mcp_v2",
            "candidate",
            ("frontend", "lifecycle"),
        )
    )
    candidate_frontend = candidate_modules["frontend"]
    candidate_lifecycle = candidate_modules["lifecycle"]

    _require_module_from_payload(
        candidate_package, payload, "candidate package"
    )
    _require_module_from_payload(
        candidate_frontend, payload, "candidate frontend"
    )
    _require_module_from_payload(
        candidate_lifecycle, payload, "candidate lifecycle"
    )
    if candidate_package.__version__ != state["version"]:
        raise RuntimeError("loaded candidate version drifted")
    HubClient = candidate_frontend.HubClient
    InstallPaths = candidate_lifecycle.InstallPaths
    broker_status = candidate_lifecycle.broker_status
    ensure_broker = candidate_lifecycle.ensure_broker
    load_install_config = candidate_lifecycle.load_install_config
    stop_broker = candidate_lifecycle.stop_broker
    paths = InstallPaths.for_test(support)
    config, secret_config = load_install_config(paths)
    if tuple(config.allowed_tools) != tuple(
        sorted(ALLOWED_PUBLIC_SCOPES)
    ):
        raise RuntimeError("loaded runtime public tool scope drifted")
    if (
        config.host != "127.0.0.1"
        or config.port != state["port"]
        or config.catalog.resolve(strict=True) != catalog_path
        or tuple(config.allowed_studios) != ("*",)
        or config.startup_timeout_seconds != 10.0
    ):
        raise RuntimeError("loaded candidate runtime identity drifted")
    _final_state_bytes, final_state = read_private_record(
        work_root / STATE_FILENAME,
        "candidate gate state",
    )
    if final_state != state:
        raise RuntimeError(
            "candidate gate state changed during operational load"
        )
    final_contract = _validated_gate_state(
        work_root, final_state
    )
    if final_contract != (
        payload,
        support,
        plugin_path,
        catalog_path,
        receipt_path,
    ):
        raise RuntimeError(
            "candidate gate artifacts changed during operational load"
        )
    native_qualification = require_native_qualification(
        work_root,
        final_state,
        plugin_path,
        receipt_path,
    )
    return (
        state,
        paths,
        config,
        secret_config,
        HubClient,
        broker_status,
        ensure_broker,
        stop_broker,
        native_qualification,
    )


def _require_private_directory(path: Path, label: str) -> Path:
    return _real_directory(
        path,
        label,
        allowed_modes=(0o700,),
    )


def _cleanup_runtime_records(
    work_root: Path,
) -> Tuple[
    Path,
    Dict[str, Any],
    Dict[str, Any],
    bytes,
    Dict[str, Any],
]:
    support = _require_private_directory(
        work_root / "support",
        "candidate support root",
    )
    config_dir = _require_private_directory(
        support / "config",
        "candidate config root",
    )
    _require_private_directory(
        support / "run",
        "candidate run root",
    )
    _require_private_directory(
        support / "logs",
        "candidate logs root",
    )
    runtime_bytes, runtime = read_bounded_record(
        config_dir / "runtime.json",
        "candidate runtime config",
        0o644,
    )
    secret_bytes, secrets_record = read_private_record(
        config_dir / "secrets.json",
        "candidate secret config",
    )
    _require_exact_keys(
        runtime,
        {
            "schema_version",
            "host",
            "port",
            "catalog",
            "allowed_studios",
            "allowed_tools",
            "startup_timeout_seconds",
        },
        "candidate runtime config",
    )
    _require_exact_keys(
        secrets_record,
        {
            "schema_version",
            "client_token",
            "studio_token",
        },
        "candidate secret config",
    )
    identity_bytes, identity = read_private_record(
        work_root / CLEANUP_IDENTITY_FILENAME,
        "candidate cleanup identity",
    )
    _require_exact_keys(
        identity,
        CLEANUP_IDENTITY_KEYS,
        "candidate cleanup identity",
    )
    for field in (
        "release_manifest_sha256",
        "durable_catalog_file_sha256",
        "runtime_config_sha256",
        "secrets_config_sha256",
    ):
        if not SHA256_PATTERN.fullmatch(
            str(identity.get(field, ""))
        ):
            raise RuntimeError(
                "candidate cleanup identity hash drifted"
            )
    if (
        identity.get("format") != CLEANUP_IDENTITY_FORMAT
        or not isinstance(identity.get("version"), str)
        or not identity["version"]
        or type(identity.get("port")) is not int
        or not 1 <= identity["port"] <= 65_535
        or not RUN_ID_PATTERN.fullmatch(
            str(identity.get("run_id", ""))
        )
        or identity["runtime_config_sha256"]
        != sha256_bytes(runtime_bytes)
        or identity["secrets_config_sha256"]
        != sha256_bytes(secret_bytes)
    ):
        raise RuntimeError(
            "candidate cleanup identity drifted"
        )
    expected_catalog = config_dir / "tool-catalog.json"
    if (
        runtime.get("schema_version") != 1
        or runtime.get("host") != "127.0.0.1"
        or type(runtime.get("port")) is not int
        or not 1 <= runtime["port"] <= 65_535
        or runtime.get("catalog") != str(expected_catalog)
        or runtime.get("allowed_studios") != ["*"]
        or runtime.get("allowed_tools")
        != sorted(ALLOWED_PUBLIC_SCOPES)
        or type(runtime.get("startup_timeout_seconds"))
        not in (int, float)
        or runtime["startup_timeout_seconds"] != 10.0
        or runtime["port"] != identity["port"]
    ):
        raise RuntimeError(
            "candidate cleanup runtime identity drifted"
        )
    client_token = secrets_record.get("client_token")
    studio_token = secrets_record.get("studio_token")
    if (
        secrets_record.get("schema_version") != 1
        or not isinstance(client_token, str)
        or not isinstance(studio_token, str)
        or not 32 <= len(client_token) <= 512
        or not 32 <= len(studio_token) <= 512
        or any(
            not character.isprintable()
            or character.isspace()
            for character in client_token + studio_token
        )
        or secrets.compare_digest(client_token, studio_token)
    ):
        raise RuntimeError(
            "candidate cleanup secret identity drifted"
        )
    return (
        support,
        runtime,
        secrets_record,
        identity_bytes,
        identity,
    )


def _cleanup_broker_record(
    identity_bytes: bytes,
    identity: Mapping[str, Any],
    health: Mapping[str, Any],
) -> Dict[str, Any]:
    broker_instance_id = validate_explicit_studio_id(
        health.get("broker_instance_id")
    )
    pid = health.get("pid")
    started_at = health.get("started_at")
    version = health.get("version")
    catalog_sha256 = health.get("catalog_sha256")
    if (
        type(pid) is not int
        or pid <= 0
        or type(started_at) not in (int, float)
        or not math.isfinite(float(started_at))
        or started_at <= 0
        or version != identity["version"]
        or not isinstance(catalog_sha256, str)
        or SHA256_PATTERN.fullmatch(catalog_sha256) is None
    ):
        raise RuntimeError(
            "candidate cleanup broker health drifted"
        )
    return {
        "format": CLEANUP_BROKER_FORMAT,
        "cleanup_identity_sha256": sha256_bytes(
            identity_bytes
        ),
        "version": identity["version"],
        "port": identity["port"],
        "run_id": identity["run_id"],
        "catalog_sha256": catalog_sha256,
        "broker_instance_id": broker_instance_id,
        "pid": pid,
        "started_at": float(started_at),
    }


def _pin_cleanup_broker(
    work_root: Path,
    health: Mapping[str, Any],
) -> Dict[str, Any]:
    (
        _support,
        _runtime,
        _secrets_record,
        identity_bytes,
        identity,
    ) = _cleanup_runtime_records(work_root)
    expected = _cleanup_broker_record(
        identity_bytes, identity, health
    )
    path = work_root / CLEANUP_BROKER_FILENAME
    encoded = (
        json.dumps(expected, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if path.exists() or path.is_symlink():
        observed_bytes, observed = read_private_record(
            path,
            "candidate cleanup broker receipt",
        )
        if observed != expected or observed_bytes != encoded:
            raise RuntimeError(
                "candidate cleanup broker receipt drifted"
            )
    else:
        write_new(path, encoded, 0o600)
        observed_bytes, observed = read_private_record(
            path,
            "candidate cleanup broker receipt",
        )
        if observed != expected or observed_bytes != encoded:
            raise RuntimeError(
                "candidate cleanup broker receipt write failed"
            )
    return expected


def _require_cleanup_broker(
    work_root: Path,
    identity_bytes: bytes,
    identity: Mapping[str, Any],
    local: Mapping[str, Any],
) -> Dict[str, Any]:
    health = local.get("broker")
    if (
        not isinstance(health, dict)
        or local.get("running") is not True
        or local.get("record_matches") is not True
    ):
        raise RuntimeError(
            "candidate cleanup broker identity is not live and recorded"
        )
    expected = _cleanup_broker_record(
        identity_bytes, identity, health
    )
    _receipt_bytes, receipt = read_private_record(
        work_root / CLEANUP_BROKER_FILENAME,
        "candidate cleanup broker receipt",
    )
    _require_exact_keys(
        receipt,
        CLEANUP_BROKER_KEYS,
        "candidate cleanup broker receipt",
    )
    if receipt != expected:
        raise RuntimeError(
            "candidate cleanup broker receipt drifted"
        )
    return receipt


def _candidate_lifecycle_action(
    work_root: Path, action: str
) -> Dict[str, Any]:
    root = _real_directory(
        work_root,
        "candidate work root",
        allowed_modes=(0o700, 0o755),
    )
    (
        support,
        runtime,
        secrets_record,
        identity_bytes,
        identity,
    ) = (
        _cleanup_runtime_records(root)
    )

    gate_package, gate_modules = _load_isolated_package(
        PROJECT_ROOT / "studio_mcp_v2",
        "gate",
        ("lifecycle",),
    )
    gate_lifecycle = gate_modules["lifecycle"]

    _require_module_from_project(
        gate_package, "gate package"
    )
    _require_module_from_project(
        gate_lifecycle, "gate lifecycle"
    )
    if gate_package.__version__ != identity["version"]:
        raise RuntimeError(
            "candidate cleanup gate version drifted"
        )
    paths = gate_lifecycle.InstallPaths.for_test(support)
    config = gate_lifecycle.RuntimeConfig(
        host=runtime["host"],
        port=runtime["port"],
        catalog=Path(runtime["catalog"]),
        allowed_studios=tuple(runtime["allowed_studios"]),
        allowed_tools=tuple(runtime["allowed_tools"]),
        startup_timeout_seconds=float(
            runtime["startup_timeout_seconds"]
        ),
    )
    secret_config = gate_lifecycle.SecretsConfig(
        client_token=secrets_record["client_token"],
        studio_token=secrets_record["studio_token"],
    )
    local = gate_lifecycle.broker_status(
        paths, config, secret_config
    )
    if action == "status":
        result = dict(local)
        if local.get("running") is True:
            try:
                receipt = _require_cleanup_broker(
                    root,
                    identity_bytes,
                    identity,
                    local,
                )
                result["cleanup_broker_identity_ok"] = True
                result["cleanup_broker_instance_id"] = (
                    receipt["broker_instance_id"]
                )
            except (OSError, RuntimeError, ValueError) as exc:
                result["cleanup_broker_identity_ok"] = False
                result["cleanup_error"] = str(exc)
                result["condition"] = (
                    "running_cleanup_identity_unproven"
                )
        else:
            result["cleanup_broker_identity_ok"] = None
        return result
    if action == "stop":
        if local.get("running") is not True:
            if local.get("condition") != "stopped":
                raise RuntimeError(
                    "candidate cleanup cannot prove the broker is "
                    "absent; refusing an unfenced stop"
                )
            return {
                "running": False,
                "stopped": False,
            }
        receipt = _require_cleanup_broker(
            root,
            identity_bytes,
            identity,
            local,
        )
        broker = local["broker"]
        if (
            local.get("condition") != "healthy_idle"
            or broker.get("stop_safe") is not True
            or broker.get("lifecycle_stopping") is True
        ):
            raise RuntimeError(
                "candidate cleanup broker is not positively stop-safe"
            )
        return gate_lifecycle.stop_broker(
            paths,
            config,
            secret_config,
            expected_broker_instance_id=receipt[
                "broker_instance_id"
            ],
        )
    raise RuntimeError("unsupported candidate lifecycle action")


def prepare(args: argparse.Namespace) -> Dict[str, Any]:
    work_root = _real_directory(
        args.work_root,
        "candidate work root",
        allowed_modes=(0o700, 0o755),
    )
    candidate_owned_paths = (
        work_root / STATE_FILENAME,
        work_root / NATIVE_RECEIPT_FILENAME,
        work_root / NATIVE_QUALIFICATION_FILENAME,
        work_root / CLEANUP_IDENTITY_FILENAME,
        work_root / CLEANUP_BROKER_FILENAME,
        work_root / "support",
        work_root / "StudioMCPv2CandidateReadOnly.rbxmx",
    )
    if any(path.exists() or path.is_symlink() for path in candidate_owned_paths):
        raise RuntimeError(
            "candidate gate work root is not fresh"
        )
    if not SHA256_PATTERN.fullmatch(args.durable_catalog_sha256):
        raise RuntimeError("expected durable catalog SHA-256 is invalid")
    if not SHA256_PATTERN.fullmatch(
        args.release_manifest_sha256
    ):
        raise RuntimeError(
            "expected release manifest SHA-256 is invalid"
        )

    payload, release_manifest_sha256 = (
        _validated_candidate_release(
            work_root,
            expected_version=args.version,
            expected_manifest_sha256=(
                args.release_manifest_sha256
            ),
        )
    )
    candidate_renderer = _load_isolated_module(
        payload / "scripts" / "render_studio_plugin.py",
        "candidate renderer",
    )
    candidate_package, candidate_modules = (
        _load_isolated_package(
            payload / "studio_mcp_v2",
            "candidate",
            ("catalog",),
        )
    )
    ToolCatalog = candidate_modules["catalog"].ToolCatalog

    _require_module_from_payload(
        candidate_renderer, payload, "candidate renderer"
    )
    _require_module_from_payload(
        candidate_package, payload, "candidate package"
    )
    if candidate_package.__version__ != args.version:
        raise RuntimeError("candidate payload version identity drifted")

    durable_source = (
        payload / "config" / "durable-tool-catalog.json"
    )
    durable_bytes = read_regular_bytes(
        durable_source,
        "candidate durable catalog",
        0o644,
        MAX_GATE_RECORD_BYTES,
    )
    if sha256_bytes(durable_bytes) != args.durable_catalog_sha256:
        raise RuntimeError("durable catalog bytes drifted")
    public_to_remote = audit_catalog_contract(
        ToolCatalog.from_file(durable_source)
    )

    support = work_root / "support"
    config_dir = support / "config"
    support.mkdir(mode=0o700)
    for directory in (
        config_dir,
        support / "run",
        support / "logs",
    ):
        directory.mkdir(mode=0o700)

    catalog_path = config_dir / "tool-catalog.json"
    write_new(catalog_path, durable_bytes, 0o644)
    upstream_catalog_bytes = read_regular_bytes(
        payload / "config" / "tool-catalog.json",
        "candidate upstream catalog",
        0o644,
        MAX_GATE_RECORD_BYTES,
    )
    write_new(
        config_dir / "upstream-known-tool-catalog.json",
        upstream_catalog_bytes,
        0o644,
    )
    upstream_compatibility_bytes = read_regular_bytes(
        (
            payload
            / "config"
            / "upstream-compatibility-map.json"
        ),
        "candidate upstream compatibility map",
        0o644,
        MAX_GATE_RECORD_BYTES,
    )
    write_new(
        config_dir / "upstream-compatibility-map.json",
        upstream_compatibility_bytes,
        0o644,
    )

    runtime = build_runtime(args.port, catalog_path)
    client_token = secrets.token_urlsafe(48)
    studio_token = secrets.token_urlsafe(48)
    while secrets.compare_digest(client_token, studio_token):
        studio_token = secrets.token_urlsafe(48)
    runtime_bytes = (
        json.dumps(
            runtime, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    write_new(
        config_dir / "runtime.json",
        runtime_bytes,
        0o644,
    )
    secrets_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "client_token": client_token,
                "studio_token": studio_token,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    write_new(
        config_dir / "secrets.json",
        secrets_bytes,
        0o600,
    )

    run_id = secrets.token_hex(16)
    cleanup_identity = {
        "format": CLEANUP_IDENTITY_FORMAT,
        "version": args.version,
        "port": args.port,
        "run_id": run_id,
        "release_manifest_sha256": (
            release_manifest_sha256
        ),
        "durable_catalog_file_sha256": sha256_bytes(
            durable_bytes
        ),
        "runtime_config_sha256": sha256_bytes(
            runtime_bytes
        ),
        "secrets_config_sha256": sha256_bytes(
            secrets_bytes
        ),
    }
    cleanup_identity_bytes = (
        json.dumps(
            cleanup_identity, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")
    write_new(
        work_root / CLEANUP_IDENTITY_FILENAME,
        cleanup_identity_bytes,
        0o600,
    )
    source = candidate_renderer.render_durable(
        studio_token,
        run_id,
        base_url=f"http://127.0.0.1:{args.port}",
    )
    package = candidate_renderer.package_rbxmx(
        source,
        package_name="StudioMCPv2CandidateReadOnly",
    ).encode("utf-8")
    plugin_path = (
        work_root / "StudioMCPv2CandidateReadOnly.rbxmx"
    )
    write_new(plugin_path, package, 0o600)
    source_sha256 = sha256_bytes(source.encode("utf-8"))
    receipt_path = (
        work_root / NATIVE_RECEIPT_FILENAME
    ).resolve()

    state = {
        "format": GATE_STATE_FORMAT,
        "version": args.version,
        "payload_root": str(payload),
        "support_root": str(support),
        "plugin_path": str(plugin_path),
        "plugin_sha256": sha256_bytes(package),
        "plugin_source_sha256": source_sha256,
        "native_compile_receipt_path": str(receipt_path),
        "release_manifest_sha256": release_manifest_sha256,
        "cleanup_identity_sha256": sha256_bytes(
            cleanup_identity_bytes
        ),
        "durable_catalog_file_sha256": sha256_bytes(
            durable_bytes
        ),
        "runtime_config_sha256": sha256_bytes(runtime_bytes),
        "secrets_config_sha256": sha256_bytes(secrets_bytes),
        "upstream_catalog_file_sha256": sha256_bytes(
            upstream_catalog_bytes
        ),
        "upstream_compatibility_file_sha256": sha256_bytes(
            upstream_compatibility_bytes
        ),
        "port": args.port,
        "run_id": run_id,
        "allowed_tools": sorted(ALLOWED_PUBLIC_SCOPES),
        "public_to_remote": public_to_remote,
    }
    write_new(
        work_root / STATE_FILENAME,
        (
            json.dumps(state, indent=2, sort_keys=True)
            + "\n"
        ).encode(),
        0o600,
    )
    return state


def client_for(work_root: Path):
    loaded = load_candidate(work_root)
    state, paths, config, secret_config = loaded[:4]
    HubClient = loaded[4]
    return (
        loaded,
        HubClient(
            config.base_url,
            secret_config.client_token,
            timeout_seconds=130.0,
        ),
    )


def start(work_root: Path) -> Dict[str, Any]:
    loaded = load_candidate(work_root)
    state, paths, config, secret_config = loaded[:4]
    health = loaded[6](paths, config, secret_config)
    broker_instance_id = validate_explicit_studio_id(
        health.get("broker_instance_id")
    )
    try:
        cleanup_broker = _pin_cleanup_broker(
            _real_directory(
                work_root,
                "candidate work root",
                allowed_modes=(0o700, 0o755),
            ),
            health,
        )
    except Exception as receipt_error:
        try:
            loaded[7](
                paths,
                config,
                secret_config,
                expected_broker_instance_id=(
                    broker_instance_id
                ),
            )
        except Exception as stop_error:
            raise RuntimeError(
                "candidate cleanup broker receipt failed and "
                "the exact newly observed broker could not be "
                "stopped safely: "
                + str(stop_error)
            ) from receipt_error
        raise RuntimeError(
            "candidate cleanup broker receipt failed; the exact "
            "newly observed broker was stopped"
        ) from receipt_error
    return {
        "ok": True,
        "candidate": {
            "version": state["version"],
            "port": state["port"],
            "durable_catalog_file_sha256": state[
                "durable_catalog_file_sha256"
            ],
            "plugin_sha256": state["plugin_sha256"],
            "plugin_source_sha256": state[
                "plugin_source_sha256"
            ],
            "allowed_tools": state["allowed_tools"],
            "public_to_remote": state["public_to_remote"],
            "native_qualification": loaded[8],
            "cleanup_broker": cleanup_broker,
        },
        "broker": health,
    }


def status(work_root: Path) -> Dict[str, Any]:
    root = _real_directory(
        work_root,
        "candidate work root",
        allowed_modes=(0o700, 0o755),
    )
    try:
        local = _candidate_lifecycle_action(root, "status")
    except (OSError, RuntimeError, ValueError) as exc:
        local = {
            "running": False,
            "condition": "unavailable",
            "error": str(exc),
        }
    try:
        _state_bytes, state = read_private_record(
            root / STATE_FILENAME,
            "candidate gate state",
        )
        (
            _payload,
            _support,
            plugin_path,
            _catalog_path,
            receipt_path,
        ) = _validated_gate_state(root, state)
        qualification = native_qualification_status(
            root,
            state,
            plugin_path,
            receipt_path,
        )
        version = state["version"]
    except (OSError, RuntimeError, ValueError) as exc:
        qualification = {
            "ok": False,
            "error": str(exc),
        }
        version = None
    result = {
        "ok": False,
        "version": version,
        "local": local,
        "native_qualification": qualification,
        "authenticated": None,
    }
    local_condition = local.get("condition")
    local_safe = local_condition in {"stopped", "healthy_idle"}
    if (
        qualification["ok"] is True
        and local.get("running") is True
    ):
        try:
            loaded = load_candidate(root)
            _state, _paths, config, secret_config = loaded[:4]
            HubClient = loaded[4]
            client = HubClient(
                config.base_url,
                secret_config.client_token,
                timeout_seconds=130.0,
            )
            authenticated = client.lifecycle_status()
            if not isinstance(authenticated, dict):
                raise RuntimeError(
                    "candidate returned malformed lifecycle status"
                )
            result["authenticated"] = authenticated
            local_safe = (
                local_condition == "healthy_idle"
                and authenticated.get("stop_safe") is True
            )
        except Exception as exc:
            result["authenticated"] = {
                "ok": False,
                "error": str(exc),
            }
            local_safe = False
    result["ok"] = (
        qualification["ok"] is True and local_safe
    )
    return result


def list_studios(work_root: Path) -> Dict[str, Any]:
    _loaded, client = client_for(work_root)
    return client.list_studios()


def _contains_nested_target(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if "studio_id" in item:
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def parse_tool_arguments(raw_arguments: str) -> Dict[str, Any]:
    if not isinstance(raw_arguments, str):
        raise RuntimeError("tool arguments must be JSON text")
    try:
        raw_bytes = raw_arguments.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "tool arguments must be valid bounded UTF-8 JSON"
        ) from exc
    if len(raw_bytes) > MAX_TOOL_ARGUMENT_BYTES:
        raise RuntimeError(
            "tool arguments exceed the candidate gate size limit"
        )
    try:
        arguments = json.loads(
            raw_arguments,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("tool arguments must be valid JSON") from exc
    if not isinstance(arguments, dict):
        raise RuntimeError("tool arguments must be an object")
    if _contains_nested_target(arguments):
        raise RuntimeError(
            "tool arguments must not contain a caller-supplied "
            "studio_id at any depth"
        )
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(
            "tool arguments must be valid bounded UTF-8 JSON"
        ) from exc
    if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
        raise RuntimeError(
            "tool arguments exceed the candidate gate size limit"
        )
    return arguments


def call(
    work_root: Path,
    tool_name: str,
    studio_id: str,
    raw_arguments: str,
) -> Any:
    public_tool = validate_public_tool_name(tool_name)
    target = validate_explicit_studio_id(studio_id)
    arguments = parse_tool_arguments(raw_arguments)
    arguments["studio_id"] = target
    _loaded, client = client_for(work_root)
    # Deliberately pass the public name unchanged. ProxyService resolves the
    # remote handler through its approved ToolCatalog.
    return client.call(
        public_tool, arguments, str(uuid.uuid4())
    )


def start_job(
    work_root: Path,
    tool_name: str,
    studio_id: str,
    raw_arguments: str,
    timeout_ms: int,
) -> Any:
    public_tool = validate_public_tool_name(tool_name)
    target = validate_explicit_studio_id(studio_id)
    arguments = parse_tool_arguments(raw_arguments)
    timeout = validate_job_timeout(timeout_ms)
    _loaded, client = client_for(work_root)
    return client.start_job(
        {
            "studio_id": target,
            "tool_name": public_tool,
            "tool_arguments": arguments,
            "timeout_ms": timeout,
        }
    )


def get_job(
    work_root: Path, studio_id: str, job_id: str
) -> Any:
    target = validate_explicit_studio_id(studio_id)
    selected_job = validate_job_id(job_id)
    _loaded, client = client_for(work_root)
    return client.get_job(
        {"studio_id": target, "job_id": selected_job}
    )


def stop(work_root: Path) -> Dict[str, Any]:
    return _candidate_lifecycle_action(work_root, "stop")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--work-root", type=Path, required=True)
    commands = root.add_subparsers(
        dest="command", required=True
    )
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--version", required=True)
    prepare_command.add_argument(
        "--durable-catalog-sha256", required=True
    )
    prepare_command.add_argument(
        "--release-manifest-sha256", required=True
    )
    prepare_command.add_argument(
        "--port", type=int, required=True
    )
    commands.add_parser("start")
    commands.add_parser("qualify-native")
    commands.add_parser("status")
    commands.add_parser("list")
    call_command = commands.add_parser("call")
    call_command.add_argument("--tool", required=True)
    call_command.add_argument("--studio-id", required=True)
    call_command.add_argument(
        "--arguments-json", required=True
    )
    job_command = commands.add_parser("start-job")
    job_command.add_argument("--tool", required=True)
    job_command.add_argument("--studio-id", required=True)
    job_command.add_argument(
        "--arguments-json", required=True
    )
    job_command.add_argument(
        "--timeout-ms", type=int, default=10_000
    )
    get_command = commands.add_parser("get-job")
    get_command.add_argument("--studio-id", required=True)
    get_command.add_argument("--job-id", required=True)
    commands.add_parser("stop")
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "prepare":
        result = prepare(args)
    elif args.command == "qualify-native":
        result = qualify_native(args.work_root)
    elif args.command == "start":
        result = start(args.work_root)
    elif args.command == "status":
        result = status(args.work_root)
    elif args.command == "list":
        result = list_studios(args.work_root)
    elif args.command == "call":
        result = call(
            args.work_root,
            args.tool,
            args.studio_id,
            args.arguments_json,
        )
    elif args.command == "start-job":
        result = start_job(
            args.work_root,
            args.tool,
            args.studio_id,
            args.arguments_json,
            args.timeout_ms,
        )
    elif args.command == "get-job":
        result = get_job(
            args.work_root, args.studio_id, args.job_id
        )
    elif args.command == "stop":
        result = stop(args.work_root)
    else:
        raise RuntimeError("unsupported command")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
