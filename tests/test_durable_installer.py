from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release_tools import builder
from release_tools import installer as durable
from release_tools import updater


ROOT = Path(__file__).resolve().parent.parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DurableInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        built = builder.build_release(ROOT, self.root / "dist")
        self.archive = built.archive
        self.archive_sha256 = built.sha256
        with tarfile.open(self.archive, "r:gz") as package:
            for member in package.getmembers():
                self.assertFalse(Path(member.name).is_absolute())
                self.assertNotIn("..", Path(member.name).parts)
            package.extractall(self.root / "extracted")
        self.package = (
            self.root
            / "extracted"
            / builder.ARCHIVE_BASENAME
        )
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.codex_dir = self.home / ".codex"
        self.codex_dir.mkdir(mode=0o755)
        self.plugins = self.home / "Documents" / "Roblox" / "Plugins"
        self.plugins.mkdir(mode=0o755, parents=True)
        self.v1_config = (
            b"# user prefix\n"
            b"[mcp_servers.Roblox_Studio]\n"
            b'command = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"\n'
            b"args = []\n"
        )
        (self.codex_dir / "config.toml").write_bytes(self.v1_config)
        self.v1_plugin = self.plugins / "MCPStudioPlugin.rbxm"
        self.v1_plugin.write_bytes(b"v1 sentinel plugin")
        self.v1_plugin_hash = _sha256(self.v1_plugin)
        self.layout = durable.InstallLayout.for_user(home=self.home)
        self.manager = durable.Installer(
            self.package,
            self.layout,
            python_executable=sys.executable,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install(self) -> dict:
        return self.manager.install()

    @staticmethod
    def _stopped() -> dict:
        return {"ok": True, "running": False, "stopped": False}

    @staticmethod
    def _verified_runtime() -> dict:
        return {
            "start": {
                "ok": True,
                "broker": {"catalog_sha256": "a" * 64},
            },
            "doctor_catalog_sha256": "a" * 64,
        }

    def _simulate_crashed_release_switch(
        self,
        *,
        candidate_version: str = "9.9.9",
        corrupt_install_state: bool = False,
    ):
        self._install()
        release_updater = updater.ReleaseUpdater(
            self.manager,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        snapshot = updater._OwnedSnapshot.capture(self.layout)
        pending = release_updater._begin_pending_validation(
            action="update",
            previous_version=durable.VERSION,
            current_version=candidate_version,
            snapshot=snapshot,
        )
        updater._ACTIVE_VALIDATION_NONCES.discard(pending["nonce"])
        originals = {
            self.layout.codex_config: self.layout.codex_config.read_bytes(),
            self.layout.plugin_target: self.layout.plugin_target.read_bytes(),
            self.layout.launcher: self.layout.launcher.read_bytes(),
            self.layout.install_state: self.layout.install_state.read_bytes(),
        }
        if corrupt_install_state:
            self.layout.install_state.write_bytes(b"{crash")
        else:
            state = json.loads(self.layout.install_state.read_text())
            state["version"] = candidate_version
            durable._atomic_write(
                self.layout.install_state,
                durable._json_bytes(state),
                0o600,
            )
        self.layout.codex_config.write_bytes(
            self.layout.codex_config.read_bytes() + b"# half candidate\n"
        )
        self.layout.plugin_target.write_bytes(b"half candidate plugin")
        self.layout.launcher.write_bytes(b"half candidate launcher")
        return release_updater, snapshot, pending, originals

    def _simulate_crashed_rollback(self):
        self._install()
        target_version = "0.2.0-test"
        release_updater = updater.ReleaseUpdater(
            self.manager,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        current_state = self.layout.install_state.read_bytes()
        target_state = json.loads(current_state)
        target_state["version"] = target_version
        durable._atomic_write(
            self.layout.install_state,
            durable._json_bytes(target_state),
            0o600,
        )
        target_snapshot = updater._OwnedSnapshot.capture(self.layout)
        durable._atomic_write(
            self.layout.install_state, current_state, 0o600
        )
        retained_target = self.layout.packages / target_version
        retained_target.mkdir(mode=0o700)
        (retained_target / "release-manifest.json").write_text(
            json.dumps(
                {
                    "format": durable.PACKAGE_FORMAT,
                    "product": durable.PRODUCT,
                    "version": target_version,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        release_updater._write_receipt(
            action="update",
            previous_version=target_version,
            current_version=durable.VERSION,
            archive_sha256="a" * 64,
            snapshot=target_snapshot,
        )
        originals = {
            self.layout.codex_config: self.layout.codex_config.read_bytes(),
            self.layout.plugin_target: self.layout.plugin_target.read_bytes(),
            self.layout.launcher: self.layout.launcher.read_bytes(),
            self.layout.install_state: self.layout.install_state.read_bytes(),
            release_updater._receipt_path(): (
                release_updater._receipt_path().read_bytes()
            ),
        }

        class CrashAfterTargetRestore:
            def doctor(candidate_self):
                del candidate_self
                raise SystemExit("simulated rollback process death")

        release_updater.candidate_factory = (
            lambda _current, _package, _version: CrashAfterTargetRestore()
        )
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            return_value={"ok": True, "running": False, "stopped": False},
        ):
            with self.assertRaisesRegex(
                SystemExit, "simulated rollback process death"
            ):
                release_updater.rollback(
                    to_version=target_version,
                    accept_current_version=durable.VERSION,
                )
        return release_updater, target_version, originals

    @staticmethod
    def _recovery_process_result(argv, **_kwargs):
        if "stop" in argv:
            payload = {"ok": True, "running": False, "stopped": False}
        elif "doctor" in argv:
            payload = {
                "ok": True,
                "lifecycle": {"condition": "stopped"},
                "catalog": {"installed_v1_cache": None},
            }
        else:
            raise AssertionError("unexpected recovery subprocess: " + repr(argv))
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=(json.dumps(payload) + "\n").encode("utf-8"),
            stderr=b"",
        )

    def _fresh_process_repair(self) -> dict:
        probe = self.root / "fresh-process-repair.py"
        probe.write_text(
            """
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from release_tools import installer, updater

def fake_run(argv, **_kwargs):
    if "stop" in argv:
        payload = {"ok": True, "running": False, "stopped": False}
    elif "doctor" in argv:
        payload = {
            "ok": True,
            "lifecycle": {"condition": "stopped"},
            "catalog": {"installed_v1_cache": None},
        }
    else:
        raise RuntimeError("unexpected recovery subprocess: " + repr(argv))
    return subprocess.CompletedProcess(
        args=argv,
        returncode=0,
        stdout=(json.dumps(payload) + "\\n").encode("utf-8"),
        stderr=b"",
    )

home = Path(sys.argv[2])
package = Path(sys.argv[3])
layout = installer.InstallLayout.for_user(home=home)
manager = installer.Installer(
    package,
    layout,
    python_executable=sys.executable,
)
updater.subprocess.run = fake_run
print(json.dumps(manager.install(repair=True), sort_keys=True))
""".lstrip(),
            encoding="utf-8",
        )
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(probe),
                str(ROOT),
                str(self.home),
                str(self.package),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        return json.loads(process.stdout)

    def test_install_is_side_by_side_private_and_preserves_shared_modes(self) -> None:
        result = self._install()
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        config = (self.codex_dir / "config.toml").read_bytes()
        self.assertTrue(config.startswith(self.v1_config))
        self.assertEqual(1, config.count(b"[mcp_servers.Roblox_Studio_v2]"))
        self.assertIn(b'default_tools_approval_mode = "writes"', config)
        self.assertIn(b"required = false", config)
        self.assertNotIn(b"STUDIO_MCP_V2_CLIENT_TOKEN", config)
        self.assertEqual(self.v1_plugin_hash, _sha256(self.v1_plugin))
        self.assertTrue(self.layout.plugin_target.is_file())
        self.assertNotEqual(self.v1_plugin, self.layout.plugin_target)
        self.assertEqual(
            0o600, stat.S_IMODE(self.layout.secrets_config.stat().st_mode)
        )
        self.assertEqual(0o755, stat.S_IMODE(self.codex_dir.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(self.plugins.stat().st_mode))
        secrets_value = json.loads(self.layout.secrets_config.read_text())
        self.assertNotEqual(
            secrets_value["client_token"], secrets_value["studio_token"]
        )

    def test_repeat_install_is_a_true_noop_and_does_not_stop(self) -> None:
        self._install()
        state_before = self.layout.install_state.read_bytes()
        plugin_before = _sha256(self.layout.plugin_target)
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            side_effect=AssertionError("no-op install must not stop"),
        ):
            result = self.manager.install()
        self.assertFalse(result["changed"])
        self.assertIsNone(result["lifecycle_stop"])
        self.assertEqual(state_before, self.layout.install_state.read_bytes())
        self.assertEqual(plugin_before, _sha256(self.layout.plugin_target))

    def test_direct_cross_version_install_requires_exact_live_update_fence(
        self,
    ) -> None:
        self._install()
        previous_version = "0.2.0-direct"
        state = json.loads(self.layout.install_state.read_text())
        state["version"] = previous_version
        durable._atomic_write(
            self.layout.install_state, durable._json_bytes(state), 0o600
        )
        protected = {
            self.layout.install_state: self.layout.install_state.read_bytes(),
            self.layout.codex_config: self.layout.codex_config.read_bytes(),
            self.layout.plugin_target: self.layout.plugin_target.read_bytes(),
        }
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            side_effect=AssertionError("direct install must not stop"),
        ):
            with self.assertRaisesRegex(
                durable.InstallError, "manage update"
            ):
                self.manager.install()
        for path, expected in protected.items():
            self.assertEqual(expected, path.read_bytes(), str(path))

        release_updater = updater.ReleaseUpdater(
            self.manager,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        recovery_snapshot = updater._OwnedSnapshot.capture(self.layout)
        recovery_pending = release_updater._begin_pending_validation(
            action="update",
            previous_version=previous_version,
            current_version=durable.VERSION,
            snapshot=recovery_snapshot,
        )
        recovery_nonce = recovery_pending["nonce"]
        updater._ACTIVE_VALIDATION_NONCES.discard(recovery_nonce)
        updater._ACTIVE_RECOVERY_NONCES.add(recovery_nonce)
        try:
            with self.assertRaisesRegex(
                durable.InstallError, "manage update"
            ):
                self.manager.install()
        finally:
            updater._ACTIVE_RECOVERY_NONCES.discard(recovery_nonce)
            release_updater._clear_pending_validation(recovery_nonce)

        candidate_snapshot = updater._OwnedSnapshot.capture(self.layout)
        candidate_pending = release_updater._begin_pending_validation(
            action="update",
            previous_version=previous_version,
            current_version=durable.VERSION,
            snapshot=candidate_snapshot,
        )
        candidate_nonce = candidate_pending["nonce"]
        try:
            with mock.patch.object(
                self.manager,
                "_safe_stop_lifecycle",
                return_value={
                    "ok": True,
                    "running": False,
                    "stopped": False,
                },
            ):
                installed = self.manager.install()
            self.assertTrue(installed["ok"])
            self.assertEqual(durable.VERSION, installed["version"])
        finally:
            release_updater._clear_pending_validation(candidate_nonce)
            updater._ACTIVE_VALIDATION_NONCES.discard(candidate_nonce)

    def test_repair_restores_corrupt_plugin_after_safe_stop(self) -> None:
        self._install()
        expected = self.layout.plugin_artifact.read_bytes()
        self.layout.plugin_target.write_bytes(b"corrupt")
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            return_value=self._stopped(),
        ) as stopped:
            result = self.manager.install(repair=True)
        stopped.assert_called_once()
        self.assertTrue(result["changed"])
        self.assertEqual(expected, self.layout.plugin_target.read_bytes())
        self.assertTrue(any((self.layout.backups / "plugin").iterdir()))
        self.assertEqual(self.v1_plugin_hash, _sha256(self.v1_plugin))

    def test_repair_uses_verified_ephemeral_launcher_when_stable_one_is_missing(
        self,
    ) -> None:
        self._install()
        self.layout.launcher.unlink()
        self.layout.plugin_target.write_bytes(b"corrupt")
        fake = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'{"ok":true,"running":false,"stopped":false}\n',
            stderr=b"",
        )
        with mock.patch("release_tools.installer.subprocess.run", return_value=fake):
            result = self.manager.install(repair=True)
        self.assertTrue(result["changed"])
        self.assertTrue(self.layout.launcher.is_file())
        self.assertFalse(
            any(self.layout.run.glob(".repair-launcher-*.py")),
            "ephemeral recovery launchers must be removed",
        )

    def test_repair_recovers_crash_with_candidate_or_corrupt_install_state(
        self,
    ) -> None:
        for corrupt_state in (False, True):
            with self.subTest(corrupt_state=corrupt_state):
                if corrupt_state:
                    self.tearDown()
                    self.setUp()
                (
                    release_updater,
                    _snapshot,
                    pending,
                    originals,
                ) = self._simulate_crashed_release_switch(
                    corrupt_install_state=corrupt_state
                )
                status = release_updater.status()
                self.assertFalse(status["ok"])
                self.assertTrue(status["recovery"]["recoverable"])
                self.assertEqual("repair", status["recovery"]["repair_command"])
                with mock.patch(
                    "release_tools.updater.subprocess.run",
                    side_effect=self._recovery_process_result,
                ):
                    result = self.manager.install(repair=True)
                self.assertTrue(result["recovered"])
                self.assertEqual(
                    durable.VERSION, result["version"]
                )
                self.assertEqual(
                    pending["current_version"],
                    result["discarded_candidate_version"],
                )
                for path, expected in originals.items():
                    self.assertEqual(expected, path.read_bytes(), str(path))
                self.assertFalse(release_updater._pending_path().exists())

    def test_fresh_process_repair_recovers_stale_pending_marker(self) -> None:
        release_updater, _snapshot, pending, originals = (
            self._simulate_crashed_release_switch(
                corrupt_install_state=True
            )
        )
        pending_before = release_updater._pending_path().read_bytes()
        half_candidate_before = {
            path: path.read_bytes() for path in originals
        }
        with self.assertRaisesRegex(durable.InstallError, "run repair"):
            self.manager.install()
        self.assertEqual(
            pending_before, release_updater._pending_path().read_bytes()
        )
        for path, expected in half_candidate_before.items():
            self.assertEqual(expected, path.read_bytes(), str(path))

        result = self._fresh_process_repair()
        self.assertTrue(result["recovered"])
        self.assertEqual(durable.VERSION, result["version"])
        self.assertEqual(
            pending["current_version"],
            result["discarded_candidate_version"],
        )
        self.assertFalse(release_updater._pending_path().exists())
        for path, expected in originals.items():
            self.assertEqual(expected, path.read_bytes(), str(path))

    def test_fresh_process_repair_aborts_interrupted_rollback(self) -> None:
        release_updater, target_version, originals = (
            self._simulate_crashed_rollback()
        )
        pending = json.loads(release_updater._pending_path().read_text())
        self.assertEqual("rollback", pending["action"])
        self.assertEqual(durable.VERSION, pending["previous_version"])
        self.assertEqual(target_version, pending["current_version"])
        self.assertEqual(
            target_version,
            json.loads(self.layout.install_state.read_text())["version"],
        )

        result = self._fresh_process_repair()
        self.assertTrue(result["recovered"])
        self.assertEqual(durable.VERSION, result["version"])
        self.assertEqual("rollback", result["interrupted_action"])
        self.assertFalse(release_updater._pending_path().exists())
        for path, expected in originals.items():
            self.assertEqual(expected, path.read_bytes(), str(path))

    def test_pending_marker_create_and_clear_are_directory_fsynced(
        self,
    ) -> None:
        self._install()
        release_updater = updater.ReleaseUpdater(
            self.manager,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        snapshot = updater._OwnedSnapshot.capture(self.layout)
        pending_parent = release_updater._pending_path().parent
        original_sync = updater._fsync_directory
        with mock.patch(
            "release_tools.updater._fsync_directory",
            wraps=original_sync,
        ) as synced:
            pending = release_updater._begin_pending_validation(
                action="update",
                previous_version=durable.VERSION,
                current_version="9.9.9",
                snapshot=snapshot,
            )
        self.assertIn(
            mock.call(pending_parent), synced.call_args_list
        )
        marker_raw = release_updater._pending_path().read_bytes()

        sync_calls = {"count": 0}

        def fail_first_sync(path):
            sync_calls["count"] += 1
            if sync_calls["count"] == 1:
                raise updater.UpdateError(
                    "simulated directory fsync failure"
                )
            return original_sync(path)

        with mock.patch(
            "release_tools.updater._fsync_directory",
            side_effect=fail_first_sync,
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "marker was restored"
            ):
                release_updater._clear_pending_validation(
                    pending["nonce"]
                )
        self.assertEqual(
            marker_raw, release_updater._pending_path().read_bytes()
        )

        with mock.patch(
            "release_tools.updater._fsync_directory",
            wraps=original_sync,
        ) as synced:
            release_updater._clear_pending_validation(pending["nonce"])
        self.assertIn(
            mock.call(pending_parent), synced.call_args_list
        )
        self.assertFalse(release_updater._pending_path().exists())
        updater._ACTIVE_VALIDATION_NONCES.discard(pending["nonce"])

    def test_installer_atomic_replace_directory_fsync_is_fail_closed(
        self,
    ) -> None:
        target = self.root / "atomic" / "owned.json"
        target.parent.mkdir()
        original_sync = durable._fsync_directory
        with mock.patch(
            "release_tools.installer._fsync_directory",
            wraps=original_sync,
        ) as synced:
            durable._atomic_write(target, b'{"ok":true}\n', 0o600)
        self.assertIn(
            mock.call(target.parent), synced.call_args_list
        )

        with mock.patch(
            "release_tools.installer._fsync_directory",
            side_effect=durable.InstallError(
                "simulated directory fsync refusal"
            ),
        ):
            with self.assertRaisesRegex(
                durable.InstallError, "fsync refusal"
            ):
                durable._atomic_write(target, b'{"ok":false}\n', 0o600)
        self.assertEqual(b'{"ok":false}\n', target.read_bytes())

    def test_crash_recovery_refuses_wrong_ack_tamper_and_out_of_root(
        self,
    ) -> None:
        release_updater, snapshot, pending, originals = (
            self._simulate_crashed_release_switch()
        )
        protected_before = {
            path: path.read_bytes() for path in originals
        }
        with mock.patch(
            "release_tools.updater.subprocess.run",
            side_effect=AssertionError("ack failure must precede stop"),
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "acknowledgement does not match"
            ):
                release_updater.recover_interrupted_update(
                    accept_pending_sha256="0" * 64,
                    accept_candidate_version=pending["current_version"],
                )
        for path, expected in protected_before.items():
            self.assertEqual(expected, path.read_bytes())

        snapshot_plugin = (
            snapshot.root / "external" / "studio-plugin"
        )
        snapshot_plugin.write_bytes(b"tampered snapshot")
        with mock.patch(
            "release_tools.updater.subprocess.run",
            side_effect=AssertionError("tamper failure must precede stop"),
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "snapshot external file changed"
            ):
                release_updater.recover_interrupted_update()
        for path, expected in protected_before.items():
            self.assertEqual(expected, path.read_bytes())

        # Recreate a clean crash record, then redirect only its claimed
        # snapshot path outside the v2-owned backup root.
        self.tearDown()
        self.setUp()
        release_updater, _snapshot, _pending, originals = (
            self._simulate_crashed_release_switch()
        )
        pending_path = release_updater._pending_path()
        value = json.loads(pending_path.read_text())
        outside = self.root / "outside-snapshot"
        outside.mkdir()
        value["snapshot"] = str(outside)
        pending_path.write_text(json.dumps(value) + "\n")
        protected_before = {
            path: path.read_bytes() for path in originals
        }
        with mock.patch(
            "release_tools.updater.subprocess.run",
            side_effect=AssertionError("path failure must precede stop"),
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "outside the owned backup root"
            ):
                release_updater.recover_interrupted_update()
        for path, expected in protected_before.items():
            self.assertEqual(expected, path.read_bytes())

    def test_retained_installer_is_verified_before_any_module_execution(
        self,
    ) -> None:
        self._install()
        sentinel = self.root / "tampered-install-executed"
        install_script = self.layout.package / "install.py"
        original = install_script.read_text(encoding="utf-8")
        install_script.write_text(
            "from pathlib import Path\n"
            + "Path("
            + repr(str(sentinel))
            + ").write_text('executed', encoding='utf-8')\n"
            + original,
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            updater.UpdateError, "trusted pre-execution checks"
        ):
            updater._candidate_installer(
                self.manager,
                self.layout.package,
                durable.VERSION,
            )
        self.assertFalse(sentinel.exists())

    def test_rollback_target_manifest_anchor_precedes_installer_execution(
        self,
    ) -> None:
        self._install()
        target_version = "0.2.0-anchor"
        retained_target = self.layout.packages / target_version
        shutil.copytree(self.layout.package, retained_target)
        target_install = retained_target / "install.py"
        target_install_text = target_install.read_text(encoding="utf-8")
        current_literal = 'VERSION = "' + durable.VERSION + '"'
        target_literal = 'VERSION = "' + target_version + '"'
        self.assertIn(current_literal, target_install_text)
        target_install_text = target_install_text.replace(
            current_literal, target_literal, 1
        )
        target_install.write_text(
            target_install_text, encoding="utf-8"
        )
        target_manifest_path = (
            retained_target / "release-manifest.json"
        )
        target_manifest = json.loads(target_manifest_path.read_text())
        target_manifest["version"] = target_version
        for item in target_manifest["files"]:
            if item["path"] == "install.py":
                item["sha256"] = _sha256(target_install)
                item["size"] = target_install.stat().st_size
                break
        else:
            self.fail("release manifest lacks install.py")
        target_manifest_path.write_bytes(
            durable._json_bytes(target_manifest)
        )
        trusted_target_manifest_sha256 = _sha256(
            target_manifest_path
        )

        current_state_raw = self.layout.install_state.read_bytes()
        target_state = json.loads(current_state_raw)
        target_state["version"] = target_version
        target_state[
            "release_manifest_sha256"
        ] = trusted_target_manifest_sha256
        durable._atomic_write(
            self.layout.install_state,
            durable._json_bytes(target_state),
            0o600,
        )
        target_snapshot = updater._OwnedSnapshot.capture(self.layout)
        durable._atomic_write(
            self.layout.install_state, current_state_raw, 0o600
        )
        release_updater = updater.ReleaseUpdater(
            self.manager,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        release_updater._write_receipt(
            action="update",
            previous_version=target_version,
            current_version=durable.VERSION,
            archive_sha256="b" * 64,
            snapshot=target_snapshot,
        )

        sentinel = self.root / "rollback-target-executed"
        attacked_install = (
            "from pathlib import Path\n"
            + "Path("
            + repr(str(sentinel))
            + ").write_text('executed', encoding='utf-8')\n"
            + target_install.read_text(encoding="utf-8")
        )
        target_install.write_text(attacked_install, encoding="utf-8")
        attacked_manifest = json.loads(target_manifest_path.read_text())
        for item in attacked_manifest["files"]:
            if item["path"] == "install.py":
                item["sha256"] = _sha256(target_install)
                item["size"] = target_install.stat().st_size
        target_manifest_path.write_bytes(
            durable._json_bytes(attacked_manifest)
        )

        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            side_effect=AssertionError(
                "rollback stop must not precede target package anchoring"
            ),
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "does not match ownership state"
            ):
                release_updater.rollback(
                    to_version=target_version,
                    accept_current_version=durable.VERSION,
                )
        self.assertFalse(sentinel.exists())
        self.assertFalse(release_updater._pending_path().exists())

    def test_extra_release_module_is_rejected_before_lifecycle_stop(
        self,
    ) -> None:
        release_updater, _snapshot, _pending, _originals = (
            self._simulate_crashed_release_switch()
        )
        sentinel = self.root / "extra-module-executed"
        (self.layout.release / "json.py").write_text(
            "from pathlib import Path\n"
            + "Path("
            + repr(str(sentinel))
            + ").write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        with mock.patch(
            "release_tools.updater.subprocess.run",
            side_effect=AssertionError(
                "lifecycle stop must not run with an extra release module"
            ),
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "exact file-set verification failed"
            ):
                release_updater.recover_interrupted_update()
        self.assertFalse(sentinel.exists())
        self.assertTrue(release_updater._pending_path().is_file())

    def test_crash_recovery_stop_refusal_does_not_restore_any_owned_byte(
        self,
    ) -> None:
        release_updater, _snapshot, _pending, originals = (
            self._simulate_crashed_release_switch()
        )
        pending_before = release_updater._pending_path().read_bytes()
        protected_before = {
            path: path.read_bytes() for path in originals
        }
        refusal = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout=b'{"ok":false,"running":true,"stopped":false}\n',
            stderr=b"",
        )
        with mock.patch(
            "release_tools.updater.subprocess.run",
            return_value=refusal,
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "stop was refused safely"
            ):
                release_updater.recover_interrupted_update()
        self.assertEqual(
            pending_before, release_updater._pending_path().read_bytes()
        )
        for path, expected in protected_before.items():
            self.assertEqual(expected, path.read_bytes())

    def test_crash_during_recovery_is_resumable_and_clears_marker_last(
        self,
    ) -> None:
        release_updater, _snapshot, _pending, originals = (
            self._simulate_crashed_release_switch()
        )

        class RefusingDoctor:
            def doctor(self):
                return {"ok": False}

        first_attempt = updater.ReleaseUpdater(
            self.manager,
            candidate_factory=lambda *_args: RefusingDoctor(),
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        with mock.patch(
            "release_tools.updater.subprocess.run",
            side_effect=self._recovery_process_result,
        ):
            with self.assertRaisesRegex(
                updater.UpdateError, "failed real doctor"
            ):
                first_attempt.recover_interrupted_update()
        self.assertTrue(release_updater._pending_path().is_file())
        for path, expected in originals.items():
            self.assertEqual(expected, path.read_bytes(), str(path))

        with mock.patch(
            "release_tools.updater.subprocess.run",
            side_effect=self._recovery_process_result,
        ):
            result = self.manager.install(repair=True)
        self.assertTrue(result["recovered"])
        self.assertFalse(release_updater._pending_path().exists())
        for path, expected in originals.items():
            self.assertEqual(expected, path.read_bytes(), str(path))

    def test_config_collision_and_drift_fail_closed(self) -> None:
        (self.codex_dir / "config.toml").write_bytes(
            self.v1_config
            + b"\n[mcp_servers.Roblox_Studio_v2]\n"
            + b'command = "/tmp/unowned"\n'
        )
        with self.assertRaises(durable.InstallError):
            self._install()
        self.assertFalse(self.layout.support_root.exists())

        (self.codex_dir / "config.toml").write_bytes(self.v1_config)
        self._install()
        config_path = self.codex_dir / "config.toml"
        config_path.write_bytes(
            config_path.read_bytes().replace(
                b"tool_timeout_sec = 180", b"tool_timeout_sec = 999"
            )
        )
        with self.assertRaises(durable.InstallError):
            self.manager.install(repair=True)

    def test_identical_unowned_codex_table_is_never_adopted(self) -> None:
        expected = durable._expected_codex_block(self.layout)
        (self.codex_dir / "config.toml").write_bytes(
            self.v1_config + b"\n" + expected
        )
        with self.assertRaisesRegex(durable.InstallError, "unowned"):
            self._install()
        self.assertFalse(self.layout.support_root.exists())

    def test_uninstall_removes_owned_separator_and_restores_exact_config(self) -> None:
        self._install()
        state = json.loads(self.layout.install_state.read_text())
        self.assertEqual("0a", state["codex"]["inserted_separator_hex"])
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            return_value=self._stopped(),
        ):
            result = self.manager.uninstall()
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.v1_config, (self.codex_dir / "config.toml").read_bytes()
        )
        self.assertEqual(self.v1_plugin_hash, _sha256(self.v1_plugin))
        self.assertFalse(self.layout.plugin_target.exists())
        self.assertFalse(self.layout.support_root.exists())
        self.assertTrue(Path(result["support_recovery"]).is_dir())

    def test_uninstall_aborts_on_stop_refusal_without_partial_changes(self) -> None:
        self._install()
        config_before = (self.codex_dir / "config.toml").read_bytes()
        plugin_before = self.layout.plugin_target.read_bytes()
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            side_effect=durable.InstallError("busy session"),
        ):
            with self.assertRaises(durable.InstallError):
                self.manager.uninstall()
        self.assertEqual(config_before, (self.codex_dir / "config.toml").read_bytes())
        self.assertEqual(plugin_before, self.layout.plugin_target.read_bytes())
        self.assertTrue(self.layout.support_root.is_dir())

    def test_broad_prefixes_are_rejected(self) -> None:
        for target in (
            Path("/"),
            self.home,
            self.home / "Library",
            self.home / "Documents",
            self.home / ".codex",
            self.plugins,
        ):
            with self.subTest(target=target):
                with self.assertRaises(durable.InstallError):
                    durable.InstallLayout.for_user(
                        home=self.home, prefix=target
                    )

    def test_unsupported_platform_refuses_before_any_install_mutation(
        self,
    ) -> None:
        with mock.patch(
            "release_tools.installer.require_supported_platform",
            side_effect=durable.UnsupportedPlatformError(
                "native Apple Silicon macOS only. No files were changed."
            ),
        ):
            with self.assertRaisesRegex(
                durable.InstallError, "No files were changed"
            ):
                self.manager.install()
        self.assertFalse(self.layout.support_root.exists())
        self.assertEqual(
            self.v1_config, (self.codex_dir / "config.toml").read_bytes()
        )
        self.assertEqual(self.v1_plugin_hash, _sha256(self.v1_plugin))

    def test_incompatible_catalog_never_stops_or_mutates(self) -> None:
        self._install()
        candidate = json.loads(self.layout.upstream_catalog.read_text())
        candidate["tools"].append(
            {
                "name": "unknown_unmapped_operation",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            }
        )
        path = self.root / "incompatible.json"
        path.write_text(json.dumps(candidate))
        before = self.layout.effective_catalog.read_bytes()
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            side_effect=AssertionError("review failure must precede stop"),
        ):
            with self.assertRaises(durable.InstallError):
                self.manager.catalog_import(path, _sha256(path))
        self.assertEqual(before, self.layout.effective_catalog.read_bytes())

    def test_reviewed_catalog_import_updates_artifacts_and_is_hash_gated(
        self,
    ) -> None:
        self._install()
        candidate = json.loads(self.layout.upstream_catalog.read_text())
        durable_catalog = json.loads(self.layout.effective_catalog.read_text())
        script_schema = next(
            tool["inputSchema"]
            for tool in durable_catalog["tools"]
            if tool["name"] == "studio_read_script"
        )
        candidate["catalog_version"] = "portable-test-compatible"
        candidate["tools"].append(
            {
                "name": "studio_structured_script_read",
                "inputSchema": script_schema,
            }
        )
        path = self.root / "compatible.json"
        path.write_text(json.dumps(candidate, indent=2) + "\n")
        digest = _sha256(path)
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            return_value=self._stopped(),
        ), mock.patch.object(
            self.manager,
            "_start_and_verify_catalog",
            return_value=self._verified_runtime(),
        ):
            result = self.manager.catalog_import(path, digest)
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.layout.effective_catalog.read_bytes(),
            self.layout.catalog_artifact.read_bytes(),
        )
        self.assertEqual(
            self.layout.upstream_catalog.read_bytes(),
            (
                self.layout.artifacts / "upstream-known-tool-catalog.json"
            ).read_bytes(),
        )
        installed = json.loads(self.layout.effective_catalog.read_text())
        self.assertEqual(
            "portable-test-compatible", installed["upstream"]["version"]
        )

    def test_import_does_not_rewrite_catalog_if_started_broker_refuses_restoration_stop(
        self,
    ) -> None:
        self._install()
        candidate = json.loads(self.layout.upstream_catalog.read_text())
        durable_catalog = json.loads(self.layout.effective_catalog.read_text())
        script_schema = next(
            tool["inputSchema"]
            for tool in durable_catalog["tools"]
            if tool["name"] == "studio_read_script"
        )
        candidate["catalog_version"] = "post-start-refusal-test"
        candidate["tools"].append(
            {
                "name": "studio_structured_script_read",
                "inputSchema": script_schema,
            }
        )
        path = self.root / "post-start-refusal.json"
        path.write_text(json.dumps(candidate, indent=2) + "\n")
        stop_results = [
            self._stopped(),
            durable.InstallError("new broker is busy"),
        ]

        def stop_side_effect() -> dict:
            value = stop_results.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            side_effect=stop_side_effect,
        ), mock.patch.object(
            self.manager,
            "_start_and_verify_catalog",
            side_effect=durable.InstallError("digest probe failed after start"),
        ):
            with self.assertRaisesRegex(
                durable.InstallError, "rollback was not attempted"
            ):
                self.manager.catalog_import(path, _sha256(path))
        installed = json.loads(self.layout.effective_catalog.read_text())
        self.assertEqual(
            "post-start-refusal-test", installed["upstream"]["version"]
        )
        self.assertEqual(
            self.layout.effective_catalog.read_bytes(),
            self.layout.catalog_artifact.read_bytes(),
        )
        state = json.loads(self.layout.install_state.read_text())
        self.assertEqual(
            _sha256(self.layout.effective_catalog),
            state["catalog"]["sha256"],
        )

    def test_runtime_catalog_proof_must_match_exact_effective_catalog(self) -> None:
        self._install()
        responses = [
            {"ok": True, "broker": {"catalog_sha256": "b" * 64}},
            {
                "ok": True,
                "catalog": {"catalog_sha256": "b" * 64},
            },
        ]
        with mock.patch.object(
            self.manager,
            "_invoke_lifecycle",
            side_effect=responses,
        ):
            with self.assertRaisesRegex(
                durable.InstallError, "active catalog digest"
            ):
                self.manager._start_and_verify_catalog()

    def test_portable_archive_is_reproducible_and_contains_no_local_run_data(
        self,
    ) -> None:
        second = builder.build_release(ROOT, self.root / "dist-second")
        self.assertEqual(self.archive_sha256, second.sha256)
        self.assertEqual(self.archive.read_bytes(), second.archive.read_bytes())
        first = builder.build_release(ROOT, self.root / "dist-third")
        self.assertEqual(
            first.bootstrap_sha256, second.bootstrap_sha256
        )
        self.assertEqual(
            first.bootstrap.read_bytes(), second.bootstrap.read_bytes()
        )
        checksum_lines = second.checksum_manifest.read_text(
            encoding="ascii"
        ).splitlines()
        self.assertEqual(2, len(checksum_lines))
        self.assertTrue(
            any(builder.ARCHIVE_FILENAME in line for line in checksum_lines)
        )
        self.assertTrue(
            any(builder.BOOTSTRAP_FILENAME in line for line in checksum_lines)
        )
        with tarfile.open(self.archive, "r:gz") as package:
            names = [member.name for member in package.getmembers()]
            contents = b"".join(
                package.extractfile(member).read()
                for member in package.getmembers()
                if member.isfile()
            )
        self.assertFalse(any("live-v2-run" in name for name in names))
        for forbidden in (
            b"host-context.json",
            b"client-context.json",
            b"run-manifest.json",
            b"/Users/",
        ):
            self.assertNotIn(forbidden, contents)

    def test_real_doctor_accepts_only_live_fenced_sequential_update_chain(
        self,
    ) -> None:
        self._install()
        state = json.loads(self.layout.install_state.read_text())
        version_a = "1.0.0"
        version_b = "1.1.0"
        version_c = "1.2.0"
        state["version"] = version_a
        durable._atomic_write(
            self.layout.install_state, durable._json_bytes(state), 0o600
        )
        real_doctor_reports = []

        class Candidate:
            def __init__(candidate_self, version: str):
                candidate_self.version = version

            def install(candidate_self) -> dict:
                candidate_state = json.loads(
                    self.layout.install_state.read_text()
                )
                candidate_state["version"] = candidate_self.version
                durable._atomic_write(
                    self.layout.install_state,
                    durable._json_bytes(candidate_state),
                    0o600,
                )
                return {"ok": True, "version": candidate_self.version}

            def doctor(candidate_self) -> dict:
                del candidate_self
                report = self.manager.doctor()
                real_doctor_reports.append(report)
                return report

        release_updater = updater.ReleaseUpdater(
            self.manager,
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        lifecycle_doctor = {
            "ok": True,
            "lifecycle": {"condition": "stopped"},
            "catalog": {"installed_v1_cache": None},
        }
        with mock.patch.object(
            self.manager,
            "_invoke_lifecycle",
            return_value=lifecycle_doctor,
        ):
            first = release_updater._transactional_switch(
                candidate=Candidate(version_b),
                previous_version=version_a,
                current_version=version_b,
                action="update",
                archive_sha256="a" * 64,
            )
            second = release_updater._transactional_switch(
                candidate=Candidate(version_c),
                previous_version=version_b,
                current_version=version_c,
                action="update",
                archive_sha256="b" * 64,
            )
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(2, len(real_doctor_reports))
        self.assertTrue(all(report["ok"] for report in real_doctor_reports))
        second_pending = real_doctor_reports[1]["release_updates"][
            "pending_validation"
        ]
        self.assertEqual(version_b, second_pending["previous_version"])
        self.assertEqual(version_c, second_pending["current_version"])
        self.assertFalse(
            (
                self.layout.backups
                / "release-updates"
                / updater.PENDING_TRANSACTION_FILENAME
            ).exists()
        )
        status = release_updater.status()
        self.assertTrue(status["ok"])
        self.assertEqual(version_c, status["installed_version"])
        self.assertEqual(version_b, status["rollback"]["target_version"])

        retained_b = self.layout.packages / version_b
        retained_b.mkdir(mode=0o700)
        (retained_b / "release-manifest.json").write_text(
            json.dumps(
                {
                    "format": durable.PACKAGE_FORMAT,
                    "product": durable.PRODUCT,
                    "version": version_b,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rollback_updater = updater.ReleaseUpdater(
            self.manager,
            candidate_factory=(
                lambda _current, _package, version: Candidate(version)
            ),
            platform_check=lambda: None,
            runtime_check=lambda: None,
        )
        with mock.patch.object(
            self.manager,
            "_safe_stop_lifecycle",
            return_value={"ok": True, "running": False, "stopped": False},
        ), mock.patch.object(
            self.manager,
            "_invoke_lifecycle",
            return_value=lifecycle_doctor,
        ):
            rolled_back = rollback_updater.rollback(
                to_version=version_b,
                accept_current_version=version_c,
            )
        self.assertTrue(rolled_back["ok"])
        self.assertEqual(version_b, rolled_back["version"])
        self.assertEqual(3, len(real_doctor_reports))
        self.assertTrue(real_doctor_reports[-1]["ok"])
        rollback_pending = real_doctor_reports[-1]["release_updates"][
            "pending_validation"
        ]
        self.assertEqual("rollback", rollback_pending["action"])
        self.assertEqual(version_c, rollback_pending["previous_version"])
        self.assertEqual(version_b, rollback_pending["current_version"])

    def test_isolated_launcher_ignores_hostile_cwd_and_pythonpath(self) -> None:
        fake_release = self.root / "releases" / durable.VERSION
        package = fake_release / "studio_mcp_v2"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "lifecycle.py").write_text(
            "def main_for_installed_support_root(root):\n"
            "    import json\n"
            "    print(json.dumps({'ok': True, 'origin': 'pinned', "
            "'root': str(root)}))\n"
        )
        hostile = self.root / "hostile"
        hostile_package = hostile / "studio_mcp_v2"
        hostile_package.mkdir(parents=True)
        (hostile_package / "__init__.py").write_text("")
        (hostile_package / "lifecycle.py").write_text(
            "def main_for_installed_support_root(root):\n"
            "    print('HOSTILE')\n"
        )
        template = (
            ROOT / "release_tools" / "runtime_launcher.py"
        ).read_text()
        replacements = {
            "__SUPPORT_ROOT_LITERAL__": repr(str(self.root)),
            "__RELEASE_ROOT_LITERAL__": repr(str(fake_release)),
            "__PYTHON_EXECUTABLE_LITERAL__": repr(sys.executable),
            "__ENTRYPOINT_MODULE_LITERAL__": repr("studio_mcp_v2.lifecycle"),
        }
        for key, value in replacements.items():
            template = template.replace(key, value)
        bootstrap = self.root / "bootstrap.py"
        bootstrap.write_text(template)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(hostile)
        environment["PYTHONSTARTUP"] = str(hostile / "startup.py")
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(bootstrap)],
            cwd=hostile,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual("pinned", json.loads(process.stdout)["origin"])
        self.assertNotIn("HOSTILE", process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
