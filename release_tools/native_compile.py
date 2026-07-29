"""Fail-closed native Roblox Studio compilation proof.

The proof runs one SHA-pinned rendered plugin source through Studio's native
Luau compiler without installing the plugin or opening a place.  A no-local
guard prefix proves the task is an empty, Edit-mode, non-plugin DataModel
before the exact extracted source begins.  Whole-chunk compilation precedes
execution, so the prefix does not hide main-chunk register-allocation errors.
The candidate must then stop at its reviewed, pre-registration plugin-context
assertion.  Any other outcome fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional, Sequence, Tuple
from xml.etree import ElementTree


PROOF_FORMAT = "roblox-studio-mcp-v2-native-compile-proof-v4"
DEFAULT_STUDIO_EXECUTABLE = Path(
    "/Applications/RobloxStudio.app/Contents/MacOS/RobloxStudio"
)
EXPECTED_STUDIO_BUNDLE_ID = "com.Roblox.RobloxStudio"
EXPECTED_STUDIO_TEAM_ID = "2CFABCH843"
EXPECTED_STUDIO_REQUIREMENT = (
    '=anchor apple generic and identifier "com.Roblox.RobloxStudio" '
    'and certificate leaf[subject.OU] = "2CFABCH843"'
)
EXPECTED_STUDIO_LEAF_AUTHORITY = (
    "Developer ID Application: Roblox Corporation (2CFABCH843)"
)
EXPECTED_STUDIO_SIGNATURE_SCOPE = (
    "main_executable_code_and_explicit_requirement"
)
EXPECTED_MAIN_ASSERTION = (
    'assert(plugin ~= nil, '
    '"Studio MCP v2 must be installed as a Studio plugin")'
)
EXPECTED_MAIN_ASSERTION_MESSAGE = (
    "Studio MCP v2 must be installed as a Studio plugin"
)
MAX_PACKAGE_BYTES = 2_000_000
MAX_SOURCE_BYTES = 1_000_000
MAX_EXECUTABLE_BYTES = 1_000_000_000
MAX_INFO_PLIST_BYTES = 1_000_000
MAX_IDENTITY_OUTPUT_BYTES = 1_000_000
MAX_PROCESS_LOG_BYTES = 10_000_000
MAX_RECEIPT_BYTES = 2_000_000
PROCESS_POLL_SECONDS = 0.05
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_ID_RE = re.compile(r"^[\x20-\x7e]{1,128}$")
REFERENT_RE = re.compile(r"^RBX[0-9a-f]{32}$")
SCRIPT_GUID_RE = re.compile(
    r"^\{[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\}$"
)
SAFE_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
USER_PLUGIN_RE = re.compile(
    rb"\buser_[A-Za-z0-9_.-]+\.(?:rbxm|rbxmx)\b",
    re.IGNORECASE,
)
COMPILE_ERROR_MARKERS = (
    b"out of local registers",
    b"exceeded register limit",
    b"syntaxerror",
    b"syntax error",
    b"compileerror",
    b"compile error",
    b"failed to compile",
    b"parse error",
)
RAW_SOURCE_OPEN = b'<ProtectedString name="Source"><![CDATA['
RAW_SOURCE_CLOSE = b"]]></ProtectedString>"
XSI_SCHEMA_ATTRIBUTE = (
    "{http://www.w3.org/2001/XMLSchema-instance}"
    "noNamespaceSchemaLocation"
)
EXPECTED_SCHEMA_LOCATION = "http://www.roblox.com/roblox.xsd"
FOLDER_PROPERTY_SHAPE = (
    ("BinaryString", "AttributesSerialize"),
    ("SecurityCapabilities", "Capabilities"),
    ("bool", "DefinesCapabilities"),
    ("string", "Name"),
    ("int64", "SourceAssetId"),
    ("BinaryString", "Tags"),
)
SCRIPT_PROPERTY_SHAPE = (
    ("ProtectedString", "Source"),
    ("bool", "Disabled"),
    ("Content", "LinkedSource"),
    ("token", "RunContext"),
    ("string", "ScriptGuid"),
    ("BinaryString", "AttributesSerialize"),
    ("SecurityCapabilities", "Capabilities"),
    ("bool", "DefinesCapabilities"),
    ("string", "Name"),
    ("int64", "SourceAssetId"),
    ("BinaryString", "Tags"),
)
EDIT_FENCE = """if not initialStudioOk
\tor initialIsStudio ~= true
\tor not initialEditOk
\tor initialIsEdit ~= true
\tor not initialRunningOk
\tor initialIsRunning ~= false
then
\t-- A local plugin copy must never register a controller from an unknown or
\t-- non-Edit DataModel. Returning here also avoids touching the document.
\treturn
end

"""
SOURCE_CONTRACT_MARKERS = (
    'local InitialRunService = game:GetService("RunService")',
    "local initialStudioOk, initialIsStudio = pcall(function()",
    "local initialEditOk, initialIsEdit = pcall(function()",
    "local initialRunningOk, initialIsRunning = pcall(function()",
    "if initialStudioOk\n"
    "\tand initialIsStudio == true\n"
    "\tand initialRunningOk\n"
    "\tand initialIsRunning == true\n"
    "then",
    EDIT_FENCE,
    EXPECTED_MAIN_ASSERTION,
    "local CONFIG = table.freeze({",
    "local function connect(",
    "local registered, registrationError = connect(false)",
)
PREFIX_FAILURE_MESSAGES = (
    "native compile guard failed: PlaceId is not zero",
    "native compile guard failed: GameId is not zero",
    "native compile guard failed: Studio mode is unavailable",
    "native compile guard failed: DataModel is not Edit",
    "native compile guard failed: DataModel is running",
    "native compile guard failed: plugin context is present",
)
SAFE_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TMPDIR",
    "USER",
    "__CF_USER_TEXT_ENCODING",
)
COMMAND_ARGUMENT_CONTRACT = (
    "--task",
    "RunScript",
    "-disableloaduserplugins",
    "--runScriptFile",
    "<private-exact-main-chunk>",
    "--outputFile",
    "<private-output>",
    "--quitAfterExecution",
)
RECEIPT_TOP_LEVEL_KEYS = {
    "command_contract",
    "created_at",
    "failure_reasons",
    "format",
    "guard_contract",
    "logs",
    "observations",
    "ok",
    "package",
    "process",
    "runner",
    "studio",
}


class NativeCompileError(RuntimeError):
    """The exact package did not produce a native compilation proof."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise NativeCompileError(label + " must be a lowercase SHA-256")
    return value


