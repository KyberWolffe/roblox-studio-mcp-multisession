#!/usr/bin/env python3
"""Standalone, pinned-release bootstrap for Apple Silicon macOS.

This file is published as an individual release asset.  It deliberately uses
only the Python standard library so a new machine can download it, verify its
published SHA-256, and then use it to fetch and verify one exact tagged release.
It never resolves a mutable branch name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


PRODUCT = "RobloxStudioMCPv2"
PACKAGE_FORMAT = "roblox-studio-mcp-v2-portable-release"
TARGET_PLATFORM = "macos-arm64"
ARCHIVE_PREFIX = "roblox-studio-mcp-v2-"
ARCHIVE_SUFFIX = "-" + TARGET_PLATFORM + ".tar.gz"
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILES = 512
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SAFE_VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?$"
)
_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BootstrapError(RuntimeError):
    """A download, integrity, platform, or package check failed closed."""


def require_native_apple_silicon() -> None:
    """Reject unsupported hosts before creating a staging directory."""

    if tuple(sys.version_info[:3]) < (3, 9):
        raise BootstrapError(
            "Roblox Studio MCP v2 requires Python 3.9 or newer; no files "
            "were changed."
        )
    system = platform.system()
    machine = platform.machine()
    translated = False
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            translated = result.returncode == 0 and result.stdout.strip() == "1"
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            translated = False
    if system == "Darwin" and machine == "arm64" and not translated:
        return
    if translated:
        reason = "the process is running through Rosetta"
    elif system != "Darwin":
        reason = "the operating system is " + (system or "unknown")
    else:
        reason = "the machine architecture is " + (machine or "unknown")
    raise BootstrapError(
        "Roblox Studio MCP v2 supports native Apple Silicon macOS only; "
        + reason
        + ". No files were changed."
    )


def version_from_tag(tag: str) -> str:
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise BootstrapError("release tag must be an exact v-prefixed version")
    version = tag[1:]
    if _SAFE_VERSION.fullmatch(version) is None:
        raise BootstrapError("release tag is not a safe semantic version")
    return version


def archive_filename(version: str) -> str:
    if _SAFE_VERSION.fullmatch(version) is None:
        raise BootstrapError("release version is invalid")
    return ARCHIVE_PREFIX + version + ARCHIVE_SUFFIX


def _validate_repo_component(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_COMPONENT.fullmatch(value) is None:
        raise BootstrapError(label + " is invalid")
    return value


def release_asset_urls(owner: str, repo: str, tag: str) -> Tuple[str, str]:
    owner = _validate_repo_component(owner, "GitHub owner")
    repo = _validate_repo_component(repo, "GitHub repository")
    version = version_from_tag(tag)
    filename = archive_filename(version)
    encoded_tag = urllib.parse.quote(tag, safe="")
    base = (
        "https://github.com/"
        + owner
        + "/"
        + repo
        + "/releases/download/"
        + encoded_tag
        + "/"
    )
    return base + filename, base + filename + ".sha256"


def download_file(url: str, target: Path, maximum_bytes: int) -> None:
    """Bounded HTTPS download with an atomic final rename."""

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise BootstrapError("release asset URL must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "RobloxStudioMCPv2-bootstrap"},
        method="GET",
    )
    temporary = target.with_name("." + target.name + ".download")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or not final_url.netloc:
                raise BootstrapError(
                    "release asset redirect did not remain on HTTPS"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError as exc:
                    raise BootstrapError(
                        "release asset has an invalid Content-Length"
                    ) from exc
                if declared < 0 or declared > maximum_bytes:
                    raise BootstrapError("release asset exceeds the size limit")
            written = 0
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > maximum_bytes:
                        raise BootstrapError(
                            "release asset exceeds the size limit"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BootstrapError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise BootstrapError("release asset download failed: " + str(exc)) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum_file(path: Path, expected_filename: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise BootstrapError("checksum must be a regular non-symlink file")
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise BootstrapError("checksum file is not valid ASCII") from exc
    lines = text.splitlines()
    if len(lines) != 1:
        raise BootstrapError("checksum file must contain exactly one line")
    match = re.fullmatch(r"([0-9a-f]{64})  ([^\s/]+)", lines[0])
    if match is None or match.group(2) != expected_filename:
        raise BootstrapError("checksum file does not name the exact release asset")
    return match.group(1)


def verify_archive_checksum(
    archive: Path,
    checksum_file: Path,
    *,
    expected_sha256: Optional[str] = None,
) -> str:
    expected = parse_checksum_file(checksum_file, archive.name)
    if expected_sha256 is not None:
        supplied = expected_sha256.lower()
        if _SAFE_SHA256.fullmatch(supplied) is None:
            raise BootstrapError("--expected-sha256 is invalid")
        if not secrets.compare_digest(expected, supplied):
            raise BootstrapError(
                "published checksum does not match the explicitly pinned SHA-256"
            )
    actual = sha256_file(archive)
    if not secrets.compare_digest(actual, expected):
        raise BootstrapError("release archive SHA-256 verification failed")
    return actual


def _safe_member_name(name: str, expected_root: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise BootstrapError("release archive contains an invalid path")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BootstrapError("release archive path escapes the staging root")
    if not path.parts or path.parts[0] != expected_root:
        raise BootstrapError("release archive has an unexpected root directory")
    return path


def safe_extract_release(
    archive: Path,
    destination: Path,
    *,
    expected_version: str,
) -> Path:
    expected_root = archive_filename(expected_version)[: -len(".tar.gz")]
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    seen = set()
    file_count = 0
    total_size = 0
    try:
        with tarfile.open(archive, "r:gz") as package:
            members = package.getmembers()
            if len(members) > MAX_ARCHIVE_FILES:
                raise BootstrapError("release archive contains too many entries")
            for member in members:
                relative = _safe_member_name(member.name, expected_root)
                if member.name in seen:
                    raise BootstrapError("release archive contains duplicate paths")
                seen.add(member.name)
                if member.isdir():
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise BootstrapError(
                        "release archive contains a non-regular entry"
                    )
                file_count += 1
                total_size += member.size
                if (
                    member.size < 0
                    or total_size > MAX_ARCHIVE_BYTES
                    or file_count > MAX_ARCHIVE_FILES
                ):
                    raise BootstrapError("release archive exceeds safety limits")
                source = package.extractfile(member)
                if source is None:
                    raise BootstrapError("release archive member cannot be read")
                target = destination / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise BootstrapError(
                                "release archive member ended unexpectedly"
                            )
                        handle.write(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        raise BootstrapError(
                            "release archive member exceeds its declared size"
                        )
                os.chmod(target, 0o700 if member.mode & 0o111 else 0o600)
    except BootstrapError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise BootstrapError("release archive extraction failed: " + str(exc))
    return destination / expected_root


def _load_manifest(package_root: Path) -> Dict[str, Any]:
    path = package_root / "release-manifest.json"
    if not path.is_file() or path.is_symlink():
        raise BootstrapError("release manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("release manifest is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BootstrapError("release manifest must contain an object")
    return value


def verify_extracted_package(
    package_root: Path,
    *,
    expected_version: str,
) -> Dict[str, Any]:
    manifest = _load_manifest(package_root)
    if (
        manifest.get("format") != PACKAGE_FORMAT
        or manifest.get("manifest_version") != 1
        or manifest.get("product") != PRODUCT
        or manifest.get("version") != expected_version
        or manifest.get("platform") != TARGET_PLATFORM
    ):
        raise BootstrapError("release manifest identity/version/platform is invalid")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise BootstrapError("release manifest files must be a nonempty array")
    expected_paths = {"release-manifest.json"}
    for item in entries:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "sha256",
            "size",
            "mode",
        }:
            raise BootstrapError("release manifest contains an invalid file entry")
        relative = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or "." in Path(relative).parts
            or not isinstance(digest, str)
            or _SAFE_SHA256.fullmatch(digest) is None
            or not isinstance(size, int)
            or size < 0
        ):
            raise BootstrapError("release manifest file metadata is invalid")
        if relative in expected_paths:
            raise BootstrapError("release manifest contains a duplicate path")
        expected_paths.add(relative)
        target = package_root / relative
        if not target.is_file() or target.is_symlink():
            raise BootstrapError("release package file is missing: " + relative)
        if target.stat().st_size != size or not secrets.compare_digest(
            sha256_file(target), digest
        ):
            raise BootstrapError("release package hash mismatch: " + relative)
    actual_paths = {
        str(path.relative_to(package_root))
        for path in package_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_paths != expected_paths:
        raise BootstrapError(
            "release package contains files outside the verified manifest"
        )
    required = {
        "install.py",
        "platform_support.py",
        "release_updater.py",
        "bootstrap.py",
    }
    if not required.issubset(expected_paths):
        raise BootstrapError("release package is missing update/install components")
    return manifest


def acquire_release(
    staging_root: Path,
    *,
    tag: str,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    archive: Optional[Path] = None,
    checksum_file: Optional[Path] = None,
    expected_sha256: Optional[str] = None,
    downloader: Callable[[str, Path, int], None] = download_file,
) -> Tuple[Path, str, Dict[str, Any]]:
    """Acquire, hash-check, safely extract, and manifest-check one release."""

    version = version_from_tag(tag)
    filename = archive_filename(version)
    online = owner is not None or repo is not None
    offline = archive is not None or checksum_file is not None
    if online == offline:
        raise BootstrapError(
            "choose exactly one source: --owner/--repo or "
            "--archive/--checksum-file"
        )
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    staged_archive = staging_root / filename
    staged_checksum = staging_root / (filename + ".sha256")
    if online:
        if owner is None or repo is None:
            raise BootstrapError("both --owner and --repo are required")
        archive_url, checksum_url = release_asset_urls(owner, repo, tag)
        downloader(archive_url, staged_archive, MAX_DOWNLOAD_BYTES)
        downloader(checksum_url, staged_checksum, 4096)
    else:
        if archive is None or checksum_file is None:
            raise BootstrapError(
                "both --archive and --checksum-file are required"
            )
        source_archive = Path(archive).resolve(strict=True)
        source_checksum = Path(checksum_file).resolve(strict=True)
        if (
            not source_archive.is_file()
            or source_archive.is_symlink()
            or not source_checksum.is_file()
            or source_checksum.is_symlink()
        ):
            raise BootstrapError(
                "offline release inputs must be regular non-symlink files"
            )
        if source_archive.name != filename:
            raise BootstrapError(
                "offline archive filename does not match the exact release tag"
            )
        if (
            source_archive.stat().st_size > MAX_DOWNLOAD_BYTES
            or source_checksum.stat().st_size > 4096
        ):
            raise BootstrapError("offline release input exceeds the size limit")
        shutil.copyfile(source_archive, staged_archive)
        shutil.copyfile(source_checksum, staged_checksum)
    digest = verify_archive_checksum(
        staged_archive,
        staged_checksum,
        expected_sha256=expected_sha256,
    )
    package_root = safe_extract_release(
        staged_archive,
        staging_root / "extracted",
        expected_version=version,
    )
    manifest = verify_extracted_package(
        package_root,
        expected_version=version,
    )
    return package_root, digest, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fresh-install or repair the same exact tagged Roblox Studio MCP "
            "v2 release after verification. Upgrade an existing different "
            "version with the installed manager's update command."
        ),
        epilog=(
            "Cross-version in-place install is intentionally refused. Use "
            "'roblox-studio-mcp-v2-manage update' with an exact tag and "
            "published archive SHA-256."
        ),
    )
    parser.add_argument("--tag", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--owner")
    source.add_argument("--archive", type=Path)
    parser.add_argument("--repo")
    parser.add_argument("--checksum-file", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument(
        "--install-arg",
        action="append",
        default=[],
        help="bounded extra argument passed to install.py install",
    )
    return parser


def verified_installer_command(
    package_root: Path, arguments: Tuple[str, ...]
) -> list:
    """Run verified sibling imports under isolated mode.

    Python's ``-I`` intentionally omits the script directory from ``sys.path``.
    This fixed bootstrap inserts only the already hash-verified package root,
    sets the candidate's argv explicitly, and executes that exact install.py.
    """

    runner = (
        "import runpy,sys;"
        "root=sys.argv[1];"
        "script=root+'/install.py';"
        "sys.path.insert(0,root);"
        "sys.argv=[script,*sys.argv[2:]];"
        "runpy.run_path(script,run_name='__main__')"
    )
    return [
        sys.executable,
        "-I",
        "-B",
        "-c",
        runner,
        str(package_root),
        *arguments,
    ]


def main() -> None:
    args = build_parser().parse_args()
    try:
        require_native_apple_silicon()
        if any(
            value not in {"--replace-owned-config", "--rotate-secrets"}
            for value in args.install_arg
        ):
            raise BootstrapError("--install-arg contains an unsupported value")
        with tempfile.TemporaryDirectory(
            prefix="roblox-studio-mcp-v2-bootstrap-"
        ) as temporary:
            package_root, digest, manifest = acquire_release(
                Path(temporary) / "release",
                tag=args.tag,
                owner=args.owner,
                repo=args.repo,
                archive=args.archive,
                checksum_file=args.checksum_file,
                expected_sha256=args.expected_sha256,
            )
            command = verified_installer_command(
                package_root, ("install", *args.install_arg)
            )
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                raise BootstrapError(
                    "the verified release installer exited unsuccessfully; "
                    "if another v2 version is installed, upgrade through "
                    "'roblox-studio-mcp-v2-manage update'"
                )
        print(
            json.dumps(
                {
                    "ok": True,
                    "version": manifest["version"],
                    "archive_sha256": digest,
                    "restart_required": (
                        "Restart Codex and reload/restart open Studio windows."
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
    except BootstrapError as exc:
        sys.stderr.write("Studio MCP v2 bootstrap refused: " + str(exc) + "\n")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
