from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release_tools import builder
from release_tools.proof import ProofError, prove_release


ROOT = Path(__file__).resolve().parent.parent


class ReleaseProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.built = builder.build_release(ROOT, self.root / "dist")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_proof_uses_disposable_home_and_restores_v1(self) -> None:
        report = prove_release(
            self.built.archive,
            checksum_file=self.built.checksum_file,
            temporary_parent=self.root,
        )
        self.assertTrue(report["ok"])
        self.assertTrue(all(report["phases"].values()))
        self.assertTrue(
            report["phases"]["legacy_update_lock_marker"]
        )
        self.assertTrue(report["phases"]["no_op_repair"])
        self.assertTrue(report["isolation"]["temporary_home_removed"])
        self.assertFalse(report["isolation"]["network_or_broker_started"])
        self.assertGreaterEqual(
            report["isolation"]["simulated_stop_calls"], 3
        )
        self.assertFalse(
            any(
                path.name.startswith("studio-mcp-v2-release-proof-")
                for path in self.root.iterdir()
            )
        )

    def test_wrong_checksum_fails_before_audit_or_extraction(self) -> None:
        with mock.patch(
            "release_tools.proof.audit_archive",
            side_effect=AssertionError("checksum failure must be first"),
        ):
            with self.assertRaisesRegex(ProofError, "SHA-256"):
                prove_release(
                    self.built.archive,
                    expected_sha256="0" * 64,
                    temporary_parent=self.root,
                )

    def test_ambiguous_checksum_file_is_rejected(self) -> None:
        ambiguous = self.root / "ambiguous.sha256"
        line = self.built.sha256 + "  " + self.built.archive.name + "\n"
        ambiguous.write_text(line + line, encoding="ascii")
        with self.assertRaisesRegex(ProofError, "exactly one"):
            prove_release(
                self.built.archive,
                checksum_file=ambiguous,
                temporary_parent=self.root,
            )


if __name__ == "__main__":
    unittest.main()
