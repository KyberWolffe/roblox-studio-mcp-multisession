from __future__ import annotations

import json
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

from release_tools import builder, installer
from studio_mcp_v2 import __version__


ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.4.0-rc.4"
PEP440_VERSION = "0.4.0rc4"
PRERELEASE_MARKERS = (
    "<!-- experimental-prerelease: true -->",
    "<!-- capability-parity: incomplete -->",
    "<!-- global-v1-fallback: forbidden -->",
)


class ReleaseVersionCoherenceTests(unittest.TestCase):
    def test_source_and_document_version_surfaces_are_exact(self) -> None:
        self.assertEqual(VERSION, installer.VERSION)
        self.assertEqual(VERSION, __version__)

        expected_json_versions = (
            ("config/durable-tool-catalog.json", "catalog_version"),
            (
                "config/upstream-compatibility-map.json",
                "durable_catalog_version",
            ),
            ("config/v1-capability-parity.json", "release_version"),
        )
        for relative, key in expected_json_versions:
            with self.subTest(relative=relative):
                value = json.loads(
                    (ROOT / relative).read_text(encoding="utf-8")
                )
                self.assertEqual(VERSION, value[key])

        pyproject = (ROOT / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        versions = re.findall(
            r'^version = "([^"]+)"$', pyproject, re.MULTILINE
        )
        self.assertEqual([PEP440_VERSION], versions)

        release_note = (
            ROOT / "docs" / ("RELEASE_NOTES_" + VERSION + ".md")
        )
        note_text = release_note.read_text(encoding="utf-8")
        self.assertIn("# Roblox Studio MCP v2 " + VERSION, note_text)
        for marker in PRERELEASE_MARKERS:
            self.assertEqual(1, note_text.count(marker))
        self.assertIn(
            "Version `" + VERSION + "`",
            (ROOT / "README.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "## " + VERSION + " — unreleased",
            (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
        )

    def test_built_archive_preserves_the_same_version_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            built = builder.build_release(ROOT, Path(temporary))
            self.assertEqual(VERSION, built.manifest["version"])
            self.assertEqual(33, len(built.manifest["files"]))
            self.assertIn(VERSION, built.archive.name)
            self.assertIn(VERSION, built.bootstrap.name)

            with tarfile.open(built.archive, "r:gz") as package:
                root = builder.ARCHIVE_BASENAME
                manifest = json.loads(
                    package.extractfile(
                        root + "/release-manifest.json"
                    ).read()
                )
                archived_init = package.extractfile(
                    root + "/payload/studio_mcp_v2/__init__.py"
                ).read().decode("utf-8")
                archived_installer = package.extractfile(
                    root + "/install.py"
                ).read().decode("utf-8")

            self.assertEqual(VERSION, manifest["version"])
            self.assertEqual(33, len(manifest["files"]))
            self.assertIn('__version__ = "' + VERSION + '"', archived_init)
            self.assertIn('VERSION = "' + VERSION + '"', archived_installer)


if __name__ == "__main__":
    unittest.main()
