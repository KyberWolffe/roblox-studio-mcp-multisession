from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.validate_capability_parity import (
    ParityValidationError,
    validate_capability_parity,
    validate_version_policy,
)


ROOT = Path(__file__).resolve().parent.parent


class CapabilityParityTests(unittest.TestCase):
    def _copy_contract(self, target: Path) -> None:
        (target / "config").mkdir(parents=True)
        (target / "docs").mkdir()
        for relative in (
            "config/tool-catalog.json",
            "config/durable-tool-catalog.json",
            "config/v1-capability-parity.json",
            "docs/CAPABILITY_PARITY.md",
            "docs/RELEASE_NOTES_0.4.0-rc.3.md",
        ):
            source = ROOT / relative
            destination = target / relative
            shutil.copyfile(source, destination)
        shutil.copyfile(ROOT / "README.md", target / "README.md")
        shutil.copyfile(ROOT / "SECURITY.md", target / "SECURITY.md")

    def test_current_matrix_is_exact_referenced_and_prerelease_gated(self) -> None:
        result = validate_capability_parity(
            ROOT,
            expected_tag="v0.4.0-rc.3",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(25, result["modern_tool_count"])
        self.assertEqual(6, result["excluded_legacy_alias_count"])
        self.assertFalse(result["p0_complete"])
        self.assertEqual(12, result["p0_gap_count"])
        self.assertEqual(
            {
                "v2_full": 3,
                "v2_partial": 10,
                "native_codex_equivalent": 2,
                "deferred": 10,
            },
            result["status_counts"],
        )
        self.assertFalse(result["full_parity_claimed"])
        self.assertTrue(result["no_global_v1_fallback"])

    def test_missing_modern_tool_fails_exact_set_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._copy_contract(root)
            path = root / "config" / "v1-capability-parity.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["tools"].pop()
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ParityValidationError, "exact ordered"):
                validate_capability_parity(
                    root,
                    release_version="0.4.0-rc.3",
                )

    def test_unknown_v2_reference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._copy_contract(root)
            path = root / "config" / "v1-capability-parity.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            script_read = next(
                item for item in value["tools"] if item["name"] == "script_read"
            )
            script_read["references"][0]["name"] = "missing_v2_handler"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ParityValidationError, "unknown durable"):
                validate_capability_parity(
                    root,
                    release_version="0.4.0-rc.3",
                )

    def test_p0_gaps_reject_stable_version_or_tag(self) -> None:
        with self.assertRaisesRegex(ParityValidationError, "prerelease"):
            validate_version_policy("0.3.0", p0_incomplete=True)
        with self.assertRaisesRegex(ParityValidationError, "exactly equal"):
            validate_version_policy(
                "0.3.0-rc.4",
                p0_incomplete=True,
                expected_tag="v0.3.0-rc.1",
            )

    def test_positive_full_parity_claim_fails_document_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._copy_contract(root)
            readme = root / "README.md"
            readme.write_text(
                readme.read_text(encoding="utf-8")
                + "\nFull capability parity achieved.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ParityValidationError, "positive"):
                validate_capability_parity(
                    root,
                    release_version="0.4.0-rc.3",
                )

    def test_tampered_route_p0_flag_gap_or_deleted_row_fails(self) -> None:
        cases = (
            (
                "`studio_read_script_v2`",
                "`invented_script_route_v2`",
            ),
            (
                "| `character_navigation` | P0 | deferred | **Yes** |",
                "| `character_navigation` | P0 | deferred | No |",
            ),
            (
                "No bounded session-local character navigation adapter exists.",
                "No gap remains.",
            ),
            (
                "| `store_image` | P1 | deferred | No | None | "
                "No safe equivalent of the upstream local-file IMAGEID "
                "storage mechanism exists. |\n",
                "",
            ),
            (
                "|---|---:|---|:---:|---|---|\n",
                "|---|---:|---|:---:|---|---|\n"
                "| `invented_tool` | P0 | v2 full | No | "
                "`invented_tool_v2` | None |\n",
            ),
        )
        for original, replacement in cases:
            with self.subTest(original=original):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self._copy_contract(root)
                    path = root / "docs" / "CAPABILITY_PARITY.md"
                    text = path.read_text(encoding="utf-8")
                    self.assertIn(original, text)
                    path.write_text(
                        text.replace(original, replacement, 1),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ParityValidationError,
                        "documentation row drifted",
                    ):
                        validate_capability_parity(
                            root,
                            release_version="0.4.0-rc.3",
                        )

    def test_readme_and_release_note_markers_are_mandatory(self) -> None:
        markers = (
            "<!-- experimental-prerelease: true -->",
            "<!-- capability-parity: incomplete -->",
            "<!-- global-v1-fallback: forbidden -->",
        )
        for relative in (
            "README.md",
            "docs/RELEASE_NOTES_0.4.0-rc.3.md",
        ):
            for marker in markers:
                with self.subTest(relative=relative, marker=marker):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        self._copy_contract(root)
                        path = root / relative
                        text = path.read_text(encoding="utf-8")
                        self.assertIn(marker, text)
                        path.write_text(
                            text.replace(marker, "", 1),
                            encoding="utf-8",
                        )
                        with self.assertRaisesRegex(
                            ParityValidationError,
                            "disclosure marker drifted",
                        ):
                            validate_capability_parity(
                                root,
                                release_version="0.4.0-rc.3",
                            )


if __name__ == "__main__":
    unittest.main()
