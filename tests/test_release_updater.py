from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import platform_support

from release_tools import bootstrap
from release_tools import builder
from release_tools import installer as durable
from release_tools import updater


ROOT = Path(__file__).resolve().parent.parent


class BootstrapVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.built = builder.build_release(ROOT, self.root / "dist")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_tag_urls_and_offline_acquisition(self) -> None:
        archive_url, checksum_url = bootstrap.release_asset_urls(
            "example-owner", "example-repo", "v" + durable.VERSION
        )
        self.assertEqual(
            (
                "https://github.com/example-owner/example-repo/releases/"
                "download/v"
                + durable.VERSION
                + "/"
                + builder.ARCHIVE_FILENAME
            ),
            archive_url,
        )
        self.assertEqual(archive_url + ".sha256", checksum_url)
        package, digest, manifest = bootstrap.acquire_release(
            self.root / "offline-stage",
            tag="v" + durable.VERSION,
            archive=self.built.archive,
            checksum_file=self.built.checksum_file,
            expected_sha256=self.built.sha256,
        )
        self.assertEqual(self.built.sha256, digest)
        self.assertEqual(durable.VERSION, manifest["version"])
        self.assertTrue((package / "install.py").is_file())

    def test_online_acquisition_is_injectable_and_fetches_only_two_assets(
        self,
    ) -> None:
        calls = []

        def downloader(url: str, target: Path, maximum: int) -> None:
            calls.append((url, target.name, maximum))
            if url.endswith(".sha256"):
                target.write_bytes(self.built.checksum_file.read_bytes())
            else:
                target.write_bytes(self.built.archive.read_bytes())

        _package, digest, _manifest = bootstrap.acquire_release(
            self.root / "online-stage",
            tag="v" + durable.VERSION,
            owner="trusted-owner",
            repo="trusted-repo",
            expected_sha256=self.built.sha256,
            downloader=downloader,
        )
        self.assertEqual(self.built.sha256, digest)
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0][0].startswith("https://github.com/"))
        self.assertEqual(builder.ARCHIVE_FILENAME, calls[0][1])
        self.assertEqual(builder.ARCHIVE_FILENAME + ".sha256", calls[1][1])

    def test_checksum_and_manifest_tampering_fail_closed(self) -> None:
        bad_checksum = self.root / (builder.ARCHIVE_FILENAME + ".sha256")
        bad_checksum.write_text(
            "0" * 64 + "  " + builder.ARCHIVE_FILENAME + "\n",
            encoding="ascii",
        )
        with self.assertRaisesRegex(
            bootstrap.BootstrapError, "SHA-256 verification"
        ):
            bootstrap.verify_archive_checksum(
                self.built.archive, bad_checksum
            )

        package, _digest, _manifest = bootstrap.acquire_release(
            self.root / "tamper-stage",
            tag="v" + durable.VERSION,
            archive=self.built.archive,
            checksum_file=self.built.checksum_file,
        )
        (package / "install.py").write_text("# tampered\n")
        with self.assertRaisesRegex(
            bootstrap.BootstrapError, "hash mismatch"
        ):
            bootstrap.verify_extracted_package(
                package, expected_version=durable.VERSION
            )

    def test_archive_links_are_rejected_without_escaping_stage(self) -> None:
        malicious = self.root / builder.ARCHIVE_FILENAME
        archive_root = builder.ARCHIVE_BASENAME
        with tarfile.open(malicious, "w:gz") as package:
            link = tarfile.TarInfo(archive_root + "/install.py")
            link.type = tarfile.SYMTYPE
            link.linkname = "/tmp/escape"
            package.addfile(link)
        destination = self.root / "malicious-extracted"
        with self.assertRaisesRegex(
            bootstrap.BootstrapError, "non-regular"
        ):
            bootstrap.safe_extract_release(
                malicious,
                destination,
                expected_version=durable.VERSION,
            )
        self.assertFalse(destination.exists())

    def test_download_rejects_redirect_that_leaves_https(self) -> None:
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "http://example.invalid/release"

        target = self.root / "download"
        with mock.patch(
            "release_tools.bootstrap.urllib.request.urlopen",
            return_value=Response(),
        ):
            with self.assertRaisesRegex(
                bootstrap.BootstrapError, "remain on HTTPS"
            ):
                bootstrap.download_file(
                    "https://github.com/owner/repo/release",
                    target,
                    100,
                )
        self.assertFalse(target.exists())

    def test_isolated_bootstrap_command_imports_only_verified_siblings(
        self,
    ) -> None:
        package, _digest, _manifest = bootstrap.acquire_release(
            self.root / "isolated-import-stage",
            tag="v" + durable.VERSION,
            archive=self.built.archive,
            checksum_file=self.built.checksum_file,
        )
        command = bootstrap.verified_installer_command(package, ("--help",))
        self.assertEqual("-I", command[1])
        process = subprocess.run(
            command,
            cwd=self.root,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONPATH": str(self.root / "hostile"),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertIn("Install and manage", process.stdout)
        self.assertNotIn("ModuleNotFoundError", process.stderr)


class SnapshotDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bin_restore_keeps_manager_present_at_every_crash_point(
        self,
    ) -> None:
        probe = self.root / "resume-bin-restore.py"
        probe.write_text(
            """
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from release_tools import updater

source = Path(sys.argv[2])
target = Path(sys.argv[3])
entries = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
updater._restore_regular_tree_in_place(source, target, entries)
""".lstrip(),
            encoding="utf-8",
        )
        names = (
            durable.STABLE_LAUNCHER_NAME + "-bootstrap.py",
            durable.STABLE_LAUNCHER_NAME,
            durable.STABLE_MANAGER_NAME,
        )

        class InjectedCrash(RuntimeError):
            pass

        for crash_at in range(1, len(names) + 1):
            for after_replace in (False, True):
                with self.subTest(
                    crash_at=crash_at,
                    after_replace=after_replace,
                ):
                    case = self.root / (
                        "case-"
                        + str(crash_at)
                        + "-"
                        + ("after" if after_replace else "before")
                    )
                    source = case / "snapshot-bin"
                    target = case / "installed-bin"
                    source.mkdir(parents=True)
                    target.mkdir()
                    entries = {}
                    for name in names:
                        source_value = ("new-" + name).encode("utf-8")
                        target_value = ("old-" + name).encode("utf-8")
                        (source / name).write_bytes(source_value)
                        (target / name).write_bytes(target_value)
                        os.chmod(source / name, 0o700)
                        os.chmod(target / name, 0o700)
                        entries[name] = {
                            "sha256": hashlib.sha256(
                                source_value
                            ).hexdigest(),
                            "mode": 0o700,
                            "size": len(source_value),
                        }
                    entries_path = case / "entries.json"
                    entries_path.write_text(
                        json.dumps(entries), encoding="utf-8"
                    )
                    original_replace = os.replace
                    calls = {"count": 0}

                    def crash_replace(source_path, target_path):
                        calls["count"] += 1
                        if calls["count"] == crash_at:
                            if after_replace:
                                original_replace(
                                    source_path, target_path
                                )
                            raise InjectedCrash("simulated process death")
                        return original_replace(source_path, target_path)

                    with mock.patch(
                        "release_tools.updater.os.replace",
                        side_effect=crash_replace,
                    ):
                        with self.assertRaises(InjectedCrash):
                            updater._restore_regular_tree_in_place(
                                source, target, entries
                            )
                    manager = target / durable.STABLE_MANAGER_NAME
                    self.assertTrue(manager.is_file())
                    self.assertFalse(manager.is_symlink())
                    self.assertIn(
                        manager.read_bytes(),
                        {
                            (
                                "old-" + durable.STABLE_MANAGER_NAME
                            ).encode("utf-8"),
                            (
                                "new-" + durable.STABLE_MANAGER_NAME
                            ).encode("utf-8"),
                        },
                    )

                    resumed = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            str(probe),
                            str(ROOT),
                            str(source),
                            str(target),
                            str(entries_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        text=True,
                    )
                    self.assertEqual(
                        0, resumed.returncode, resumed.stderr
                    )
                    for name in names:
                        self.assertEqual(
                            ("new-" + name).encode("utf-8"),
                            (target / name).read_bytes(),
                        )


class _FakeCurrentInstaller:
    def __init__(self, layout: durable.InstallLayout):
        self.layout = layout
        self.python_executable = sys.executable

    def _load_state(self, *, optional: bool = True):
        del optional
        return json.loads(self.layout.install_state.read_text())

    def _safe_stop_lifecycle(self):
        return {"ok": True, "running": False, "stopped": True}


class _FakeCandidate:
    def __init__(
        self,
        layout: durable.InstallLayout,
        version: str,
        *,
        fail: bool = False,
    ):
        self.layout = layout
        self.version = version
        self.fail = fail

    def install(self):
        state = json.loads(self.layout.install_state.read_text())
        state["version"] = self.version
        self.layout.install_state.write_text(
            json.dumps(state, sort_keys=True) + "\n"
        )
        self.layout.codex_config.write_bytes(
            self.layout.codex_config.read_bytes() + b"# candidate\n"
        )
        self.layout.plugin_target.write_bytes(
            ("plugin-" + self.version).encode("ascii")
        )
        retained = self.layout.packages / self.version
        retained.mkdir(mode=0o700, parents=True, exist_ok=True)
        (retained / "release-manifest.json").write_text(
            json.dumps(
                {
                    "format": bootstrap.PACKAGE_FORMAT,
                    "product": bootstrap.PRODUCT,
                    "version": self.version,
                }
            )
        )
        if self.fail:
            raise RuntimeError("simulated candidate failure")
        return {"ok": True, "version": self.version}

    def doctor(self):
        return {"ok": True, "version": self.version}


class ReleaseTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.layout = durable.InstallLayout.for_user(home=self.home)
        for directory in (
            self.layout.support_root,
            self.layout.config,
            self.layout.artifacts,
            self.layout.state,
            self.layout.bin,
            self.layout.backups,
            self.layout.run,
            self.layout.packages,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.layout.codex_config.parent.mkdir(parents=True)
        self.layout.codex_config.write_bytes(
            b"[mcp_servers.Roblox_Studio]\ncommand = \"v1\"\n"
            b"[mcp_servers.Roblox_Studio_v2]\ncommand = \"v2\"\n"
        )
        self.layout.plugin_target.parent.mkdir(parents=True)
        self.layout.plugin_target.write_bytes(b"plugin-0.2.0")
        self.layout.install_state.write_text(
            json.dumps({"version": "0.2.0"}) + "\n"
        )
        (self.layout.config / "runtime.json").write_bytes(b"runtime-old\n")
        (self.layout.artifacts / "catalog.json").write_bytes(b"catalog-old\n")
        (self.layout.bin / "launcher").write_bytes(b"launcher-old\n")
        for path in (
            self.layout.install_state,
            self.layout.config / "runtime.json",
            self.layout.artifacts / "catalog.json",
            self.layout.bin / "launcher",
            self.layout.codex_config,
            self.layout.plugin_target,
        ):
            os.chmod(path, 0o600)
        previous = self.layout.packages / "0.2.0"
        previous.mkdir(mode=0o700)
        (previous / "release-manifest.json").write_text(
            json.dumps(
                {
                    "format": bootstrap.PACKAGE_FORMAT,
                    "product": bootstrap.PRODUCT,
                    "version": "0.2.0",
                }
            )
        )
        self.built = builder.build_release(ROOT, self.root / "dist")
        self.current = _FakeCurrentInstaller(self.layout)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(self, *, fail_versions=()):
        failed = set(fail_versions)

        def factory(current, package_root, version):
            del current, package_root
            return _FakeCandidate(
                self.layout, version, fail=version in failed
            )

        return updater.ReleaseUpdater(
            self.current,
            candidate_factory=factory,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )

    def _update(self, manager):
        return manager.update(
            tag="v" + durable.VERSION,
            archive=self.built.archive,
            checksum_file=self.built.checksum_file,
            expected_sha256=self.built.sha256,
        )

    def test_offline_update_and_one_step_rollback_are_receipted(self) -> None:
        manager = self._manager()
        shared_paths = (
            self.layout.install_state,
            self.layout.config / "runtime.json",
            self.layout.artifacts / "catalog.json",
            self.layout.bin / "launcher",
            self.layout.codex_config,
            self.layout.plugin_target,
        )
        before = {path: path.read_bytes() for path in shared_paths}
        updated = self._update(manager)
        self.assertTrue(updated["ok"])
        self.assertEqual("0.2.0", updated["previous_version"])
        self.assertEqual(durable.VERSION, updated["version"])
        self.assertEqual(
            durable.VERSION,
            json.loads(self.layout.install_state.read_text())["version"],
        )
        receipt = json.loads(
            (self.layout.state / updater.LATEST_RECEIPT_FILENAME).read_text()
        )
        self.assertEqual("0.2.0", receipt["previous_version"])
        self.assertEqual(durable.VERSION, receipt["current_version"])

        rolled_back = manager.rollback(
            to_version="0.2.0",
            accept_current_version=durable.VERSION,
        )
        self.assertTrue(rolled_back["ok"])
        self.assertEqual("rollback", rolled_back["action"])
        self.assertEqual(
            "0.2.0",
            json.loads(self.layout.install_state.read_text())["version"],
        )
        for path, expected in before.items():
            self.assertEqual(expected, path.read_bytes(), str(path))
        reverse_receipt = json.loads(
            (self.layout.state / updater.LATEST_RECEIPT_FILENAME).read_text()
        )
        self.assertEqual(durable.VERSION, reverse_receipt["previous_version"])
        self.assertEqual("0.2.0", reverse_receipt["current_version"])

    def test_failed_switch_restores_exact_shared_and_external_bytes(self) -> None:
        paths = (
            self.layout.install_state,
            self.layout.config / "runtime.json",
            self.layout.artifacts / "catalog.json",
            self.layout.bin / "launcher",
            self.layout.codex_config,
            self.layout.plugin_target,
        )
        before = {path: path.read_bytes() for path in paths}
        manager = self._manager(fail_versions={durable.VERSION})
        with self.assertRaisesRegex(
            updater.UpdateError, "prior v2-owned bytes were restored"
        ):
            self._update(manager)
        for path, expected in before.items():
            self.assertEqual(expected, path.read_bytes(), str(path))
        self.assertFalse(
            (self.layout.state / updater.LATEST_RECEIPT_FILENAME).exists()
        )

    def test_rollback_requires_exact_current_version_and_latest_receipt(self) -> None:
        manager = self._manager()
        self._update(manager)
        with self.assertRaisesRegex(updater.UpdateError, "does not match"):
            manager.rollback(
                to_version="0.2.0",
                accept_current_version="9.9.9",
            )
        with self.assertRaisesRegex(updater.UpdateError, "does not authorize"):
            manager.rollback(
                to_version="0.1.0",
                accept_current_version=durable.VERSION,
            )

    def test_unsupported_platform_refuses_before_lock_or_staging_mutation(
        self,
    ) -> None:
        run_before = sorted(self.layout.run.iterdir())

        def unsupported():
            raise platform_support.UnsupportedPlatformError(
                "unsupported; No files were changed."
            )

        manager = updater.ReleaseUpdater(
            self.current,
            candidate_factory=lambda *_args: None,
            platform_check=unsupported,
            runtime_check=lambda: None,
        )
        with self.assertRaisesRegex(updater.UpdateError, "No files were changed"):
            self._update(manager)
        self.assertEqual(run_before, sorted(self.layout.run.iterdir()))
        self.assertEqual(
            "0.2.0",
            json.loads(self.layout.install_state.read_text())["version"],
        )

    def test_stale_pending_record_never_weakens_status_or_rollback_fence(
        self,
    ) -> None:
        pending = {
            "format": updater.RECEIPT_FORMAT + "-pending",
            "schema_version": updater.RECEIPT_VERSION,
            "action": "update",
            "phase": "candidate_doctor",
            "pid": os.getpid(),
            "nonce": "f" * 32,
            "created_at": "2026-01-01T00:00:00+00:00",
            "previous_version": "0.1.0",
            "current_version": "0.2.0",
            "snapshot": str(self.layout.backups / "missing"),
            "snapshot_manifest_sha256": "e" * 64,
        }
        pending_root = self.layout.backups / "release-updates"
        pending_root.mkdir(mode=0o700)
        (pending_root / updater.PENDING_TRANSACTION_FILENAME).write_text(
            json.dumps(pending) + "\n"
        )
        status = self._manager().status()
        self.assertFalse(status["ok"])
        self.assertFalse(status["rollback"]["available"])


if __name__ == "__main__":
    unittest.main()
