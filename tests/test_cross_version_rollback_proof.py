from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release_tools import builder
from release_tools.cross_version_proof import (
    MULTISESSION_CANDIDATE_VERSION,
    MULTISESSION_PRIOR_ARCHIVE_SHA256,
    MULTISESSION_PRIOR_MANIFEST_SHA256,
    MULTISESSION_PRIOR_VERSION,
    ProofError,
    RC4_VERSION,
    _active_fingerprint,
    _archive_manifest,
    _bounded_subprocesses,
    prove_multisession_migration_rollback,
)


ROOT = Path(__file__).resolve().parent.parent


class CrossVersionRollbackProofTests(unittest.TestCase):
    def test_rename_proof_has_a_separate_immutable_prior_identity(
        self,
    ) -> None:
        self.assertEqual("0.3.0-rc.4", RC4_VERSION)
        self.assertEqual("0.4.0-rc.4", MULTISESSION_PRIOR_VERSION)
        self.assertEqual(
            "0.4.0-rc.5", MULTISESSION_CANDIDATE_VERSION
        )
        self.assertEqual(
            (
                "21e75b1fa74fdc7463d29fde45dffaa35323cb5017e47b85b"
                "29289619988adf8"
            ),
            MULTISESSION_PRIOR_ARCHIVE_SHA256,
        )
        self.assertEqual(
            (
                "ce926e9e81ab0803c028831cf41614050e29016f11ac2ac073"
                "25556e63ab44cd"
            ),
            MULTISESSION_PRIOR_MANIFEST_SHA256,
        )

    def test_rename_proof_rejects_the_wrong_candidate_before_io(
        self,
    ) -> None:
        missing = ROOT / "does-not-exist"
        with self.assertRaisesRegex(
            ProofError, "requires candidate 0.4.0-rc.5"
        ):
            prove_multisession_migration_rollback(
                prior_archive=missing,
                prior_checksum_file=missing,
                candidate_archive=missing,
                candidate_checksum_file=missing,
                candidate_expected_sha256="0" * 64,
                candidate_version="0.4.0-rc.6",
                source_commit="0" * 40,
                source_tree="0" * 40,
            )

    def test_wrong_checksum_fails_before_archive_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            built = builder.build_release(ROOT, Path(temporary))
            with mock.patch(
                "release_tools.cross_version_proof.audit_archive",
                side_effect=AssertionError(
                    "checksum mismatch must fail before audit"
                ),
            ):
                with self.assertRaisesRegex(ProofError, "SHA-256"):
                    _archive_manifest(
                        built.archive,
                        checksum_file=built.checksum_file,
                        expected_sha256="0" * 64,
                        expected_version=builder.installer.VERSION,
                    )

    def test_active_manifest_detects_byte_and_mode_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            support = root / "support"
            for name in ("config", "artifacts", "state", "bin"):
                (support / name).mkdir(mode=0o700, parents=True)
            (support / "config" / "runtime.json").write_bytes(b"one")
            (support / "artifacts" / "plugin.rbxmx").write_bytes(
                b"plugin"
            )
            (support / "bin" / "manager").write_bytes(b"manager")
            install_state = (
                support / "state" / "install-state.json"
            )
            install_state.write_bytes(b"state")
            codex = root / "config.toml"
            codex.write_bytes(b"codex")
            plugin = root / "StudioMCPv2.rbxmx"
            plugin.write_bytes(b"plugin")
            layout = SimpleNamespace(
                support_root=support,
                install_state=install_state,
                codex_config=codex,
                plugin_target=plugin,
            )

            baseline = _active_fingerprint(layout)
            (support / "config" / "runtime.json").write_bytes(b"two")
            self.assertNotEqual(baseline, _active_fingerprint(layout))
            (support / "config" / "runtime.json").write_bytes(b"one")
            os.chmod(support / "bin" / "manager", 0o700)
            self.assertNotEqual(baseline, _active_fingerprint(layout))

    def test_subprocess_boundary_allows_only_disposable_stop(self) -> None:
        original = subprocess.run
        counters = {"subprocess_stop_calls": 0}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap = root / "snapshot-bootstrap.py"
            bootstrap.write_text("# synthetic\n", encoding="utf-8")
            with _bounded_subprocesses(root, counters):
                with self.assertRaisesRegex(
                    ProofError, "unexpected subprocess"
                ):
                    subprocess.run(["unexpected"])
                result = subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-B",
                        str(bootstrap),
                        "stop",
                        "--json",
                    ]
                )
                self.assertEqual(0, result.returncode)
                self.assertIn(b'"running": false', result.stdout)
        self.assertIs(original, subprocess.run)
        self.assertEqual(1, counters["subprocess_stop_calls"])


if __name__ == "__main__":
    unittest.main()