def _lstat_regular(path: Path, label: str) -> os.stat_result:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise NativeCompileError(label + " is missing: " + str(path)) from exc
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise NativeCompileError(label + " must be a non-symlink regular file")
    return details


def _canonical_regular(path: Path, label: str) -> Path:
    supplied = Path(path)
    _lstat_regular(supplied, label)
    resolved = supplied.resolve(strict=True)
    _lstat_regular(resolved, label)
    return resolved


def _read_regular_bytes(path: Path, label: str, maximum: int) -> bytes:
    """Read one unchanged non-symlink regular file under an exact byte bound."""

    resolved = _canonical_regular(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise NativeCompileError(label + " changed filesystem type")
        if before.st_size < 1 or before.st_size > maximum:
            raise NativeCompileError(label + " size is outside the bound")
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(value) > maximum or len(value) != before.st_size:
        raise NativeCompileError(label + " changed or exceeded its byte bound")
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise NativeCompileError(label + " changed while it was read")
    return value


def _sha256_regular_file(path: Path, label: str, maximum: int) -> str:
    resolved = _canonical_regular(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > maximum
        ):
            raise NativeCompileError(label + " size is outside the bound")
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise NativeCompileError(label + " exceeds its byte bound")
            digest.update(chunk)
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
    if total != before.st_size or any(
        getattr(before, field) != getattr(after, field)
        for field in stable_fields
    ):
        raise NativeCompileError(label + " changed while it was hashed")
    return digest.hexdigest()


def _property_shape(properties: ElementTree.Element) -> Tuple[Tuple[str, str], ...]:
    return tuple((child.tag, child.attrib.get("name", "")) for child in properties)


def _one_direct_child(
    parent: ElementTree.Element,
    tag: str,
    label: str,
) -> ElementTree.Element:
    children = [child for child in parent if child.tag == tag]
    if len(children) != 1:
        raise NativeCompileError(label + " must appear exactly once")
    return children[0]


def extract_exact_main_source(package_path: Path) -> Tuple[bytes, bytes]:
    """Return exact package and sole Main source bytes from renderer XML."""

    path = _canonical_regular(Path(package_path), "plugin package")
    package_bytes = _read_regular_bytes(
        path,
        "plugin package",
        MAX_PACKAGE_BYTES,
    )
    if package_bytes.startswith(b"\xef\xbb\xbf"):
        raise NativeCompileError("plugin package must not contain a UTF-8 BOM")
    try:
        package_text = package_bytes.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise NativeCompileError("plugin package must be strict UTF-8") from exc
    upper_package = package_bytes.upper()
    if b"<!DOCTYPE" in upper_package or b"<!ENTITY" in upper_package:
        raise NativeCompileError("plugin package must not declare a DTD or entity")
    if package_bytes.count(RAW_SOURCE_OPEN) != 1:
        raise NativeCompileError(
            "plugin package must contain one exact Source CDATA opening"
        )
    raw_before, raw_separator, raw_after = package_bytes.partition(RAW_SOURCE_OPEN)
    del raw_before
    if not raw_separator or raw_after.count(RAW_SOURCE_CLOSE) != 1:
        raise NativeCompileError(
            "plugin package must contain one exact Source CDATA closing"
        )
    source_bytes, raw_separator, raw_remainder = raw_after.partition(
        RAW_SOURCE_CLOSE
    )
    del raw_remainder
    if not raw_separator:
        raise NativeCompileError("plugin package Source CDATA is unterminated")
    try:
        source_text = source_bytes.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise NativeCompileError("plugin Main source is not valid UTF-8") from exc
    if not 1 <= len(source_bytes) <= MAX_SOURCE_BYTES:
        raise NativeCompileError("plugin Main source size is outside the bound")
    if "\x00" in source_text:
        raise NativeCompileError("plugin Main source contains a NUL")

    try:
        root = ElementTree.fromstring(package_text)
    except ElementTree.ParseError as exc:
        raise NativeCompileError("plugin package XML is invalid") from exc
    if (
        root.tag != "roblox"
        or root.attrib
        != {
            XSI_SCHEMA_ATTRIBUTE: EXPECTED_SCHEMA_LOCATION,
            "version": "4",
        }
    ):
        raise NativeCompileError("plugin package root contract is invalid")
    root_children = list(root)
    if (
        len(root_children) != 3
        or [child.tag for child in root_children] != ["External", "External", "Item"]
        or [(root_children[0].text or ""), (root_children[1].text or "")]
        != ["null", "nil"]
    ):
        raise NativeCompileError("plugin package root structure is invalid")

    folder = root_children[2]
    if folder.attrib.get("class") != "Folder" or set(folder.attrib) != {
        "class",
        "referent",
    }:
        raise NativeCompileError("plugin package Folder contract is invalid")
    folder_referent = folder.attrib.get("referent", "")
    if REFERENT_RE.fullmatch(folder_referent) is None:
        raise NativeCompileError("plugin package Folder referent is invalid")
    folder_children = list(folder)
    if len(folder_children) != 2 or [child.tag for child in folder_children] != [
        "Properties",
        "Item",
    ]:
        raise NativeCompileError("plugin package Folder structure is invalid")
    folder_properties, script = folder_children
    if _property_shape(folder_properties) != FOLDER_PROPERTY_SHAPE:
        raise NativeCompileError("plugin package Folder properties drifted")
    folder_name = [
        child.text or ""
        for child in folder_properties
        if child.tag == "string" and child.attrib.get("name") == "Name"
    ]
    if (
        len(folder_name) != 1
        or SAFE_PACKAGE_NAME_RE.fullmatch(folder_name[0]) is None
    ):
        raise NativeCompileError("plugin package Folder name is invalid")

    if script.attrib.get("class") != "Script" or set(script.attrib) != {
        "class",
        "referent",
    }:
        raise NativeCompileError("plugin package Main Script contract is invalid")
    script_referent = script.attrib.get("referent", "")
    if (
        REFERENT_RE.fullmatch(script_referent) is None
        or script_referent == folder_referent
    ):
        raise NativeCompileError("plugin package Main referent is invalid")
    script_children = list(script)
    if len(script_children) != 1 or script_children[0].tag != "Properties":
        raise NativeCompileError("plugin package Main structure is invalid")
    script_properties = script_children[0]
    if _property_shape(script_properties) != SCRIPT_PROPERTY_SHAPE:
        raise NativeCompileError("plugin package Main properties drifted")
    sources = [
        child
        for child in script_properties
        if child.tag == "ProtectedString" and child.attrib.get("name") == "Source"
    ]
    names = [
        child.text or ""
        for child in script_properties
        if child.tag == "string" and child.attrib.get("name") == "Name"
    ]
    guids = [
        child.text or ""
        for child in script_properties
        if child.tag == "string" and child.attrib.get("name") == "ScriptGuid"
    ]
    if (
        len(sources) != 1
        or list(sources[0])
        or (sources[0].text or "") != source_text
        or names != ["Main"]
        or len(guids) != 1
        or SCRIPT_GUID_RE.fullmatch(guids[0]) is None
    ):
        raise NativeCompileError("plugin package must expose one exact Main source")
    if len(list(root.iter("Item"))) != 2:
        raise NativeCompileError("plugin package contains an unexpected Item")
    return package_bytes, source_bytes


def validate_candidate_guard_contract(source_bytes: bytes) -> Dict[str, object]:
    """Prove the pinned source has the reviewed early Edit/plugin fence."""

    try:
        source = source_bytes.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise NativeCompileError("plugin Main source is not valid UTF-8") from exc
    positions = []
    for marker in SOURCE_CONTRACT_MARKERS:
        if source.count(marker) != 1:
            raise NativeCompileError(
                "plugin Main native-smoke guard contract drifted"
            )
        positions.append(source.index(marker))
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        raise NativeCompileError(
            "plugin Main native-smoke guard ordering drifted"
        )
    guard_index = source.index(EXPECTED_MAIN_ASSERTION)
    guard_line = source.count("\n", 0, guard_index) + 1
    config_index = source.index("local CONFIG = table.freeze({")
    if source[guard_index + len(EXPECTED_MAIN_ASSERTION) : config_index].strip():
        raise NativeCompileError(
            "plugin Main guard is not immediately before configuration"
        )
    return {
        "assertion_line": guard_line,
        "assertion_message": EXPECTED_MAIN_ASSERTION_MESSAGE,
        "config_line": source.count("\n", 0, config_index) + 1,
        "edit_fence_sha256": _sha256_bytes(EDIT_FENCE.encode("utf-8")),
        "reviewed_markers": len(SOURCE_CONTRACT_MARKERS),
    }


def _studio_bundle(executable: Path) -> Path:
    if (
        executable.name != "RobloxStudio"
        or executable.parent.name != "MacOS"
        or executable.parent.parent.name != "Contents"
    ):
        raise NativeCompileError(
            "Studio executable has an unexpected bundle-relative path"
        )
    bundle = executable.parent.parent.parent
    if bundle.suffix != ".app" or not bundle.is_dir() or bundle.is_symlink():
        raise NativeCompileError(
            "Studio executable must be inside a non-symlink app bundle"
        )
    return bundle


def _bounded_completed_process(
    arguments: Sequence[str],
    label: str,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeCompileError(label + " could not complete") from exc
    if (
        len(result.stdout) > MAX_IDENTITY_OUTPUT_BYTES
        or len(result.stderr) > MAX_IDENTITY_OUTPUT_BYTES
    ):
        raise NativeCompileError(label + " output exceeds the bound")
    return result


def inspect_studio_identity(
    executable_path: Path,
    *,
    expected_executable_sha256: str,
) -> Dict[str, object]:
    """Verify the exact signed arm64 Studio executable and return its identity.

    The required check validates the main executable's code and the explicit
    Apple/Roblox signing requirement. It deliberately does not make every
    nested app-bundle resource a prerequisite for compiling the candidate.
    Actual plugin-context loading and registration are proved separately.
    """

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise NativeCompileError(
            "native Studio compilation requires arm64 macOS"
        )
    expected_sha256 = _validate_sha256(
        expected_executable_sha256,
        "expected Studio executable SHA-256",
    )
    executable = _canonical_regular(
        Path(executable_path),
        "Studio executable",
    )
    details = executable.lstat()
    if stat.S_IMODE(details.st_mode) & 0o111 == 0:
        raise NativeCompileError("Studio executable is not executable")
    executable_sha256 = _sha256_regular_file(
        executable,
        "Studio executable",
        MAX_EXECUTABLE_BYTES,
    )
    if executable_sha256 != expected_sha256:
        raise NativeCompileError("Studio executable SHA-256 mismatch")

    bundle = _studio_bundle(executable)
    info_path = bundle / "Contents" / "Info.plist"
    info_bytes = _read_regular_bytes(
        info_path,
        "Studio Info.plist",
        MAX_INFO_PLIST_BYTES,
    )
    try:
        info = plistlib.loads(info_bytes)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise NativeCompileError("Studio Info.plist is invalid") from exc
    bundle_id = info.get("CFBundleIdentifier")
    bundle_executable = info.get("CFBundleExecutable")
    short_version = info.get("CFBundleShortVersionString")
    bundle_version = info.get("CFBundleVersion")
    if bundle_id != EXPECTED_STUDIO_BUNDLE_ID:
        raise NativeCompileError("unexpected Studio bundle identifier")
    if bundle_executable != executable.name:
        raise NativeCompileError("unexpected Studio bundle executable")
    if (
        not isinstance(short_version, str)
        or VERSION_ID_RE.fullmatch(short_version) is None
        or not isinstance(bundle_version, str)
        or VERSION_ID_RE.fullmatch(bundle_version) is None
    ):
        raise NativeCompileError("Studio bundle version identity is incomplete")

    verify = _bounded_completed_process(
        [
            "/usr/bin/codesign",
            "--verify",
            "--ignore-resources",
            "--verbose=2",
            "-R",
            EXPECTED_STUDIO_REQUIREMENT,
            str(executable),
        ],
        "Studio main-executable code-signature verification",
    )
    if verify.returncode != 0:
        raise NativeCompileError(
            "Studio main executable code signature verification failed"
        )
    details_result = _bounded_completed_process(
        [
            "/usr/bin/codesign",
            "-dv",
            "--verbose=4",
            str(executable),
        ],
        "Studio code-signature identity inspection",
    )
    if details_result.returncode != 0:
        raise NativeCompileError("Studio code signature identity is unavailable")
    signature_text = (
        details_result.stdout + details_result.stderr
    ).decode("utf-8", "replace")
    identifier_match = re.search(r"(?m)^Identifier=(.+)$", signature_text)
    team_match = re.search(r"(?m)^TeamIdentifier=(.+)$", signature_text)
    cdhash_match = re.search(
        r"(?m)^CandidateCDHashFull sha256=([0-9a-f]{64})$",
        signature_text,
    )
    cms_digest_match = re.search(
        r"(?m)^CMSDigest=([0-9a-f]{64})$",
        signature_text,
    )
    authorities = re.findall(r"(?m)^Authority=(.+)$", signature_text)
    if (
        identifier_match is None
        or identifier_match.group(1).strip() != bundle_id
        or team_match is None
        or team_match.group(1).strip() != EXPECTED_STUDIO_TEAM_ID
        or cdhash_match is None
        or cms_digest_match is None
        or cdhash_match.group(1) != cms_digest_match.group(1)
        or not authorities
        or authorities[0].strip() != EXPECTED_STUDIO_LEAF_AUTHORITY
    ):
        raise NativeCompileError("Studio signed identity is incomplete")
    return {
        "bundle_executable": bundle_executable,
        "bundle_id": bundle_id,
        "bundle_path": str(bundle),
        "bundle_short_version": short_version,
        "bundle_version": bundle_version,
        "executable_path": str(executable),
        "executable_sha256": executable_sha256,
        "info_plist_sha256": _sha256_bytes(info_bytes),
        "resource_integrity_verified": False,
        "signature_cdhash_full": cdhash_match.group(1),
        "signature_identifier": identifier_match.group(1).strip(),
        "signature_leaf_authority": authorities[0].strip(),
        "signature_requirement": EXPECTED_STUDIO_REQUIREMENT,
        "signature_scope": EXPECTED_STUDIO_SIGNATURE_SCOPE,
        "team_identifier": EXPECTED_STUDIO_TEAM_ID,
    }


def _running_studio_processes(executable: Path) -> list[int]:
    result = _bounded_completed_process(
        ["/bin/ps", "-axo", "pid=,command="],
        "running Studio process audit",
    )
    if result.returncode != 0:
        raise NativeCompileError("could not audit running Studio processes")
    target = str(executable).encode("utf-8")
    processes = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        first, separator, command = stripped.partition(b" ")
        if not separator:
            continue
        command = command.lstrip()
        process_executable, _, _arguments = command.partition(b" ")
        if process_executable != target:
            continue
        try:
            processes.append(int(first))
        except ValueError as exc:
            raise NativeCompileError(
                "running Studio process audit returned invalid output"
            ) from exc
    return sorted(set(processes))


def _write_private_new(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise NativeCompileError("private evidence write was short")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _terminate_process_group(process: subprocess.Popen) -> Optional[str]:
    errors = []
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        errors.append("SIGTERM failed: " + str(exc))
    try:
        process.wait(timeout=5)
        return "; ".join(errors) or None
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        errors.append("SIGKILL failed: " + str(exc))
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        errors.append("process group remained alive after SIGKILL")
    return "; ".join(errors) or None


def _safe_process_environment() -> Dict[str, str]:
    return {
        key: os.environ[key]
        for key in SAFE_ENVIRONMENT_KEYS
        if key in os.environ
    }


def _path_size(path: Path) -> int:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return 0
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise NativeCompileError("Studio produced unsafe process evidence")
    return details.st_size


def _read_process_evidence(path: Path) -> Tuple[bytes, int, bool]:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return b"", 0, False
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise NativeCompileError("Studio produced unsafe process evidence")
    captured = b""
    with path.open("rb") as handle:
        captured = handle.read(MAX_PROCESS_LOG_BYTES + 1)
    truncated = len(captured) > MAX_PROCESS_LOG_BYTES
    if truncated:
        captured = captured[:MAX_PROCESS_LOG_BYTES]
    return captured, details.st_size, truncated


def _run_native_studio(
    command: Sequence[str],
    *,
    working_directory: Path,
    studio_output_path: Path,
    timeout_seconds: int,
) -> Dict[str, object]:
    """Run Studio with disk-backed bounded output and a bounded process group."""

    stdout_path = working_directory / "process-stdout.log"
    stderr_path = working_directory / "process-stderr.log"
    started = time.monotonic()
    process: Optional[subprocess.Popen] = None
    timed_out = False
    log_limit_exceeded = False
    lifecycle_error: Optional[str] = None
    launch_error: Optional[str] = None
    with stdout_path.open("xb") as stdout_handle, stderr_path.open(
        "xb"
    ) as stderr_handle:
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(working_directory),
                env=_safe_process_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            launch_error = str(exc)[:500]
        if process is not None:
            while process.poll() is None:
                if time.monotonic() - started >= timeout_seconds:
                    timed_out = True
                    lifecycle_error = _terminate_process_group(process)
                    break
                try:
                    output_too_large = any(
                        _path_size(path) > MAX_PROCESS_LOG_BYTES
                        for path in (
                            stdout_path,
                            stderr_path,
                            studio_output_path,
                        )
                    )
                except NativeCompileError as exc:
                    log_limit_exceeded = True
                    termination_error = _terminate_process_group(process)
                    lifecycle_error = str(exc)
                    if termination_error is not None:
                        lifecycle_error += "; " + termination_error
                    break
                if output_too_large:
                    log_limit_exceeded = True
                    lifecycle_error = _terminate_process_group(process)
                    break
                time.sleep(PROCESS_POLL_SECONDS)
            if (
                process.poll() is None
                and lifecycle_error is None
                and not timed_out
                and not log_limit_exceeded
            ):
                lifecycle_error = _terminate_process_group(process)
            if process.poll() is None and lifecycle_error is None:
                lifecycle_error = "Studio process did not reach a terminal state"

    stdout, stdout_size, stdout_truncated = _read_process_evidence(stdout_path)
    stderr, stderr_size, stderr_truncated = _read_process_evidence(stderr_path)
    output, output_size, output_truncated = _read_process_evidence(
        studio_output_path
    )
    return {
        "launch_error": launch_error,
        "lifecycle_error": lifecycle_error,
        "log_limit_exceeded": log_limit_exceeded,
        "returncode": None if process is None else process.returncode,
        "stderr": stderr,
        "stderr_size": stderr_size,
        "stderr_truncated": stderr_truncated,
        "stdout": stdout,
        "stdout_size": stdout_size,
        "stdout_truncated": stdout_truncated,
        "studio_output": output,
        "studio_output_size": output_size,
        "studio_output_truncated": output_truncated,
        "timed_out": timed_out,
    }


def _receipt_log_path(receipt: Path, suffix: str) -> Path:
    return receipt.with_name(receipt.stem + suffix)


def _canonical_new_receipt(receipt_path: Path) -> Tuple[Path, Dict[str, Path]]:
    supplied = Path(receipt_path)
    parent = supplied.parent.resolve(strict=True)
    if not parent.is_dir() or supplied.parent.is_symlink():
        raise NativeCompileError(
            "native compile receipt parent must be an existing directory"
        )
    receipt = parent / supplied.name
    if receipt.name in {"", ".", ".."} or PurePosixPath(receipt.name).name != receipt.name:
        raise NativeCompileError("native compile receipt name is invalid")
    paths = {
        "receipt": receipt,
        "stdout": _receipt_log_path(receipt, ".studio-stdout.log"),
        "stderr": _receipt_log_path(receipt, ".studio-stderr.log"),
        "output": _receipt_log_path(receipt, ".studio-output.log"),
    }
    if len(set(paths.values())) != len(paths):
        raise NativeCompileError("native compile evidence paths collide")
    for path in paths.values():
        if path.exists() or path.is_symlink():
            raise NativeCompileError(
                "native compile evidence path already exists: " + str(path)
            )
    return receipt, paths


def _guard_prefix(
    *,
    nonce: str,
    package_sha256: str,
    source_sha256: str,
) -> Tuple[bytes, str]:
    sentinel = (
        "STUDIO_MCP_V2_NATIVE_COMPILE_PREFIX_OK:"
        + nonce
        + ":"
        + package_sha256
        + ":"
        + source_sha256
    )
    lines = (
        "-- Native compile-only no-local safety prefix.",
        'assert(game.PlaceId == 0, "' + PREFIX_FAILURE_MESSAGES[0] + '")',
        'assert(game.GameId == 0, "' + PREFIX_FAILURE_MESSAGES[1] + '")',
        'assert(game:GetService("RunService"):IsStudio() == true, "'
        + PREFIX_FAILURE_MESSAGES[2]
        + '")',
        'assert(game:GetService("RunService"):IsEdit() == true, "'
        + PREFIX_FAILURE_MESSAGES[3]
        + '")',
        'assert(game:GetService("RunService"):IsRunning() == false, "'
        + PREFIX_FAILURE_MESSAGES[4]
        + '")',
        'assert(plugin == nil, "' + PREFIX_FAILURE_MESSAGES[5] + '")',
        "print(" + json.dumps(sentinel) + ")",
        "-- Exact SHA-pinned rendered candidate source follows unchanged.",
    )
    prefix = ("\n".join(lines) + "\n").encode("utf-8")
    if re.search(rb"(?m)^[ \t]*local\b", prefix) is not None:
        raise NativeCompileError("native compile prefix unexpectedly allocates a local")
    return prefix, sentinel


def _log_receipt(
    *,
    path: Path,
    value: bytes,
    produced_bytes: int,
    truncated: bool,
) -> Dict[str, object]:
    _write_private_new(path, value)
    return {
        "captured_bytes": len(value),
        "path": str(path),
        "produced_bytes": produced_bytes,
        "sha256": _sha256_bytes(value),
        "truncated": truncated,
    }


def prove_native_studio_compilation(
    package_path: Path,
    *,
    expected_package_sha256: str,
    expected_source_sha256: str,
    receipt_path: Path,
    expected_studio_executable_sha256: str,
    studio_executable: Path = DEFAULT_STUDIO_EXECUTABLE,
    timeout_seconds: int = 120,
    temporary_parent: Optional[Path] = None,
) -> Dict[str, object]:
    """Compile one SHA-pinned Main chunk and record an identity-bound proof."""

    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise NativeCompileError("native compile timeout is outside the bound")
    package_expected = _validate_sha256(
        expected_package_sha256,
        "expected package SHA-256",
    )
    source_expected = _validate_sha256(
        expected_source_sha256,
        "expected source SHA-256",
    )
    studio_expected = _validate_sha256(
        expected_studio_executable_sha256,
        "expected Studio executable SHA-256",
    )
    package = _canonical_regular(Path(package_path), "plugin package")
    package_bytes, source_bytes = extract_exact_main_source(package)
    package_sha256 = _sha256_bytes(package_bytes)
    source_sha256 = _sha256_bytes(source_bytes)
    if package_sha256 != package_expected:
        raise NativeCompileError("plugin package SHA-256 mismatch")
    if source_sha256 != source_expected:
        raise NativeCompileError("plugin Main source SHA-256 mismatch")
    guard_contract = validate_candidate_guard_contract(source_bytes)
    receipt, evidence_paths = _canonical_new_receipt(Path(receipt_path))

    studio_identity = inspect_studio_identity(
        studio_executable,
        expected_executable_sha256=studio_expected,
    )
    executable = Path(str(studio_identity["executable_path"]))
    running_before = _running_studio_processes(executable)
    if running_before:
        raise NativeCompileError(
            "native compile requires no running Roblox Studio process"
        )

    parent = None
    if temporary_parent is not None:
        parent_candidate = Path(temporary_parent)
        if (
            parent_candidate.is_symlink()
            or not parent_candidate.resolve(strict=True).is_dir()
        ):
            raise NativeCompileError("temporary parent is unsafe")
        parent = parent_candidate.resolve(strict=True)
    nonce = secrets.token_hex(16)
    prefix, sentinel = _guard_prefix(
        nonce=nonce,
        package_sha256=package_sha256,
        source_sha256=source_sha256,
    )
    for collision in (
        sentinel,
        *PREFIX_FAILURE_MESSAGES,
    ):
        if collision.encode("utf-8") in source_bytes:
            raise NativeCompileError(
                "native compile witness collides with candidate source"
            )
    runner_source = prefix + source_bytes

    with tempfile.TemporaryDirectory(
        prefix="studio-mcp-v2-native-compile-",
        dir=str(parent) if parent is not None else None,
    ) as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        runner = root / "compile-only.luau"
        studio_output = root / "studio-output.log"
        _write_private_new(runner, runner_source)
        command = [
            str(executable),
            "--task",
            "RunScript",
            "-disableloaduserplugins",
            "--runScriptFile",
            str(runner),
            "--outputFile",
            str(studio_output),
            "--quitAfterExecution",
        ]
        process_result = _run_native_studio(
            command,
            working_directory=root,
            studio_output_path=studio_output,
            timeout_seconds=timeout_seconds,
        )

    post_audit_error = None
    running_after: list[int] = []
    try:
        running_after = _running_studio_processes(executable)
    except NativeCompileError as exc:
        post_audit_error = str(exc)

    stdout = bytes(process_result["stdout"])
    stderr = bytes(process_result["stderr"])
    output = bytes(process_result["studio_output"])
    combined = stdout + b"\n" + stderr + b"\n" + output
    combined_lower = combined.lower()
    user_plugin_hits = sorted(
        {
            match.group(0).decode("utf-8", "replace")
            for match in USER_PLUGIN_RE.finditer(combined)
        }
    )
    compile_markers = [
        marker.decode("ascii")
        for marker in COMPILE_ERROR_MARKERS
        if marker in combined_lower
    ]
    prefix_guard_failures = [
        marker
        for marker in PREFIX_FAILURE_MESSAGES
        if marker.encode("utf-8") in combined
    ]
    sentinel_bytes = sentinel.encode("ascii")
    assertion_bytes = EXPECTED_MAIN_ASSERTION_MESSAGE.encode("utf-8")
    sentinel_count = output.count(sentinel_bytes)
    assertion_count = output.count(assertion_bytes)
    sentinel_position = output.find(sentinel_bytes)
    assertion_position = output.find(assertion_bytes)

    failure_reasons = []
    if process_result["launch_error"] is not None:
        failure_reasons.append("studio_launch_failed")
    if process_result["lifecycle_error"] is not None:
        failure_reasons.append("studio_lifecycle_not_bounded")
    if process_result["timed_out"] is True:
        failure_reasons.append("studio_timed_out")
    if process_result["log_limit_exceeded"] is True:
        failure_reasons.append("studio_log_limit_exceeded")
    if process_result["returncode"] is None:
        failure_reasons.append("studio_returncode_missing")
    if any(
        process_result[key] is True
        for key in (
            "stdout_truncated",
            "stderr_truncated",
            "studio_output_truncated",
        )
    ):
        failure_reasons.append("studio_evidence_truncated")
    if sentinel_count != 1:
        failure_reasons.append("prefix_witness_missing_or_duplicated")
    if assertion_count != 1:
        failure_reasons.append("terminal_assertion_missing_or_duplicated")
    if (
        sentinel_position < 0
        or assertion_position < 0
        or sentinel_position >= assertion_position
    ):
        failure_reasons.append("witness_order_invalid")
    if prefix_guard_failures:
        failure_reasons.append("compile_environment_guard_failed")
    if compile_markers:
        failure_reasons.append("native_compile_error_observed")
    if user_plugin_hits:
        failure_reasons.append("user_plugin_loaded")
    if post_audit_error is not None:
        failure_reasons.append("post_process_audit_failed")
    if running_after:
        failure_reasons.append("studio_process_remained_running")
    failure_reasons = list(dict.fromkeys(failure_reasons))
    ok = not failure_reasons

    logs = {
        "studio_output": _log_receipt(
            path=evidence_paths["output"],
            value=output,
            produced_bytes=int(process_result["studio_output_size"]),
            truncated=bool(process_result["studio_output_truncated"]),
        ),
        "studio_stderr": _log_receipt(
            path=evidence_paths["stderr"],
            value=stderr,
            produced_bytes=int(process_result["stderr_size"]),
            truncated=bool(process_result["stderr_truncated"]),
        ),
        "studio_stdout": _log_receipt(
            path=evidence_paths["stdout"],
            value=stdout,
            produced_bytes=int(process_result["stdout_size"]),
            truncated=bool(process_result["stdout_truncated"]),
        ),
    }
    result: Dict[str, object] = {
        "command_contract": {
            "arguments": list(COMMAND_ARGUMENT_CONTRACT),
            "disable_user_plugins": True,
            "environment_allowlist": list(SAFE_ENVIRONMENT_KEYS),
            "no_place_argument": True,
            "quit_after_execution": True,
            "task": "RunScript",
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "failure_reasons": failure_reasons,
        "format": PROOF_FORMAT,
        "guard_contract": guard_contract,
        "logs": logs,
        "observations": {
            "compile_error_markers": compile_markers,
            "prefix_guard_failures": prefix_guard_failures,
            "prefix_witness_count_in_output": sentinel_count,
            "terminal_assertion_count_in_output": assertion_count,
            "user_plugin_log_hits": user_plugin_hits,
            "witness_precedes_terminal_assertion": (
                sentinel_position >= 0
                and assertion_position >= 0
                and sentinel_position < assertion_position
            ),
        },
        "ok": ok,
        "package": {
            "bytes": len(package_bytes),
            "expected_sha256": package_expected,
            "path": str(package),
            "sha256": package_sha256,
            "source_bytes": len(source_bytes),
            "source_expected_sha256": source_expected,
            "source_sha256": source_sha256,
        },
        "process": {
            "launch_error": process_result["launch_error"],
            "lifecycle_error": process_result["lifecycle_error"],
            "log_limit_exceeded": process_result["log_limit_exceeded"],
            "returncode": process_result["returncode"],
            "running_after": running_after,
            "running_before": running_before,
            "running_process_audit_after_error": post_audit_error,
            "timed_out": process_result["timed_out"],
            "timeout_seconds": timeout_seconds,
        },
        "runner": {
            "candidate_source_exact_after_prefix": (
                runner_source[len(prefix) :] == source_bytes
            ),
            "candidate_source_line_offset": prefix.count(b"\n"),
            "candidate_source_sha256": _sha256_bytes(
                runner_source[len(prefix) :]
            ),
            "main_chunk_not_wrapped": True,
            "prefix_has_local_declarations": False,
            "prefix_lines": prefix.count(b"\n"),
            "prefix_sha256": _sha256_bytes(prefix),
            "runner_sha256": _sha256_bytes(runner_source),
            "terminal_contract": "reviewed_plugin_context_assertion",
            "witness_sha256": _sha256_bytes(sentinel_bytes),
        },
        "studio": studio_identity,
    }
    encoded = (
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_private_new(receipt, encoded)
    if not ok:
        raise NativeCompileError(
            "native Studio compilation proof failed; see " + str(receipt)
        )
    return result


def _strict_json_object(pairs: list[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NativeCompileError(
                "native compile receipt contains a duplicate JSON key"
            )
        result[key] = value
    return result


def _receipt_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise NativeCompileError(label + " must be a JSON object")
    return value


def _require_receipt_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise NativeCompileError(label + " schema drifted")


def _read_private_receipt_file(
    path: Path,
    *,
    label: str,
    maximum: int,
    allow_empty: bool,
) -> bytes:
    resolved = _canonical_regular(path, label)
    details = resolved.lstat()
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise NativeCompileError(label + " must have mode 0600")
    if (
        details.st_size < (0 if allow_empty else 1)
        or details.st_size > maximum
    ):
        raise NativeCompileError(label + " size is outside the bound")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        chunks = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
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
        len(value) != before.st_size
        or len(value) > maximum
        or any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        )
    ):
        raise NativeCompileError(label + " changed while it was validated")
    return value


def _validated_receipt_logs(
    receipt: Path,
    logs_value: object,
) -> Dict[str, bytes]:
    logs = _receipt_mapping(logs_value, "native compile receipt logs")
    expected_logs = {"studio_output", "studio_stderr", "studio_stdout"}
    _require_receipt_keys(
        logs,
        expected_logs,
        "native compile receipt logs",
    )
    specifications = {
        "studio_output": (
            _receipt_log_path(receipt, ".studio-output.log"),
            "native compile Studio output evidence",
        ),
        "studio_stderr": (
            _receipt_log_path(receipt, ".studio-stderr.log"),
            "native compile Studio stderr evidence",
        ),
        "studio_stdout": (
            _receipt_log_path(receipt, ".studio-stdout.log"),
            "native compile Studio stdout evidence",
        ),
    }
    values: Dict[str, bytes] = {}
    for key, (expected_path, label) in specifications.items():
        metadata = _receipt_mapping(logs[key], label + " metadata")
        _require_receipt_keys(
            metadata,
            {
                "captured_bytes",
                "path",
                "produced_bytes",
                "sha256",
                "truncated",
            },
            label + " metadata",
        )
        if metadata["path"] != str(expected_path):
            raise NativeCompileError(label + " path drifted")
        value = _read_private_receipt_file(
            expected_path,
            label=label,
            maximum=MAX_PROCESS_LOG_BYTES,
            allow_empty=True,
        )
        if (
            not isinstance(metadata["captured_bytes"], int)
            or isinstance(metadata["captured_bytes"], bool)
            or metadata["captured_bytes"] != len(value)
            or not isinstance(metadata["produced_bytes"], int)
            or isinstance(metadata["produced_bytes"], bool)
            or metadata["produced_bytes"] != len(value)
            or metadata["truncated"] is not False
            or _validate_sha256(
                metadata["sha256"],
                label + " SHA-256",
            )
            != _sha256_bytes(value)
        ):
            raise NativeCompileError(label + " metadata does not match")
        values[key] = value
    return values


def validate_native_compile_receipt(
    receipt_path: Path,
    *,
    package_path: Path,
    expected_package_sha256: str,
    expected_source_sha256: str,
    studio_executable: Path = DEFAULT_STUDIO_EXECUTABLE,
) -> Dict[str, object]:
    """Validate a successful exact-artifact receipt without launching Studio."""

    package_expected = _validate_sha256(
        expected_package_sha256,
        "expected package SHA-256",
    )
    source_expected = _validate_sha256(
        expected_source_sha256,
        "expected source SHA-256",
    )
    receipt = _canonical_regular(
        Path(receipt_path),
        "native compile receipt",
    )
    receipt_bytes = _read_private_receipt_file(
        receipt,
        label="native compile receipt",
        maximum=MAX_RECEIPT_BYTES,
        allow_empty=False,
    )
    try:
        receipt_text = receipt_bytes.decode("utf-8", "strict")
        decoded = json.loads(
            receipt_text,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeCompileError("native compile receipt JSON is invalid") from exc
    result = _receipt_mapping(decoded, "native compile receipt")
    _require_receipt_keys(
        result,
        RECEIPT_TOP_LEVEL_KEYS,
        "native compile receipt",
    )
    if (
        result["format"] != PROOF_FORMAT
        or result["ok"] is not True
        or result["failure_reasons"] != []
    ):
        raise NativeCompileError("native compile receipt is not successful")
    created_at = result["created_at"]
    if not isinstance(created_at, str):
        raise NativeCompileError("native compile receipt timestamp is invalid")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise NativeCompileError(
            "native compile receipt timestamp is invalid"
        ) from exc
    if (
        created.tzinfo is None
        or created.utcoffset() != timezone.utc.utcoffset(created)
    ):
        raise NativeCompileError(
            "native compile receipt timestamp must be UTC"
        )

    command = _receipt_mapping(
        result["command_contract"],
        "native compile command contract",
    )
    _require_receipt_keys(
        command,
        {
            "arguments",
            "disable_user_plugins",
            "environment_allowlist",
            "no_place_argument",
            "quit_after_execution",
            "task",
        },
        "native compile command contract",
    )
    if (
        command["arguments"] != list(COMMAND_ARGUMENT_CONTRACT)
        or command["disable_user_plugins"] is not True
        or command["environment_allowlist"] != list(SAFE_ENVIRONMENT_KEYS)
        or command["no_place_argument"] is not True
        or command["quit_after_execution"] is not True
        or command["task"] != "RunScript"
    ):
        raise NativeCompileError("native compile command contract is not positive")

    package = _canonical_regular(Path(package_path), "plugin package")
    package_bytes, source_bytes = extract_exact_main_source(package)
    package_sha256 = _sha256_bytes(package_bytes)
    source_sha256 = _sha256_bytes(source_bytes)
    if (
        package_sha256 != package_expected
        or source_sha256 != source_expected
    ):
        raise NativeCompileError(
            "native compile receipt artifact input does not match expected hashes"
        )
    package_receipt = _receipt_mapping(
        result["package"],
        "native compile receipt package",
    )
    _require_receipt_keys(
        package_receipt,
        {
            "bytes",
            "expected_sha256",
            "path",
            "sha256",
            "source_bytes",
            "source_expected_sha256",
            "source_sha256",
        },
        "native compile receipt package",
    )
    expected_package_receipt = {
        "bytes": len(package_bytes),
        "expected_sha256": package_expected,
        "path": str(package),
        "sha256": package_sha256,
        "source_bytes": len(source_bytes),
        "source_expected_sha256": source_expected,
        "source_sha256": source_sha256,
    }
    if dict(package_receipt) != expected_package_receipt:
        raise NativeCompileError("native compile receipt package identity drifted")

    guard_contract = validate_candidate_guard_contract(source_bytes)
    if result["guard_contract"] != guard_contract:
        raise NativeCompileError("native compile receipt guard contract drifted")

    log_values = _validated_receipt_logs(receipt, result["logs"])
    output = log_values["studio_output"]
    stderr = log_values["studio_stderr"]
    stdout = log_values["studio_stdout"]
    combined = stdout + b"\n" + stderr + b"\n" + output
    combined_lower = combined.lower()
    witnesses = list(
        re.finditer(
            rb"STUDIO_MCP_V2_NATIVE_COMPILE_PREFIX_OK:"
            rb"([0-9a-f]{32}):([0-9a-f]{64}):([0-9a-f]{64})",
            output,
        )
    )
    if len(witnesses) != 1:
        raise NativeCompileError(
            "native compile receipt witness is missing or duplicated"
        )
    witness = witnesses[0]
    nonce = witness.group(1).decode("ascii")
    if (
        witness.group(2).decode("ascii") != package_sha256
        or witness.group(3).decode("ascii") != source_sha256
    ):
        raise NativeCompileError(
            "native compile receipt witness artifact identity drifted"
        )
    prefix, sentinel = _guard_prefix(
        nonce=nonce,
        package_sha256=package_sha256,
        source_sha256=source_sha256,
    )
    sentinel_bytes = sentinel.encode("ascii")
    assertion_bytes = EXPECTED_MAIN_ASSERTION_MESSAGE.encode("utf-8")
    compile_markers = [
        marker.decode("ascii")
        for marker in COMPILE_ERROR_MARKERS
        if marker in combined_lower
    ]
    prefix_guard_failures = [
        marker
        for marker in PREFIX_FAILURE_MESSAGES
        if marker.encode("utf-8") in combined
    ]
    user_plugin_hits = sorted(
        {
            match.group(0).decode("utf-8", "replace")
            for match in USER_PLUGIN_RE.finditer(combined)
        }
    )
    witness_precedes_assertion = (
        output.find(sentinel_bytes) >= 0
        and output.find(assertion_bytes) >= 0
        and output.find(sentinel_bytes) < output.find(assertion_bytes)
    )
    observations = _receipt_mapping(
        result["observations"],
        "native compile receipt observations",
    )
    _require_receipt_keys(
        observations,
        {
            "compile_error_markers",
            "prefix_guard_failures",
            "prefix_witness_count_in_output",
            "terminal_assertion_count_in_output",
            "user_plugin_log_hits",
            "witness_precedes_terminal_assertion",
        },
        "native compile receipt observations",
    )
    expected_observations = {
        "compile_error_markers": compile_markers,
        "prefix_guard_failures": prefix_guard_failures,
        "prefix_witness_count_in_output": output.count(sentinel_bytes),
        "terminal_assertion_count_in_output": output.count(assertion_bytes),
        "user_plugin_log_hits": user_plugin_hits,
        "witness_precedes_terminal_assertion": witness_precedes_assertion,
    }
    if (
        dict(observations) != expected_observations
        or compile_markers
        or prefix_guard_failures
        or user_plugin_hits
        or expected_observations["prefix_witness_count_in_output"] != 1
        or expected_observations["terminal_assertion_count_in_output"] != 1
        or witness_precedes_assertion is not True
    ):
        raise NativeCompileError(
            "native compile receipt observations are not positive"
        )

    process = _receipt_mapping(
        result["process"],
        "native compile receipt process",
    )
    _require_receipt_keys(
        process,
        {
            "launch_error",
            "lifecycle_error",
            "log_limit_exceeded",
            "returncode",
            "running_after",
            "running_before",
            "running_process_audit_after_error",
            "timed_out",
            "timeout_seconds",
        },
        "native compile receipt process",
    )
    returncode = process["returncode"]
    timeout_seconds = process["timeout_seconds"]
    if (
        process["launch_error"] is not None
        or process["lifecycle_error"] is not None
        or process["log_limit_exceeded"] is not False
        or not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or process["running_after"] != []
        or process["running_before"] != []
        or process["running_process_audit_after_error"] is not None
        or process["timed_out"] is not False
        or not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not MIN_TIMEOUT_SECONDS
        <= timeout_seconds
        <= MAX_TIMEOUT_SECONDS
    ):
        raise NativeCompileError(
            "native compile receipt process contract is not positive"
        )

    runner = _receipt_mapping(
        result["runner"],
        "native compile receipt runner",
    )
    _require_receipt_keys(
        runner,
        {
            "candidate_source_exact_after_prefix",
            "candidate_source_line_offset",
            "candidate_source_sha256",
            "main_chunk_not_wrapped",
            "prefix_has_local_declarations",
            "prefix_lines",
            "prefix_sha256",
            "runner_sha256",
            "terminal_contract",
            "witness_sha256",
        },
        "native compile receipt runner",
    )
    prefix_lines = prefix.count(b"\n")
    expected_runner = {
        "candidate_source_exact_after_prefix": True,
        "candidate_source_line_offset": prefix_lines,
        "candidate_source_sha256": source_sha256,
        "main_chunk_not_wrapped": True,
        "prefix_has_local_declarations": False,
        "prefix_lines": prefix_lines,
        "prefix_sha256": _sha256_bytes(prefix),
        "runner_sha256": _sha256_bytes(prefix + source_bytes),
        "terminal_contract": "reviewed_plugin_context_assertion",
        "witness_sha256": _sha256_bytes(sentinel_bytes),
    }
    if dict(runner) != expected_runner:
        raise NativeCompileError("native compile receipt runner identity drifted")

    studio_receipt = _receipt_mapping(
        result["studio"],
        "native compile receipt Studio identity",
    )
    _require_receipt_keys(
        studio_receipt,
        {
            "bundle_executable",
            "bundle_id",
            "bundle_path",
            "bundle_short_version",
            "bundle_version",
            "executable_path",
            "executable_sha256",
            "info_plist_sha256",
            "resource_integrity_verified",
            "signature_cdhash_full",
            "signature_identifier",
            "signature_leaf_authority",
            "signature_requirement",
            "signature_scope",
            "team_identifier",
        },
        "native compile receipt Studio identity",
    )
    executable = _canonical_regular(
        Path(studio_executable),
        "Studio executable",
    )
    if studio_receipt["executable_path"] != str(executable):
        raise NativeCompileError(
            "native compile receipt Studio executable path drifted"
        )
    studio_sha256 = _validate_sha256(
        studio_receipt["executable_sha256"],
        "native compile receipt Studio executable SHA-256",
    )
    if (
        _sha256_regular_file(
            executable,
            "Studio executable",
            MAX_EXECUTABLE_BYTES,
        )
        != studio_sha256
    ):
        raise NativeCompileError(
            "native compile receipt Studio executable bytes drifted"
        )
    current_identity = inspect_studio_identity(
        executable,
        expected_executable_sha256=studio_sha256,
    )
    if dict(studio_receipt) != current_identity:
        raise NativeCompileError(
            "native compile receipt Studio signed identity drifted"
        )
    return dict(result)
