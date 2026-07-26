"""Pinned bootstrap used by the installed stable launcher.

The installer substitutes the four bounded placeholders below and writes this
file beneath the stable support root.  No credential is embedded here.
"""

from __future__ import annotations

import os
from pathlib import Path


SUPPORT_ROOT = Path(__SUPPORT_ROOT_LITERAL__)
RELEASE_ROOT = Path(__RELEASE_ROOT_LITERAL__)
PYTHON_EXECUTABLE = __PYTHON_EXECUTABLE_LITERAL__
ENTRYPOINT_MODULE = __ENTRYPOINT_MODULE_LITERAL__

_BOOTSTRAP = (
    "import sys;"
    "sys.path.insert(0,"
    + repr(str(RELEASE_ROOT))
    + ");"
    "from studio_mcp_v2.lifecycle import "
    "main_for_installed_support_root as main;"
    "main("
    + repr(str(SUPPORT_ROOT))
    + ")"
)


def main() -> None:
    support = SUPPORT_ROOT.resolve(strict=True)
    release = RELEASE_ROOT.resolve(strict=True)
    expected_release_parent = support / "releases"
    if release.parent != expected_release_parent or not release.is_dir():
        raise SystemExit("Studio MCP v2 installed release path is invalid")
    if not (release / "studio_mcp_v2" / "__init__.py").is_file():
        raise SystemExit("Studio MCP v2 installed release is incomplete")

    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    # Isolated mode removes cwd, user-site, and all PYTHON* influence.  The
    # fixed bootstrap then inserts only the verified absolute release root
    # ahead of the standard library before importing the lifecycle.
    argv = [
        PYTHON_EXECUTABLE,
        "-I",
        "-B",
        "-c",
        _BOOTSTRAP,
        *os.sys.argv[1:],
    ]
    os.execve(PYTHON_EXECUTABLE, argv, environment)


if __name__ == "__main__":
    main()
