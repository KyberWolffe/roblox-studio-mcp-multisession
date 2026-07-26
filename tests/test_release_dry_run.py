from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from release_tools.dry_run import DryRunError, run_release_dry_run


ROOT = Path(__file__).resolve().parent.parent


class ReleaseDryRunTests(unittest.TestCase):
    def test_reproducible_proven_assets_can_be_staged_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "assets"
            result = run_release_dry_run(
                ROOT,
                output_directory=output,
                platform_check=lambda: None,
                runtime_check=lambda: None,
            )
            self.assertTrue(result["ok"])
            self.assertTrue(result["reproducible_build"]["ok"])
            self.assertEqual(
                25,
                result["capability_parity"]["modern_tool_count"],
            )
            self.assertFalse(result["capability_parity"]["p0_complete"])
            self.assertTrue(
                result["isolated_install_proof"]["update_rollback"]["tested"]
            )
            self.assertTrue((output / "SHA256SUMS").is_file())
            # Repeated staging is idempotent when every byte is unchanged.
            repeated = run_release_dry_run(
                ROOT,
                output_directory=output,
                platform_check=lambda: None,
                runtime_check=lambda: None,
            )
            self.assertEqual(
                result["reproducible_build"]["sha256"],
                repeated["reproducible_build"]["sha256"],
            )

    def test_output_inside_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(DryRunError, "outside"):
            run_release_dry_run(
                ROOT,
                output_directory=ROOT / "dist-proof",
                platform_check=lambda: None,
                runtime_check=lambda: None,
            )


if __name__ == "__main__":
    unittest.main()
