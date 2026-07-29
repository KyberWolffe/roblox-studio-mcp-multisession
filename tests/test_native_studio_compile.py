from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from release_tools import native_compile
from release_tools.native_compile import (
    EXPECTED_MAIN_ASSERTION,
    EXPECTED_MAIN_ASSERTION_MESSAGE,
    NativeCompileError,
    extract_exact_main_source,
    inspect_studio_identity,
    prove_native_studio_compilation,
    validate_candidate_guard_contract,
    validate_native_compile_receipt,
)
from scripts import native_studio_compile_smoke
from scripts.render_studio_plugin import package_rbxmx


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _guarded_source() -> str:
    return """local InitialRunService = game:GetService("RunService")
local initialStudioOk, initialIsStudio = pcall(function()
\treturn InitialRunService:IsStudio()
end)
local initialEditOk, initialIsEdit = pcall(function()
\treturn InitialRunService:IsEdit()
end)
local initialRunningOk, initialIsRunning = pcall(function()
\treturn InitialRunService:IsRunning()
end)

if initialStudioOk
\tand initialIsStudio == true
\tand initialRunningOk
\tand initialIsRunning == true
then
\treturn
end

if not initialStudioOk
\tor initialIsStudio ~= true
\tor not initialEditOk
\tor initialIsEdit ~= true
\tor not initialRunningOk
\tor initialIsRunning ~= false
then
\t-- A local plugin copy must never register a controller from an unknown or
\t-- non-Edit DataModel. Returning here also avoids touching the document.
\treturn
end

assert(plugin ~= nil, "Studio MCP v2 must be installed as a Studio plugin")

local CONFIG = table.freeze({
\trun_id = "AAAAAAAAAAAAAAAA",
})

local function connect(reconnecting)
\treturn reconnecting
end

local registered, registrationError = connect(false)
-- Exact UTF-8 evidence: π
"""


class NativeCompileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = _guarded_source().encode("utf-8")
        self.package = self.root / "candidate.rbxmx"
        self.package.write_text(
            package_rbxmx(
                self.source.decode("utf-8"),
                package_name="StudioMCPv2Candidate",
            ),
            encoding="utf-8",
        )
        self.package_bytes = self.package.read_bytes()
        self.package_sha256 = _sha256(self.package_bytes)
        self.source_sha256 = _sha256(self.source)
        self.executable = (
            self.root
            / "RobloxStudio.app"
            / "Contents"
            / "MacOS"
            / "RobloxStudio"
        )
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"qualification-studio-executable")
        self.executable.chmod(0o755)
        self.executable = self.executable.resolve(strict=True)
        self.executable_sha256 = _sha256(self.executable.read_bytes())
        self.identity = {
            "bundle_id": "com.roblox.RobloxStudio",
            "bundle_path": str(self.executable.parent.parent.parent),
            "bundle_short_version": "1.2.3",
            "bundle_version": "123",
            "executable_path": str(self.executable),
            "executable_sha256": self.executable_sha256,
            "info_plist_sha256": "f" * 64,
            "signature_identifier": "com.roblox.RobloxStudio",
            "team_identifier": "ABCDEFGHIJ",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _process_result(
        self,
        output: bytes,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        **changes: object,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "launch_error": None,
            "lifecycle_error": None,
            "log_limit_exceeded": False,
            "returncode": 1,
            "stderr": stderr,
            "stderr_size": len(stderr),
            "stderr_truncated": False,
            "stdout": stdout,
            "stdout_size": len(stdout),
            "stdout_truncated": False,
            "studio_output": output,
            "studio_output_size": len(output),
            "studio_output_truncated": False,
            "timed_out": False,
        }
        result.update(changes)
        return result

    def _expected_output(self, nonce: str = "12" * 16) -> bytes:
        sentinel = (
            "STUDIO_MCP_V2_NATIVE_COMPILE_PREFIX_OK:"
            + nonce
            + ":"
            + self.package_sha256
            + ":"
            + self.source_sha256
        )
        return (
            sentinel
            + "\ncompile-only.luau:42: "
            + EXPECTED_MAIN_ASSERTION_MESSAGE
            + "\n"
        ).encode("utf-8")

    def _proof_patches(
        self,
        process_result: dict[str, object],
        *,
        running: object = None,
    ) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch(
                "release_tools.native_compile.inspect_studio_identity",
                return_value=dict(self.identity),
            )
        )
        stack.enter_context(
            mock.patch(
                "release_tools.native_compile._running_studio_processes",
                side_effect=running if running is not None else [[], []],
            )
        )
        stack.enter_context(
            mock.patch(
                "release_tools.native_compile._run_native_studio",
                return_value=process_result,
            )
        )
        stack.enter_context(
            mock.patch(
                "release_tools.native_compile.secrets.token_hex",
                return_value="12" * 16,
            )
        )
        return stack

    def _prove(self, receipt: Path, **changes: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "expected_package_sha256": self.package_sha256,
            "expected_source_sha256": self.source_sha256,
            "receipt_path": receipt,
            "expected_studio_executable_sha256": self.executable_sha256,
            "studio_executable": self.executable,
            "timeout_seconds": 30,
            "temporary_parent": self.root,
        }
        arguments.update(changes)
        return prove_native_studio_compilation(self.package, **arguments)

    def _success_receipt(self, name: str = "validated.json") -> Path:
        receipt = self.root / name
        with self._proof_patches(
            self._process_result(self._expected_output())
        ):
            self._prove(receipt)
        return receipt

    def test_extracts_exact_utf8_cdata_source_from_strict_renderer_shape(
        self,
    ) -> None:
        package, source = extract_exact_main_source(self.package)
        self.assertEqual(package, self.package_bytes)
        self.assertEqual(source, self.source)
        self.assertEqual(_sha256(source), self.source_sha256)

    def test_package_symlink_is_rejected(self) -> None:
        symlink = self.root / "candidate-link.rbxmx"
        symlink.symlink_to(self.package)
        with self.assertRaisesRegex(NativeCompileError, "non-symlink"):
            extract_exact_main_source(symlink)

    def test_package_doctype_and_property_shape_drift_are_rejected(self) -> None:
        doctype = self.root / "doctype.rbxmx"
        doctype.write_bytes(
            self.package_bytes.replace(
                b"<roblox ",
                b"<!DOCTYPE roblox><roblox ",
                1,
            )
        )
        with self.assertRaisesRegex(NativeCompileError, "DTD"):
            extract_exact_main_source(doctype)

        drift = self.root / "drift.rbxmx"
        drift.write_bytes(
            self.package_bytes.replace(
                b'<BinaryString name="Tags"></BinaryString>\n'
                b"\t\t</Properties>",
                b'<BinaryString name="Unexpected"></BinaryString>\n'
                b'<BinaryString name="Tags"></BinaryString>\n'
                b"\t\t</Properties>",
                1,
            )
        )
        with self.assertRaisesRegex(NativeCompileError, "properties drifted"):
            extract_exact_main_source(drift)

    def test_script_must_be_the_folder_child(self) -> None:
        sibling = self.root / "sibling.rbxmx"
        value = self.package_bytes.replace(
            b'\t\t<Item class="Script"',
            b'\t</Item>\n\t<Item class="Script"',
            1,
        ).replace(
            b"\t\t</Item>\n\t</Item>\n</roblox>",
            b"\t</Item>\n</roblox>",
            1,
        )
        sibling.write_bytes(value)
        with self.assertRaisesRegex(NativeCompileError, "root structure"):
            extract_exact_main_source(sibling)

    def test_guard_contract_requires_unique_ordered_early_assertion(
        self,
    ) -> None:
        contract = validate_candidate_guard_contract(self.source)
        self.assertEqual(
            contract["assertion_message"],
            EXPECTED_MAIN_ASSERTION_MESSAGE,
        )
        self.assertLess(contract["assertion_line"], contract["config_line"])
        with self.assertRaisesRegex(NativeCompileError, "contract drifted"):
            validate_candidate_guard_contract(
                self.source.replace(
                    EXPECTED_MAIN_ASSERTION.encode("utf-8"),
                    b'assert(plugin ~= nil, "changed")',
                )
            )
        config = b"local CONFIG = table.freeze({"
        connect = b"local function connect("
        reordered = (
            self.source.replace(config, b"__CONFIG_MARKER__", 1)
            .replace(connect, config, 1)
            .replace(b"__CONFIG_MARKER__", connect, 1)
        )
        with self.assertRaisesRegex(NativeCompileError, "ordering drifted"):
            validate_candidate_guard_contract(reordered)

    def test_identity_is_hash_signature_bundle_and_team_bound(self) -> None:
        app = self.root / "identity-valid" / "RobloxStudio.app"
        executable = app / "Contents" / "MacOS" / "RobloxStudio"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"signed-studio")
        executable.chmod(0o755)
        info = app / "Contents" / "Info.plist"
        info.write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.roblox.RobloxStudio",
                    "CFBundleShortVersionString": "1.2.3",
                    "CFBundleVersion": "123",
                }
            )
        )
        expected = _sha256(executable.read_bytes())
        verified = subprocess.CompletedProcess([], 0, b"", b"valid\n")
        identity = subprocess.CompletedProcess(
            [],
            0,
            b"",
            b"Identifier=com.roblox.RobloxStudio\n"
            b"TeamIdentifier=ABCDEFGHIJ\n",
        )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch(
                "release_tools.native_compile._bounded_completed_process",
                side_effect=[verified, identity],
            ) as commands,
        ):
            result = inspect_studio_identity(
                executable,
                expected_executable_sha256=expected,
            )
        self.assertEqual(result["executable_sha256"], expected)
        self.assertEqual(result["team_identifier"], "ABCDEFGHIJ")
        self.assertIn("--strict", commands.call_args_list[0].args[0])

    def test_identity_fails_closed_on_strict_codesign_failure(self) -> None:
        app = self.root / "identity-invalid" / "RobloxStudio.app"
        executable = app / "Contents" / "MacOS" / "RobloxStudio"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"studio")
        executable.chmod(0o755)
        (app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.roblox.RobloxStudio",
                    "CFBundleShortVersionString": "1",
                    "CFBundleVersion": "1",
                }
            )
        )
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch(
                "release_tools.native_compile._bounded_completed_process",
                return_value=subprocess.CompletedProcess([], 1, b"", b"bad"),
            ),
        ):
            with self.assertRaisesRegex(
                NativeCompileError,
                "signature verification failed",
            ):
                inspect_studio_identity(
                    executable,
                    expected_executable_sha256=_sha256(b"studio"),
                )

    def test_success_receipt_binds_exact_artifacts_and_expected_assertion(
        self,
    ) -> None:
        receipt = self.root / "native-proof.json"
        process = self._process_result(self._expected_output())
        with self._proof_patches(process):
            result = self._prove(receipt)
        self.assertTrue(result["ok"])
        self.assertEqual(result["package"]["sha256"], self.package_sha256)
        self.assertEqual(
            result["package"]["source_sha256"],
            self.source_sha256,
        )
        self.assertEqual(
            result["studio"]["executable_sha256"],
            self.executable_sha256,
        )
        self.assertTrue(result["runner"]["main_chunk_not_wrapped"])
        self.assertTrue(
            result["runner"]["candidate_source_exact_after_prefix"]
        )
        self.assertFalse(
            result["runner"]["prefix_has_local_declarations"]
        )
        self.assertEqual(
            result["observations"]["prefix_witness_count_in_output"],
            1,
        )
        self.assertEqual(
            result["observations"]["terminal_assertion_count_in_output"],
            1,
        )
        self.assertEqual(
            json.loads(receipt.read_text(encoding="utf-8"))["ok"],
            True,
        )
        for path in (
            receipt,
            self.root / "native-proof.studio-stdout.log",
            self.root / "native-proof.studio-stderr.log",
            self.root / "native-proof.studio-output.log",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_receipt_validator_rehashes_every_bound_identity(self) -> None:
        receipt = self._success_receipt()
        with mock.patch(
            "release_tools.native_compile.inspect_studio_identity",
            return_value=dict(self.identity),
        ) as identity:
            result = validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["runner"]["candidate_source_sha256"],
            self.source_sha256,
        )
        self.assertEqual(
            identity.call_args.kwargs["expected_executable_sha256"],
            self.executable_sha256,
        )

    def test_receipt_validator_rejects_schema_log_and_path_tampering(
        self,
    ) -> None:
        receipt = self._success_receipt("schema.json")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["unexpected"] = True
        receipt.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        with self.assertRaisesRegex(NativeCompileError, "schema drifted"):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )

        receipt = self._success_receipt("log.json")
        output = self.root / "log.studio-output.log"
        output.write_bytes(output.read_bytes() + b"tampered")
        output.chmod(0o600)
        with self.assertRaisesRegex(NativeCompileError, "metadata does not match"):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )

        receipt = self._success_receipt("path.json")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["logs"]["studio_output"]["path"] = str(
            self.root / "elsewhere.log"
        )
        receipt.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        with self.assertRaisesRegex(NativeCompileError, "path drifted"):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )

    def test_receipt_validator_rejects_package_and_studio_drift(self) -> None:
        receipt = self._success_receipt("artifact.json")
        original_package = self.package.read_bytes()
        self.package.write_bytes(original_package + b"\n")
        with self.assertRaisesRegex(NativeCompileError, "artifact input"):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )
        self.package.write_bytes(original_package)

        original_executable = self.executable.read_bytes()
        self.executable.write_bytes(original_executable + b"drift")
        self.executable.chmod(0o755)
        with (
            mock.patch(
                "release_tools.native_compile.inspect_studio_identity"
            ) as identity,
            self.assertRaisesRegex(NativeCompileError, "bytes drifted"),
        ):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )
        identity.assert_not_called()

    def test_receipt_validator_rejects_signed_identity_and_mode_drift(
        self,
    ) -> None:
        receipt = self._success_receipt("identity.json")
        changed_identity = dict(self.identity)
        changed_identity["team_identifier"] = "KLMNOPQRST"
        with (
            mock.patch(
                "release_tools.native_compile.inspect_studio_identity",
                return_value=changed_identity,
            ),
            self.assertRaisesRegex(NativeCompileError, "identity drifted"),
        ):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )

        receipt.chmod(0o644)
        with self.assertRaisesRegex(NativeCompileError, "mode 0600"):
            validate_native_compile_receipt(
                receipt,
                package_path=self.package,
                expected_package_sha256=self.package_sha256,
                expected_source_sha256=self.source_sha256,
                studio_executable=self.executable,
            )

    def test_command_uses_no_place_and_disables_user_plugins(self) -> None:
        receipt = self.root / "command-proof.json"
        process = self._process_result(self._expected_output())
        with (
            self._proof_patches(process),
            mock.patch(
                "release_tools.native_compile._run_native_studio",
                return_value=process,
            ) as runner,
        ):
            result = self._prove(receipt)
        command = runner.call_args.args[0]
        self.assertEqual(command[1:3], ["--task", "RunScript"])
        self.assertIn("-disableloaduserplugins", command)
        self.assertIn("--quitAfterExecution", command)
        self.assertNotIn("--place", command)
        self.assertTrue(result["command_contract"]["no_place_argument"])

    def test_compile_error_writes_failure_receipt_then_raises(self) -> None:
        receipt = self.root / "compile-failure.json"
        output = self._expected_output() + b"Out of local registers\n"
        with self._proof_patches(self._process_result(output)):
            with self.assertRaisesRegex(NativeCompileError, "see"):
                self._prove(receipt)
        result = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertIn(
            "native_compile_error_observed",
            result["failure_reasons"],
        )
        self.assertEqual(
            result["observations"]["compile_error_markers"],
            ["out of local registers"],
        )

    def test_timeout_user_plugin_and_post_process_each_fail_closed(self) -> None:
        cases = (
            (
                self._process_result(
                    b"",
                    launch_error="not executable",
                    returncode=None,
                ),
                [[], []],
                "studio_launch_failed",
            ),
            (
                self._process_result(
                    b"",
                    timed_out=True,
                    lifecycle_error="terminated",
                    returncode=-15,
                ),
                [[], []],
                "studio_timed_out",
            ),
            (
                self._process_result(
                    self._expected_output(),
                    stderr=b"Loading user_bad.rbxmx\n",
                ),
                [[], []],
                "user_plugin_loaded",
            ),
            (
                self._process_result(self._expected_output()),
                [[], [991]],
                "studio_process_remained_running",
            ),
            (
                self._process_result(self._expected_output()),
                [[], NativeCompileError("ps unavailable")],
                "post_process_audit_failed",
            ),
        )
        for index, (process, running, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                receipt = self.root / f"failure-{index}.json"
                with self._proof_patches(process, running=running):
                    with self.assertRaises(NativeCompileError):
                        self._prove(receipt)
                result = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertIn(reason, result["failure_reasons"])

    def test_wrong_hash_active_studio_and_evidence_collision_prevent_launch(
        self,
    ) -> None:
        receipt = self.root / "never.json"
        with mock.patch(
            "release_tools.native_compile.inspect_studio_identity"
        ) as identity:
            with self.assertRaisesRegex(NativeCompileError, "package SHA"):
                self._prove(
                    receipt,
                    expected_package_sha256="0" * 64,
                )
            identity.assert_not_called()

        with (
            mock.patch(
                "release_tools.native_compile.inspect_studio_identity",
                return_value=dict(self.identity),
            ),
            mock.patch(
                "release_tools.native_compile._running_studio_processes",
                return_value=[42],
            ),
            mock.patch(
                "release_tools.native_compile._run_native_studio"
            ) as runner,
        ):
            with self.assertRaisesRegex(NativeCompileError, "no running"):
                self._prove(receipt)
            runner.assert_not_called()

        collision = self.root / "collision.studio-output.log"
        collision.write_bytes(b"owned")
        with mock.patch(
            "release_tools.native_compile.inspect_studio_identity"
        ) as identity:
            with self.assertRaisesRegex(NativeCompileError, "already exists"):
                self._prove(self.root / "collision.json")
            identity.assert_not_called()

    def test_prefix_has_no_locals_and_runner_keeps_source_byte_exact(self) -> None:
        prefix, sentinel = native_compile._guard_prefix(
            nonce="ab" * 16,
            package_sha256=self.package_sha256,
            source_sha256=self.source_sha256,
        )
        self.assertNotRegex(prefix.decode("utf-8"), r"(?m)^\s*local\b")
        self.assertIn(sentinel.encode("ascii"), prefix)
        runner = prefix + self.source
        self.assertEqual(runner[len(prefix) :], self.source)

    def test_process_environment_is_an_allowlist(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/safe/home",
                "PATH": "/usr/bin",
                "STUDIO_MCP_V2_STUDIO_TOKEN": "secret",
                "HTTPS_PROXY": "http://proxy.invalid",
            },
            clear=True,
        ):
            environment = native_compile._safe_process_environment()
        self.assertEqual(
            environment,
            {"HOME": "/safe/home", "PATH": "/usr/bin"},
        )

    def test_native_process_runner_uses_disk_backed_bounded_evidence(
        self,
    ) -> None:
        working = self.root / "process"
        working.mkdir()
        output = working / "studio-output.log"
        command = [
            "/mock/RobloxStudio",
            "--task",
            "RunScript",
            "-disableloaduserplugins",
            "--runScriptFile",
            str(working / "compile-only.luau"),
            "--outputFile",
            str(output),
            "--quitAfterExecution",
        ]
        captured: dict[str, object] = {}

        class FakeProcess:
            pid = 99
            returncode = 0

            def __init__(self, arguments: list[str], **kwargs: object) -> None:
                captured["arguments"] = arguments
                captured["kwargs"] = kwargs
                stdout = kwargs["stdout"]
                stderr = kwargs["stderr"]
                stdout.write(b"native stdout")
                stdout.flush()
                stderr.write(b"native stderr")
                stderr.flush()
                Path(arguments[arguments.index("--outputFile") + 1]).write_bytes(
                    b"native output"
                )

            def poll(self) -> int:
                return 0

        with (
            mock.patch.dict(
                os.environ,
                {
                    "HOME": "/safe/home",
                    "PATH": "/usr/bin",
                    "STUDIO_MCP_V2_STUDIO_TOKEN": "secret",
                },
                clear=True,
            ),
            mock.patch(
                "release_tools.native_compile.subprocess.Popen",
                FakeProcess,
            ),
        ):
            result = native_compile._run_native_studio(
                command,
                working_directory=working,
                studio_output_path=output,
                timeout_seconds=30,
            )
        self.assertEqual(result["stdout"], b"native stdout")
        self.assertEqual(result["stderr"], b"native stderr")
        self.assertEqual(result["studio_output"], b"native output")
        self.assertEqual(captured["arguments"], command)
        environment = captured["kwargs"]["env"]
        self.assertEqual(
            environment,
            {"HOME": "/safe/home", "PATH": "/usr/bin"},
        )
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(captured["kwargs"]["close_fds"])

    def test_cli_has_required_identity_pin_and_stable_json_outcomes(self) -> None:
        parser = native_studio_compile_smoke.build_parser()
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(
                [
                    "--package",
                    str(self.package),
                    "--expected-package-sha256",
                    self.package_sha256,
                    "--expected-source-sha256",
                    self.source_sha256,
                    "--receipt",
                    str(self.root / "cli.json"),
                ]
            )

        expected = {"format": "proof", "ok": True}
        stdout = io.StringIO()
        with (
            mock.patch(
                "scripts.native_studio_compile_smoke."
                "prove_native_studio_compilation",
                return_value=expected,
            ) as prove,
            contextlib.redirect_stdout(stdout),
        ):
            returncode = native_studio_compile_smoke.main(
                [
                    "--package",
                    str(self.package),
                    "--expected-package-sha256",
                    self.package_sha256,
                    "--expected-source-sha256",
                    self.source_sha256,
                    "--receipt",
                    str(self.root / "cli.json"),
                    "--expected-studio-executable-sha256",
                    self.executable_sha256,
                ]
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(
            prove.call_args.kwargs["expected_studio_executable_sha256"],
            self.executable_sha256,
        )

        stdout = io.StringIO()
        with (
            mock.patch(
                "scripts.native_studio_compile_smoke."
                "prove_native_studio_compilation",
                side_effect=NativeCompileError("strict failure"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            returncode = native_studio_compile_smoke.main(
                [
                    "--package",
                    str(self.package),
                    "--expected-package-sha256",
                    self.package_sha256,
                    "--expected-source-sha256",
                    self.source_sha256,
                    "--receipt",
                    str(self.root / "cli-failed.json"),
                    "--expected-studio-executable-sha256",
                    self.executable_sha256,
                ]
            )
        self.assertEqual(returncode, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "strict failure")


if __name__ == "__main__":
    unittest.main()
