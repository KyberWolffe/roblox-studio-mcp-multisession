from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import platform_support
from release_tools import builder


class PlatformSupportTests(unittest.TestCase):
    def test_native_apple_silicon_is_supported(self) -> None:
        status = platform_support.detect_platform(
            system="Darwin",
            machine="arm64",
            rosetta_translated=False,
        )
        self.assertTrue(status.supported)
        self.assertEqual("macos-arm64", status.target)

    def test_intel_and_rosetta_fail_with_non_mutating_message(self) -> None:
        cases = (
            ("Darwin", "x86_64", False, "machine architecture"),
            ("Darwin", "x86_64", True, "Rosetta"),
            ("Linux", "aarch64", False, "operating system"),
        )
        for system, machine, translated, reason in cases:
            with self.subTest(
                system=system,
                machine=machine,
                translated=translated,
            ):
                with self.assertRaisesRegex(
                    platform_support.UnsupportedPlatformError,
                    reason + r".*No files were changed",
                ):
                    platform_support.require_supported_platform(
                        system=system,
                        machine=machine,
                        rosetta_translated=translated,
                    )

    def test_sysctl_rosetta_probe_is_bounded_and_non_shell(self) -> None:
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="1\n",
            stderr="",
        )
        runner = mock.Mock(return_value=result)
        self.assertTrue(platform_support._detect_rosetta(runner))
        runner.assert_called_once_with(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def test_release_builder_refuses_unsupported_host_before_output(
        self,
    ) -> None:
        with mock.patch(
            "release_tools.builder.platform_support.require_supported_platform",
            side_effect=platform_support.UnsupportedPlatformError(
                "unsupported. No files were changed."
            ),
        ):
            with self.assertRaises(platform_support.UnsupportedPlatformError):
                builder.build_release(
                    Path(__file__).resolve().parent.parent,
                    Path("/tmp/unused-studio-mcp-v2-unsupported"),
                )

    def test_python_runtime_is_checked_before_mutation(self) -> None:
        self.assertEqual(
            (3, 9, 0),
            platform_support.require_supported_runtime((3, 9, 0)),
        )
        with self.assertRaisesRegex(
            platform_support.UnsupportedPlatformError,
            r"Python 3\.9 or newer.*No files were changed",
        ):
            platform_support.require_supported_runtime((3, 8, 18))


if __name__ == "__main__":
    unittest.main()
