"""Fail-closed host checks for the macOS Apple Silicon distribution."""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Optional


SUPPORTED_SYSTEM = "Darwin"
SUPPORTED_MACHINE = "arm64"
TARGET_PLATFORM = "macos-arm64"
MINIMUM_PYTHON = (3, 9)


class UnsupportedPlatformError(RuntimeError):
    """Raised before a mutating command runs on an unsupported host."""


@dataclass(frozen=True)
class PlatformStatus:
    system: str
    machine: str
    rosetta_translated: bool
    supported: bool
    target: str = TARGET_PLATFORM

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


def _detect_rosetta(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Return true only when macOS explicitly reports Rosetta translation."""

    try:
        result = runner(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def detect_platform(
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    rosetta_translated: Optional[bool] = None,
) -> PlatformStatus:
    detected_system = platform.system() if system is None else system
    detected_machine = platform.machine() if machine is None else machine
    translated = (
        _detect_rosetta()
        if rosetta_translated is None and detected_system == SUPPORTED_SYSTEM
        else bool(rosetta_translated)
    )
    supported = (
        detected_system == SUPPORTED_SYSTEM
        and detected_machine == SUPPORTED_MACHINE
        and not translated
    )
    return PlatformStatus(
        system=detected_system,
        machine=detected_machine,
        rosetta_translated=translated,
        supported=supported,
    )


def require_supported_platform(
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    rosetta_translated: Optional[bool] = None,
) -> PlatformStatus:
    status = detect_platform(
        system=system,
        machine=machine,
        rosetta_translated=rosetta_translated,
    )
    if status.supported:
        return status
    if status.rosetta_translated:
        reason = "the process is running through Rosetta"
    elif status.system != SUPPORTED_SYSTEM:
        reason = "the operating system is " + (status.system or "unknown")
    else:
        reason = "the machine architecture is " + (status.machine or "unknown")
    raise UnsupportedPlatformError(
        "Roblox Studio MCP v2 supports native Apple Silicon macOS only; "
        + reason
        + ". No files were changed."
    )


def require_supported_runtime(
    version_info: Optional[tuple] = None,
) -> tuple:
    detected = tuple(sys.version_info[:3] if version_info is None else version_info)
    if detected >= MINIMUM_PYTHON:
        return detected
    raise UnsupportedPlatformError(
        "Roblox Studio MCP v2 requires Python 3.9 or newer; detected "
        + ".".join(str(item) for item in detected)
        + ". No files were changed."
    )
