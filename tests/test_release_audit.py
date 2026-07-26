from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release_tools import audit


class RepositoryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, value: str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
        return target

    def test_clean_allowlisted_tree_passes(self) -> None:
        self._write("README.md", "# Safe\n")
        self._write("studio_mcp_v2/example.py", "VALUE = '__TOKEN_PLACEHOLDER__'\n")
        self._write(
            "tests/fixtures/synthetic/example.json",
            json.dumps(
                {
                    "release_audit_fixture": "synthetic",
                    "studio_id": "synthetic-studio-one",
                }
            ),
        )
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_repository(self.root)
        self.assertTrue(report.ok, report.findings)

    def test_canonical_session_id_is_allowed_only_in_synthetic_fixture(self) -> None:
        studio_id = "00000000-0000-4000-8000-000000000001"
        self._write(
            "tests/fixtures/synthetic/session.json",
            json.dumps(
                {
                    "release_audit_fixture": "synthetic",
                    "studio_id": studio_id,
                }
            ),
        )
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            synthetic_report = audit.audit_repository(self.root)
        self.assertTrue(synthetic_report.ok, synthetic_report.findings)

        self._write("config/session.json", json.dumps({"studio_id": studio_id}))
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            concrete_report = audit.audit_repository(self.root)
        self.assertIn(
            "sensitive_json_value",
            {item.code for item in concrete_report.findings},
        )

    def test_rejects_user_path_secret_state_and_unexpected_artifact(self) -> None:
        user_path = "/" + "Users" + "/" + "sample-account" + "/private.json"
        self._write("docs/path.md", user_path + "\n")
        self._write(
            "config/example.json",
            json.dumps({"studio_token": "real-looking-value-123456789"}),
        )
        self._write("logs/session.log", "not publishable")
        self._write("surprise.bin", "not allowlisted")
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_repository(self.root)
        codes = {item.code for item in report.findings}
        self.assertIn("absolute_macos_user_path", codes)
        self.assertIn("sensitive_json_value", codes)
        self.assertIn("runtime_or_build_material", codes)
        self.assertIn("unexpected_repository_file", codes)
        self.assertIn("unexpected_file_type", codes)

    def test_does_not_echo_the_matched_credential(self) -> None:
        credential = "github" + "_pat_" + ("A" * 32)
        self._write("README.md", credential)
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_repository(self.root)
        rendered = json.dumps(report.to_dict())
        self.assertFalse(report.ok)
        self.assertNotIn(credential, rendered)

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_symlinks_without_following_them(self) -> None:
        self._write("README.md", "# Safe\n")
        os.symlink(self.root / "README.md", self.root / "docs-link")
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_repository(self.root)
        self.assertIn("symlink", {item.code for item in report.findings})


class ArchiveAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _archive(
        self,
        files: dict[str, tuple[bytes, int]],
        *,
        manifest_platform: str = "macos-arm64",
        malicious_name: str | None = None,
    ) -> Path:
        package_root = "roblox-studio-mcp-v2-9.9.9-macos-arm64"
        manifest = {
            "format": "roblox-studio-mcp-v2-portable-release",
            "manifest_version": 1,
            "product": "RobloxStudioMCPv2",
            "version": "9.9.9",
            "platform": manifest_platform,
            "python_requires": ">=3.9",
            "source_date_epoch": 0,
            "files": [
                {
                    "path": name,
                    "sha256": audit._sha256_bytes(value),
                    "size": len(value),
                    "mode": mode,
                }
                for name, (value, mode) in sorted(files.items())
            ],
        }
        complete = {
            **files,
            "release-manifest.json": (
                (json.dumps(manifest, sort_keys=True) + "\n").encode(),
                0o644,
            ),
        }
        target = self.root / (package_root + ".tar.gz")
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as package:
            for name, (value, mode) in sorted(complete.items()):
                member_name = (
                    malicious_name
                    if malicious_name is not None and name == "install.py"
                    else package_root + "/" + name
                )
                info = tarfile.TarInfo(member_name)
                info.size = len(value)
                info.mode = mode
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                package.addfile(info, io.BytesIO(value))
        with target.open("wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0
            ) as compressed:
                compressed.write(tar_buffer.getvalue())
        return target

    def test_valid_manifested_arm64_archive_passes(self) -> None:
        archive = self._archive(
            {
                "install.py": (b"#!/usr/bin/env python3\n", 0o755),
                "payload/config/tool-catalog.json": (b'{"tools":[]}\n', 0o644),
            }
        )
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_archive(archive)
        self.assertTrue(report.ok, report.findings)

    def test_rejects_wrong_platform_and_unsafe_entry(self) -> None:
        archive = self._archive(
            {"install.py": (b"safe\n", 0o755)},
            manifest_platform="macos",
            malicious_name="../install.py",
        )
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_archive(archive)
        codes = {item.code for item in report.findings}
        self.assertIn("unsafe_archive_path", codes)
        self.assertIn("unsupported_manifest_platform", codes)

    def test_rejects_unmanifested_archive_member(self) -> None:
        archive = self._archive({"install.py": (b"safe\n", 0o755)})
        rewritten = self.root / archive.name
        tar_buffer = io.BytesIO()
        with tarfile.open(archive, "r:gz") as original, tarfile.open(
            fileobj=tar_buffer, mode="w"
        ) as output:
            for member in original.getmembers():
                stream = original.extractfile(member)
                assert stream is not None
                output.addfile(member, io.BytesIO(stream.read()))
            data = b"unexpected\n"
            extra = tarfile.TarInfo(
                "roblox-studio-mcp-v2-9.9.9-macos-arm64/extra.txt"
            )
            extra.size = len(data)
            extra.mode = 0o644
            extra.mtime = 0
            output.addfile(extra, io.BytesIO(data))
        with rewritten.open("wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0
            ) as compressed:
                compressed.write(tar_buffer.getvalue())
        with mock.patch(
            "release_tools.audit._safe_current_identity", return_value=()
        ):
            report = audit.audit_archive(rewritten)
        self.assertIn(
            "unmanifested_archive_entry",
            {item.code for item in report.findings},
        )


if __name__ == "__main__":
    unittest.main()
