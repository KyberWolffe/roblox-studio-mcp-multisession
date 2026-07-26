"""Fail-closed audits for the publishable repository and release archives.

The auditor deliberately reports finding categories and locations without
echoing matched values.  It is intended to be safe to run in CI logs even when
the input accidentally contains a credential.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import pwd
import re
import stat
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_FILES = 512

_ALLOWED_REPOSITORY_DIRECTORIES = {
    ".github",
    "bootstrap",
    "config",
    "docs",
    "packaging",
    "release_tools",
    "scripts",
    "studio_mcp_v2",
    "tests",
    "tools",
}
_ALLOWED_REPOSITORY_ROOT_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE_STATUS.md",
    "README.md",
    "SECURITY.md",
    "VERSION",
    "platform_support.py",
    "pyproject.toml",
}
_ALLOWED_SUFFIXES = {
    ".json",
    ".luau",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_FORBIDDEN_COMPONENTS = {
    ".coverage",
    ".env",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "backups",
    "build",
    "dist",
    "live-state",
    "logs",
    "node_modules",
    "receipts",
    "rollback-receipts",
    "run",
    "runtime-state",
    "venv",
}
_FORBIDDEN_EXACT_FILENAMES = {
    ".DS_Store",
    "client-context.json",
    "host-context.json",
    "install-state.json",
    "run-manifest.json",
    "runtime.json",
    "secrets.json",
}
_FORBIDDEN_FILENAME_SUFFIXES = {
    ".bak",
    ".core",
    ".dump",
    ".key",
    ".log",
    ".orig",
    ".pem",
    ".pid",
    ".pyc",
    ".pyo",
    ".receipt",
    ".sqlite",
    ".sqlite3",
    ".swp",
    ".token",
}
_FORBIDDEN_PATH_MARKERS = (
    "live-v2-run",
    "catalog-import-receipt-",
)

_MAC_HOME = re.compile(rb"(?<![A-Za-z0-9_])/Users/[A-Za-z0-9._-]+(?:/|\\b)")
_UNIX_HOME = re.compile(rb"(?<![A-Za-z0-9_])/home/[A-Za-z0-9._-]+(?:/|\\b)")
_WINDOWS_HOME = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])[A-Z]:\\\\Users\\\\[A-Za-z0-9._-]+(?:\\\\|\\b)"
)
_PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_TOKEN_PREFIX = re.compile(
    rb"(?<![A-Za-z0-9])(?:"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    rb"xox[baprs]-[A-Za-z0-9-]{16,}|"
    rb"AKIA[0-9A-Z]{16}"
    rb")"
)
_BEARER_LITERAL = re.compile(
    rb"(?i)authorization[\"']?\\s*(?:=|:)\\s*[\"']?"
    rb"bearer\\s+([A-Za-z0-9._~+/-]{16,})"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    rb"(?i)[\"']?(?:"
    rb"access_token|api_key|bearer_token|bridge_token|broker_id|"
    rb"client_secret|client_token|credential|github_token|install_run_id|"
    rb"password|private_key|registration_secret|resume_token|server_token|"
    rb"session_id|studio_bearer_token|studio_id|studio_token"
    rb")[\"']?\\s*(?:=|:)\\s*[\"']([^\"'\\r\\n]{8,})[\"']"
)
_UUID = re.compile(
    rb"(?i)\\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    rb"[89ab][0-9a-f]{3}-[0-9a-f]{12}\\b"
)
_UUID_TEXT = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PLACEHOLDER = re.compile(
    rb"(?:"
    rb"__[A-Z0-9_]+__|"
    rb"\\$\\{[A-Za-z_][A-Za-z0-9_]*\\}|"
    rb"<[A-Za-z0-9_.-]+>|"
    rb"(?:example|placeholder|replace-me|redacted|synthetic)[A-Za-z0-9_.:-]*"
    rb")",
    re.IGNORECASE,
)
_SENSITIVE_JSON_KEY = re.compile(
    r"(?i)(?:"
    r".*(?:password|private_key|secret|token|credential)|"
    r"authorization|broker_id|install_run_id|session_id|studio_id"
    r")$"
)


@dataclass(frozen=True)
class AuditFinding:
    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class AuditReport:
    kind: str
    target: str
    files_checked: int
    bytes_checked: int
    findings: Tuple[AuditFinding, ...]
    sha256: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "target": self.target,
            "files_checked": self.files_checked,
            "bytes_checked": self.bytes_checked,
            "sha256": self.sha256,
            "findings": [asdict(item) for item in self.findings],
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_current_identity() -> Tuple[bytes, ...]:
    """Return current-machine identifiers without embedding them in source."""

    values = set()
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        values.add(str(home).encode("utf-8"))
        values.add(home.name.encode("utf-8"))
    except (KeyError, OSError, UnicodeError):
        pass
    try:
        name = getpass.getuser()
        if name:
            values.add(name.encode("utf-8"))
    except (OSError, UnicodeError):
        pass
    generic = {
        b"admin",
        b"build",
        b"ci",
        b"root",
        b"runner",
        b"user",
    }
    return tuple(
        value
        for value in values
        if len(value) >= 4 and value.lower() not in generic
    )


def _is_placeholder(value: bytes) -> bool:
    stripped = value.strip()
    return _PLACEHOLDER.fullmatch(stripped) is not None


def _is_repeated_test_value(value: bytes) -> bool:
    stripped = value.strip()
    return len(stripped) >= 8 and len(set(stripped)) == 1


def _is_synthetic_path(relative: PurePosixPath) -> bool:
    parts = relative.parts
    return (
        len(parts) >= 3
        and parts[0] == "tests"
        and parts[1] == "fixtures"
        and "synthetic" in parts
    )


def _path_findings(
    relative: PurePosixPath,
    *,
    repository: bool,
) -> List[AuditFinding]:
    rendered = relative.as_posix()
    lowered_parts = tuple(part.lower() for part in relative.parts)
    findings: List[AuditFinding] = []
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in rendered
    ):
        findings.append(
            AuditFinding("unsafe_path", rendered, "path is not a safe relative path")
        )
        return findings
    if any(part in _FORBIDDEN_COMPONENTS for part in lowered_parts):
        findings.append(
            AuditFinding(
                "runtime_or_build_material",
                rendered,
                "path contains a forbidden runtime, backup, log, or build component",
            )
        )
    filename = relative.name
    lowered_name = filename.lower()
    if lowered_name in _FORBIDDEN_EXACT_FILENAMES or any(
        lowered_name.endswith(suffix) for suffix in _FORBIDDEN_FILENAME_SUFFIXES
    ):
        findings.append(
            AuditFinding(
                "runtime_or_secret_file",
                rendered,
                "filename is reserved for local runtime or sensitive material",
            )
        )
    if any(marker in rendered.lower() for marker in _FORBIDDEN_PATH_MARKERS):
        findings.append(
            AuditFinding(
                "live_state_path",
                rendered,
                "path resembles a live-run or rollback-receipt artifact",
            )
        )
    if repository:
        if len(relative.parts) == 1:
            if filename not in _ALLOWED_REPOSITORY_ROOT_FILES:
                findings.append(
                    AuditFinding(
                        "unexpected_repository_file",
                        rendered,
                        "root file is outside the publishable allowlist",
                    )
                )
        elif relative.parts[0] not in _ALLOWED_REPOSITORY_DIRECTORIES:
            findings.append(
                AuditFinding(
                    "unexpected_repository_directory",
                    rendered,
                    "top-level directory is outside the publishable allowlist",
                )
            )
        if (
            filename not in {".gitignore", ".gitattributes"}
            and Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES
        ):
            findings.append(
                AuditFinding(
                    "unexpected_file_type",
                    rendered,
                    "file type is not in the source/documentation allowlist",
                )
            )
    return findings


def _json_findings(
    relative: PurePosixPath,
    value: Any,
    *,
    synthetic: bool,
) -> List[AuditFinding]:
    findings: List[AuditFinding] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                child_path = path + "." + str(key)
                if (
                    isinstance(key, str)
                    and _SENSITIVE_JSON_KEY.fullmatch(key)
                    and isinstance(child, str)
                    and child
                ):
                    encoded = child.encode("utf-8", "replace")
                    allowed = _is_placeholder(encoded) or (
                        synthetic
                        and (
                            child.lower().startswith("synthetic")
                            or _is_repeated_test_value(encoded)
                            or _UUID_TEXT.fullmatch(child) is not None
                        )
                    )
                    if not allowed:
                        findings.append(
                            AuditFinding(
                                "sensitive_json_value",
                                relative.as_posix(),
                                "concrete sensitive identifier or credential at "
                                + child_path,
                            )
                        )
                walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, path + "[" + str(index) + "]")

    walk(value, "$")
    return findings


def _content_findings(
    relative: PurePosixPath,
    data: bytes,
    *,
    current_identity: Sequence[bytes],
) -> List[AuditFinding]:
    path = relative.as_posix()
    findings: List[AuditFinding] = []
    synthetic = _is_synthetic_path(relative)
    if b"\0" in data:
        findings.append(
            AuditFinding(
                "binary_content",
                path,
                "publishable source files must be text",
            )
        )
        return findings
    for code, pattern, detail in (
        ("absolute_macos_user_path", _MAC_HOME, "contains an absolute macOS user path"),
        ("absolute_unix_user_path", _UNIX_HOME, "contains an absolute user home path"),
        (
            "absolute_windows_user_path",
            _WINDOWS_HOME,
            "contains an absolute Windows user path",
        ),
        ("private_key", _PRIVATE_KEY, "contains private-key material"),
        ("credential_prefix", _TOKEN_PREFIX, "contains a recognized credential form"),
    ):
        if pattern.search(data):
            findings.append(AuditFinding(code, path, detail))
    for identity in current_identity:
        if identity in data:
            findings.append(
                AuditFinding(
                    "current_machine_identity",
                    path,
                    "contains the current machine home path or account name",
                )
            )
            break
    if _BEARER_LITERAL.search(data):
        findings.append(
            AuditFinding(
                "bearer_credential",
                path,
                "contains a concrete Authorization bearer value",
            )
        )
    for match in _SENSITIVE_ASSIGNMENT.finditer(data):
        value = match.group(1).strip()
        if _is_placeholder(value):
            continue
        if (
            (synthetic or relative.parts[0] == "tests")
            and _is_repeated_test_value(value)
        ):
            continue
        findings.append(
            AuditFinding(
                "sensitive_assignment",
                path,
                "contains a concrete credential or local routing identifier",
            )
        )
        break
    # UUIDs are common as documentation examples. They become state-like only
    # when the file also names a concrete local routing identity.
    if _UUID.search(data) and re.search(
        rb"(?i)(?:broker_id|session_id|studio_id|install_run_id)\\s*(?:=|:)",
        data,
    ):
        findings.append(
            AuditFinding(
                "concrete_runtime_identifier",
                path,
                "contains a concrete session, broker, Studio, or install identifier",
            )
        )
    if relative.suffix.lower() == ".json":
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            findings.append(
                AuditFinding("invalid_json", path, "JSON source is not valid UTF-8 JSON")
            )
        else:
            findings.extend(_json_findings(relative, parsed, synthetic=synthetic))
    return findings


def audit_repository(root: Path) -> AuditReport:
    """Audit a candidate Git worktree, excluding only the repository metadata."""

    target = Path(root).resolve(strict=True)
    if not target.is_dir():
        raise ValueError("repository audit target must be a directory")
    findings: List[AuditFinding] = []
    files_checked = 0
    bytes_checked = 0
    identity = _safe_current_identity()
    for directory, directory_names, filenames in os.walk(target, topdown=True):
        base = Path(directory)
        relative_base = base.relative_to(target)
        if relative_base == Path("."):
            directory_names[:] = sorted(
                name for name in directory_names if name != ".git"
            )
        else:
            directory_names[:] = sorted(directory_names)
        filenames.sort()
        for name in directory_names:
            candidate = base / name
            relative = PurePosixPath(candidate.relative_to(target).as_posix())
            lowered_parts = tuple(part.lower() for part in relative.parts)
            if any(part in _FORBIDDEN_COMPONENTS for part in lowered_parts):
                findings.append(
                    AuditFinding(
                        "runtime_or_build_material",
                        relative.as_posix(),
                        "directory is reserved for runtime, backup, log, or build material",
                    )
                )
            if relative.parts[0] not in _ALLOWED_REPOSITORY_DIRECTORIES:
                findings.append(
                    AuditFinding(
                        "unexpected_repository_directory",
                        relative.as_posix(),
                        "top-level directory is outside the publishable allowlist",
                    )
                )
            if candidate.is_symlink():
                findings.append(
                    AuditFinding(
                        "symlink",
                        relative.as_posix(),
                        "repository symlinks are not permitted",
                    )
                )
        for name in filenames:
            candidate = base / name
            relative = PurePosixPath(candidate.relative_to(target).as_posix())
            findings.extend(_path_findings(relative, repository=True))
            try:
                details = candidate.lstat()
            except OSError:
                findings.append(
                    AuditFinding("unreadable", relative.as_posix(), "file is unreadable")
                )
                continue
            if stat.S_ISLNK(details.st_mode):
                findings.append(
                    AuditFinding(
                        "symlink",
                        relative.as_posix(),
                        "repository symlinks are not permitted",
                    )
                )
                continue
            if not stat.S_ISREG(details.st_mode):
                findings.append(
                    AuditFinding(
                        "non_regular",
                        relative.as_posix(),
                        "only regular source files are permitted",
                    )
                )
                continue
            if details.st_size > MAX_FILE_BYTES:
                findings.append(
                    AuditFinding(
                        "oversized_file",
                        relative.as_posix(),
                        "file exceeds the repository audit size bound",
                    )
                )
                continue
            try:
                data = candidate.read_bytes()
            except OSError:
                findings.append(
                    AuditFinding("unreadable", relative.as_posix(), "file is unreadable")
                )
                continue
            files_checked += 1
            bytes_checked += len(data)
            findings.extend(
                _content_findings(relative, data, current_identity=identity)
            )
    return AuditReport(
        kind="repository",
        target=str(target),
        files_checked=files_checked,
        bytes_checked=bytes_checked,
        findings=tuple(findings),
    )


def _manifest_file_map(manifest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise ValueError("release manifest files must be a list")
    result: Dict[str, Mapping[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise ValueError("release manifest file entry must be an object")
        relative = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        mode = item.get("mode")
        if (
            not isinstance(relative, str)
            or not relative
            or relative in result
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(size, int)
            or size < 0
            or not isinstance(mode, int)
            or mode not in {0o644, 0o755}
        ):
            raise ValueError("release manifest has an invalid file entry")
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("release manifest contains an unsafe path")
        result[relative] = item
    return result


def audit_archive(archive: Path) -> AuditReport:
    """Audit a deterministic release tarball and its internal manifest."""

    target = Path(archive).resolve(strict=True)
    if not target.is_file() or target.is_symlink():
        raise ValueError("archive audit target must be a regular file")
    outer_size = target.stat().st_size
    outer_bytes = target.read_bytes()
    findings: List[AuditFinding] = []
    if outer_size > MAX_ARCHIVE_BYTES:
        findings.append(
            AuditFinding(
                "oversized_archive",
                target.name,
                "archive exceeds the audit size bound",
            )
        )
    archive_sha = hashlib.sha256(outer_bytes).hexdigest()
    if (
        len(outer_bytes) < 10
        or outer_bytes[:2] != b"\x1f\x8b"
        or outer_bytes[4:8] != b"\0\0\0\0"
    ):
        findings.append(
            AuditFinding(
                "nondeterministic_gzip_header",
                target.name,
                "gzip header is missing or has a nonzero timestamp",
            )
        )
    entries: Dict[str, Tuple[tarfile.TarInfo, bytes]] = {}
    roots = set()
    total_bytes = 0
    try:
        package = tarfile.open(target, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("unable to read release archive: " + str(exc)) from exc
    with package:
        members = package.getmembers()
        names_in_order = [member.name for member in members]
        if names_in_order != sorted(names_in_order):
            findings.append(
                AuditFinding(
                    "nondeterministic_archive_order",
                    target.name,
                    "archive entries are not lexicographically ordered",
                )
            )
        if len(members) > MAX_ARCHIVE_FILES:
            findings.append(
                AuditFinding(
                    "too_many_archive_entries",
                    target.name,
                    "archive exceeds the entry-count bound",
                )
            )
        for member in members:
            raw_name = member.name
            path = PurePosixPath(raw_name)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or "\\" in raw_name
            ):
                findings.append(
                    AuditFinding(
                        "unsafe_archive_path",
                        raw_name,
                        "archive member path is unsafe",
                    )
                )
                continue
            roots.add(path.parts[0])
            if not member.isfile():
                findings.append(
                    AuditFinding(
                        "non_regular_archive_entry",
                        raw_name,
                        "archive may contain regular files only",
                    )
                )
                continue
            if raw_name in entries:
                findings.append(
                    AuditFinding(
                        "duplicate_archive_entry",
                        raw_name,
                        "archive contains a duplicate path",
                    )
                )
                continue
            if (
                member.size > MAX_FILE_BYTES
                or member.size < 0
                or total_bytes + member.size > MAX_ARCHIVE_BYTES
            ):
                findings.append(
                    AuditFinding(
                        "oversized_archive_entry",
                        raw_name,
                        "archive member exceeds an extraction bound",
                    )
                )
                continue
            if (
                member.mtime != 0
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
            ):
                findings.append(
                    AuditFinding(
                        "nondeterministic_metadata",
                        raw_name,
                        "archive ownership or timestamp metadata is not normalized",
                    )
                )
            if member.pax_headers:
                findings.append(
                    AuditFinding(
                        "unexpected_pax_metadata",
                        raw_name,
                        "archive member has nonessential extended metadata",
                    )
                )
            if member.mode not in {0o644, 0o755}:
                findings.append(
                    AuditFinding(
                        "unsafe_archive_mode",
                        raw_name,
                        "archive member mode is outside 0644/0755",
                    )
                )
            stream = package.extractfile(member)
            if stream is None:
                findings.append(
                    AuditFinding(
                        "unreadable_archive_entry",
                        raw_name,
                        "archive member cannot be read",
                    )
                )
                continue
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) != member.size:
                findings.append(
                    AuditFinding(
                        "archive_size_mismatch",
                        raw_name,
                        "archive member size does not match extracted bytes",
                    )
                )
                continue
            entries[raw_name] = (member, data)
            total_bytes += len(data)

    if len(roots) != 1:
        findings.append(
            AuditFinding(
                "archive_root",
                target.name,
                "archive must contain exactly one root directory",
            )
        )
        root = ""
    else:
        root = next(iter(roots))
        expected_suffix = "-macos-arm64"
        if not root.startswith("roblox-studio-mcp-v2-") or not root.endswith(
            expected_suffix
        ):
            findings.append(
                AuditFinding(
                    "archive_root",
                    root,
                    "archive root must be a versioned macos-arm64 package",
                )
            )
        if target.name != root + ".tar.gz":
            findings.append(
                AuditFinding(
                    "archive_filename",
                    target.name,
                    "archive filename must match its versioned package root",
                )
            )
    manifest_name = root + "/release-manifest.json" if root else ""
    manifest_item = entries.get(manifest_name)
    manifest_files: Dict[str, Mapping[str, Any]] = {}
    if manifest_item is None:
        findings.append(
            AuditFinding(
                "missing_manifest",
                target.name,
                "archive lacks release-manifest.json",
            )
        )
    else:
        try:
            manifest = json.loads(manifest_item[1].decode("utf-8"))
            if not isinstance(manifest, Mapping):
                raise ValueError("manifest must be an object")
            manifest_files = _manifest_file_map(manifest)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            findings.append(
                AuditFinding("invalid_manifest", manifest_name, str(exc))
            )
            manifest = {}
        expected_manifest_keys = {
            "files",
            "format",
            "manifest_version",
            "platform",
            "product",
            "python_requires",
            "source_date_epoch",
            "version",
        }
        if set(manifest) != expected_manifest_keys:
            findings.append(
                AuditFinding(
                    "manifest_fields",
                    manifest_name,
                    "release manifest fields differ from the reviewed format",
                )
            )
        if (
            manifest.get("format")
            != "roblox-studio-mcp-v2-portable-release"
            or manifest.get("manifest_version") != 1
            or manifest.get("product") != "RobloxStudioMCPv2"
            or manifest.get("python_requires") != ">=3.9"
            or manifest.get("source_date_epoch") != 0
        ):
            findings.append(
                AuditFinding(
                    "manifest_identity",
                    manifest_name,
                    "release manifest identity or reproducibility fields are invalid",
                )
            )
        if manifest.get("platform") != "macos-arm64":
            findings.append(
                AuditFinding(
                    "unsupported_manifest_platform",
                    manifest_name,
                    "release manifest must declare macos-arm64",
                )
            )
        if root:
            version_from_root = root[
                len("roblox-studio-mcp-v2-") : -len("-macos-arm64")
            ]
            if manifest.get("version") != version_from_root:
                findings.append(
                    AuditFinding(
                        "manifest_version",
                        manifest_name,
                        "manifest version does not match the archive root",
                    )
                )
        findings.extend(
            _content_findings(
                PurePosixPath("release-manifest.json"),
                manifest_item[1],
                current_identity=_safe_current_identity(),
            )
        )

    identity = _safe_current_identity()
    observed_relative = set()
    for raw_name, (member, data) in sorted(entries.items()):
        if raw_name == manifest_name or not root:
            continue
        prefix = root + "/"
        if not raw_name.startswith(prefix):
            findings.append(
                AuditFinding(
                    "archive_root_escape",
                    raw_name,
                    "archive member is outside the package root",
                )
            )
            continue
        relative_text = raw_name[len(prefix) :]
        relative = PurePosixPath(relative_text)
        observed_relative.add(relative_text)
        findings.extend(_path_findings(relative, repository=False))
        findings.extend(
            _content_findings(relative, data, current_identity=identity)
        )
        expected = manifest_files.get(relative_text)
        if expected is None:
            findings.append(
                AuditFinding(
                    "unmanifested_archive_entry",
                    raw_name,
                    "archive member is absent from the release manifest",
                )
            )
            continue
        if (
            expected.get("sha256") != _sha256_bytes(data)
            or expected.get("size") != len(data)
            or expected.get("mode") != member.mode
        ):
            findings.append(
                AuditFinding(
                    "manifest_mismatch",
                    raw_name,
                    "archive bytes, size, or mode do not match the manifest",
                )
            )
    for missing in sorted(set(manifest_files) - observed_relative):
        findings.append(
            AuditFinding(
                "missing_archive_entry",
                root + "/" + missing,
                "manifest entry is absent from the archive",
            )
        )

    return AuditReport(
        kind="archive",
        target=str(target),
        files_checked=len(entries),
        bytes_checked=total_bytes,
        findings=tuple(findings),
        sha256=archive_sha,
    )


def audit_many(
    *,
    repository: Optional[Path] = None,
    archives: Iterable[Path] = (),
) -> Dict[str, Any]:
    reports = []
    if repository is not None:
        reports.append(audit_repository(repository))
    reports.extend(audit_archive(path) for path in archives)
    if not reports:
        raise ValueError("at least one repository or archive target is required")
    return {
        "ok": all(report.ok for report in reports),
        "reports": [report.to_dict() for report in reports],
    }
