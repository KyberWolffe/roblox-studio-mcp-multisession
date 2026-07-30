from __future__ import annotations

import argparse
import copy
import inspect
import json
import os
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import candidate_readonly_gate as gate
from studio_mcp_v2.auth import AuthorizationPolicy, Principal
from studio_mcp_v2.catalog import ToolCatalog
from release_tools import builder

from .helpers import PROJECT_ROOT


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)
STUDIO_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.calls = []
        self.jobs = []

    def call(self, tool_name, arguments, request_id):
        self.calls.append(
            (tool_name, copy.deepcopy(arguments), request_id)
        )
        return {"called": tool_name}

    def start_job(self, arguments):
        self.jobs.append(copy.deepcopy(arguments))
        return {"started": arguments["tool_name"]}

    def get_job(self, arguments):
        self.jobs.append(copy.deepcopy(arguments))
        return {"selected": arguments["job_id"]}


class CandidateReadOnlyGateTests(unittest.TestCase):
    def test_public_scope_and_catalog_mapping_are_exact(self) -> None:
        self.assertEqual(
            {
                "studio_get_state_v2": "studio_get_state",
                "studio_list_tree_v2": "studio_list_tree",
                "studio_search_scripts_v2": (
                    "studio_search_scripts"
                ),
                "studio_grep_scripts_v2": "studio_grep_scripts",
                "studio_inspect_instance_v2": (
                    "studio_inspect_instance"
                ),
            },
            gate.PUBLIC_READ_ONLY_TO_REMOTE,
        )
        for public_name, remote_name in (
            gate.PUBLIC_READ_ONLY_TO_REMOTE.items()
        ):
            self.assertTrue(public_name.endswith("_v2"))
            self.assertFalse(remote_name.endswith("_v2"))
            self.assertNotIn(
                remote_name, gate.ALLOWED_PUBLIC_SCOPES
            )

        catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.assertEqual(
            gate.PUBLIC_READ_ONLY_TO_REMOTE,
            gate.audit_catalog_contract(catalog),
        )
        exposed = {
            tool["name"]: tool
            for tool in catalog.tools_for_mcp()
        }
        for public_name in gate.PUBLIC_READ_ONLY_TOOLS:
            schema = exposed[public_name]["inputSchema"]
            self.assertIn("studio_id", schema["required"])
            self.assertEqual(
                "uuid",
                schema["properties"]["studio_id"]["format"],
            )

    def test_mapping_drift_fails_closed(self) -> None:
        catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        original = gate.PUBLIC_READ_ONLY_TO_REMOTE
        drifted = dict(original)
        drifted["studio_get_state_v2"] = "studio_list_tree"
        with mock.patch.object(
            gate, "PUBLIC_READ_ONLY_TO_REMOTE", drifted
        ):
            with self.assertRaisesRegex(
                RuntimeError, "mapping drifted"
            ):
                gate.audit_catalog_contract(catalog)

    def test_schema_and_annotation_drift_fail_closed(self) -> None:
        payload = json.loads(
            DURABLE_CATALOG.read_text(encoding="utf-8")
        )
        tools = payload["tools"]
        selected = next(
            item
            for item in tools
            if item["name"] == "studio_get_state"
        )
        selected["annotations"]["readOnlyHint"] = False
        with self.assertRaisesRegex(
            RuntimeError, "read-only annotation"
        ):
            gate.audit_catalog_contract(ToolCatalog(tools))

        payload = json.loads(
            DURABLE_CATALOG.read_text(encoding="utf-8")
        )
        tools = payload["tools"]
        selected = next(
            item
            for item in tools
            if item["name"] == "studio_get_state"
        )
        selected["inputSchema"]["properties"]["studio_id"] = {
            "type": "string"
        }
        with self.assertRaisesRegex(
            RuntimeError, "remote schema contains routing"
        ):
            gate.audit_catalog_contract(ToolCatalog(tools))

    def test_runtime_authorizes_only_public_names(self) -> None:
        runtime = gate.build_runtime(
            41_337, Path("/isolated/tool-catalog.json")
        )
        self.assertEqual(
            sorted(gate.ALLOWED_PUBLIC_SCOPES),
            runtime["allowed_tools"],
        )
        self.assertEqual("127.0.0.1", runtime["host"])
        for name in runtime["allowed_tools"]:
            self.assertTrue(name.endswith("_v2"))
        for remote_name in (
            gate.PUBLIC_READ_ONLY_TO_REMOTE.values()
        ):
            self.assertNotIn(remote_name, runtime["allowed_tools"])
        principal = Principal.create(
            "candidate-read-only",
            allowed_tools=runtime["allowed_tools"],
        )
        policy = AuthorizationPolicy()
        for public_name in gate.ALLOWED_PUBLIC_SCOPES:
            self.assertTrue(
                policy.can_use_tool(principal, public_name)
            )
        for denied_name in {
            *gate.PUBLIC_READ_ONLY_TO_REMOTE.values(),
            "studio_multi_edit_v2",
            "cancel_studio_job_v2",
            "start_stop_play_v2",
        }:
            self.assertFalse(
                policy.can_use_tool(principal, denied_name)
            )

    def test_direct_call_passes_public_name_unchanged(self) -> None:
        client = FakeClient()
        with mock.patch.object(
            gate, "client_for", return_value=(None, client)
        ):
            result = gate.call(
                Path("/unused"),
                "studio_list_tree_v2",
                STUDIO_ID,
                '{"root_path":[],"page_size":1}',
            )
        self.assertEqual(
            {"called": "studio_list_tree_v2"}, result
        )
        tool_name, arguments, request_id = client.calls[0]
        self.assertEqual("studio_list_tree_v2", tool_name)
        self.assertEqual(STUDIO_ID, arguments["studio_id"])
        self.assertEqual([], arguments["root_path"])
        self.assertEqual(1, arguments["page_size"])
        self.assertEqual(36, len(request_id))

    def test_job_passes_public_name_and_outer_target(self) -> None:
        client = FakeClient()
        with mock.patch.object(
            gate, "client_for", return_value=(None, client)
        ):
            result = gate.start_job(
                Path("/unused"),
                "studio_inspect_instance_v2",
                STUDIO_ID,
                '{"path":["Workspace"]}',
                30_000,
            )
        self.assertEqual(
            {"started": "studio_inspect_instance_v2"}, result
        )
        self.assertEqual(
            {
                "studio_id": STUDIO_ID,
                "tool_name": "studio_inspect_instance_v2",
                "tool_arguments": {"path": ["Workspace"]},
                "timeout_ms": 30_000,
            },
            client.jobs[0],
        )

    def test_get_job_uses_only_explicit_outer_target(self) -> None:
        client = FakeClient()
        job_id = "00000000-0000-4000-8000-000000000002"
        with mock.patch.object(
            gate, "client_for", return_value=(None, client)
        ):
            result = gate.get_job(
                Path("/unused"), STUDIO_ID, job_id
            )
        self.assertEqual({"selected": job_id}, result)
        self.assertEqual(
            {"studio_id": STUDIO_ID, "job_id": job_id},
            client.jobs[0],
        )

    def test_remote_names_and_ambiguous_targets_fail_locally(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, "public read-only"
        ):
            gate.validate_public_tool_name("studio_get_state")
        with self.assertRaisesRegex(
            RuntimeError, "caller-supplied studio_id"
        ):
            gate.parse_tool_arguments(
                '{"studio_id":"'
                + STUDIO_ID
                + '"}'
            )
        with self.assertRaisesRegex(
            RuntimeError, "caller-supplied studio_id"
        ):
            gate.parse_tool_arguments(
                '{"filter":{"studio_id":"'
                + STUDIO_ID
                + '"}}'
            )
        with self.assertRaisesRegex(
            RuntimeError, "duplicate JSON keys"
        ):
            gate.parse_tool_arguments(
                '{"page_size":1,"page_size":2}'
            )
        with self.assertRaisesRegex(
            RuntimeError, "non-finite"
        ):
            gate.parse_tool_arguments('{"page_size":NaN}')

        client = FakeClient()
        with mock.patch.object(
            gate, "client_for", return_value=(None, client)
        ) as mocked_client:
            with self.assertRaisesRegex(
                RuntimeError, "canonical lowercase UUID"
            ):
                gate.call(
                    Path("/unused"),
                    "studio_get_state_v2",
                    STUDIO_ID.upper(),
                    "{}",
                )
            with self.assertRaisesRegex(
                RuntimeError, "between 1 and 120000"
            ):
                gate.start_job(
                    Path("/unused"),
                    "studio_get_state_v2",
                    STUDIO_ID,
                    "{}",
                    0,
                )
        mocked_client.assert_not_called()

    def test_state_file_requires_public_scope_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            payload = root / "archive" / "payload"
            support = root / "support"
            payload.mkdir(parents=True)
            support.mkdir()
            state = {
                "format": gate.GATE_STATE_FORMAT,
                "version": "future-candidate",
                "payload_root": str(payload),
                "support_root": str(support),
                "plugin_path": str(
                    root
                    / "StudioMCPv2CandidateReadOnly.rbxmx"
                ),
                "plugin_sha256": "0" * 64,
                "plugin_source_sha256": "2" * 64,
                "native_compile_receipt_path": str(
                    root / gate.NATIVE_RECEIPT_FILENAME
                ),
                "release_manifest_sha256": "3" * 64,
                "cleanup_identity_sha256": "8" * 64,
                "durable_catalog_file_sha256": "1" * 64,
                "runtime_config_sha256": "4" * 64,
                "secrets_config_sha256": "5" * 64,
                "upstream_catalog_file_sha256": "6" * 64,
                "upstream_compatibility_file_sha256": "7" * 64,
                "port": 44_757,
                "run_id": "2" * 32,
                "allowed_tools": sorted(
                    gate.ALLOWED_PUBLIC_SCOPES
                ),
                "public_to_remote": (
                    gate.PUBLIC_READ_ONLY_TO_REMOTE
                ),
            }
            (root / gate.STATE_FILENAME).write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            os.chmod(root / gate.STATE_FILENAME, 0o600)
            drifted = copy.deepcopy(state)
            drifted["allowed_tools"] = [
                "studio_get_state"
            ]
            (root / gate.STATE_FILENAME).write_text(
                json.dumps(drifted),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError, "public tool scope drifted"
            ):
                gate.load_candidate(root)

            drifted = copy.deepcopy(state)
            drifted["public_to_remote"][
                "studio_get_state_v2"
            ] = "studio_list_tree"
            (root / gate.STATE_FILENAME).write_text(
                json.dumps(drifted),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError, "mapping audit drifted"
            ):
                gate.load_candidate(root)

    def test_operational_loader_has_no_unqualified_bypass_and_stop_is_separate(
        self,
    ) -> None:
        self.assertNotIn(
            "require_native",
            inspect.signature(
                gate.load_candidate
            ).parameters,
        )

        failure = {
            "ok": False,
            "error": "native receipt is missing",
        }
        with (
            mock.patch.object(
                gate,
                "native_qualification_status",
                return_value=failure,
            ),
            self.assertRaisesRegex(
                RuntimeError, "not qualified"
            ),
        ):
            gate.require_native_qualification(
                Path("/gate"),
                {},
                Path("/gate/plugin.rbxmx"),
                Path("/gate/receipt.json"),
            )

        stopped = {"ok": True, "stopped": True}
        with mock.patch.object(
            gate,
            "_candidate_lifecycle_action",
            return_value=stopped,
        ) as lifecycle:
            self.assertEqual(stopped, gate.stop(Path("/gate")))
        lifecycle.assert_called_once_with(
            Path("/gate"), "stop"
        )

    def test_client_creation_always_uses_qualified_loader(
        self,
    ) -> None:
        loaded = (
            {"version": "candidate"},
            "paths",
            type(
                "Config",
                (),
                {"base_url": "http://127.0.0.1:44757"},
            )(),
            type(
                "Secrets",
                (),
                {"client_token": "c" * 48},
            )(),
            FakeClient,
            object,
            object,
            object,
            {"ok": True},
        )
        with mock.patch.object(
            gate, "load_candidate", return_value=loaded
        ) as loader:
            observed, client = gate.client_for(Path("/gate"))
        self.assertIs(observed, loaded)
        self.assertIsInstance(client, FakeClient)
        loader.assert_called_once_with(Path("/gate"))

    def test_cleanup_runtime_does_not_require_plugin_catalog_or_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            support = root / "support"
            config = support / "config"
            for directory in (
                support,
                config,
                support / "run",
                support / "logs",
            ):
                directory.mkdir()
                os.chmod(directory, 0o700)
            runtime = gate.build_runtime(
                44_757, config / "tool-catalog.json"
            )
            (config / "runtime.json").write_text(
                json.dumps(runtime),
                encoding="utf-8",
            )
            os.chmod(config / "runtime.json", 0o644)
            (config / "secrets.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "client_token": "c" * 48,
                        "studio_token": "s" * 48,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(config / "secrets.json", 0o600)
            cleanup_identity = {
                "format": gate.CLEANUP_IDENTITY_FORMAT,
                "version": "0.4.0-rc.5",
                "port": 44_757,
                "run_id": "a" * 32,
                "release_manifest_sha256": "b" * 64,
                "durable_catalog_file_sha256": "c" * 64,
                "runtime_config_sha256": gate.sha256_bytes(
                    (config / "runtime.json").read_bytes()
                ),
                "secrets_config_sha256": gate.sha256_bytes(
                    (config / "secrets.json").read_bytes()
                ),
            }
            (root / gate.CLEANUP_IDENTITY_FILENAME).write_text(
                json.dumps(
                    cleanup_identity,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(
                root / gate.CLEANUP_IDENTITY_FILENAME,
                0o600,
            )
            (
                observed_support,
                observed_runtime,
                observed_secrets,
                identity_bytes,
                observed_identity,
            ) = gate._cleanup_runtime_records(root)
            self.assertEqual(support.resolve(), observed_support)
            self.assertEqual(44_757, observed_runtime["port"])
            self.assertEqual(
                "c" * 48, observed_secrets["client_token"]
            )
            self.assertEqual(
                cleanup_identity, observed_identity
            )
            self.assertEqual(
                gate.sha256_bytes(identity_bytes),
                gate.sha256_bytes(
                    (
                        root
                        / gate.CLEANUP_IDENTITY_FILENAME
                    ).read_bytes()
                ),
            )
            self.assertFalse(
                (config / "tool-catalog.json").exists()
            )
            self.assertFalse(
                (root / gate.STATE_FILENAME).exists()
            )
            self.assertFalse(
                (
                    root
                    / "StudioMCPv2CandidateReadOnly.rbxmx"
                ).exists()
            )
            (config / "secrets.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "client_token": "x" * 48,
                        "studio_token": "y" * 48,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(config / "secrets.json", 0o600)
            with self.assertRaisesRegex(
                RuntimeError, "cleanup identity drifted"
            ):
                gate._cleanup_runtime_records(root)

    def test_private_record_rejects_symlink_mode_and_duplicate_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "record.json"
            record.write_text('{"ok":true}', encoding="utf-8")
            os.chmod(record, 0o644)
            with self.assertRaisesRegex(
                RuntimeError, "exact bounded"
            ):
                gate.read_private_record(record, "record")
            os.chmod(record, 0o600)
            linked = root / "linked.json"
            linked.symlink_to(record)
            with self.assertRaisesRegex(
                RuntimeError, "exact bounded"
            ):
                gate.read_private_record(linked, "linked")
            record.write_text(
                '{"same":1,"same":2}', encoding="utf-8"
            )
            with self.assertRaisesRegex(
                RuntimeError, "duplicate JSON keys"
            ):
                gate.read_private_record(record, "record")

    def test_native_revalidation_is_anchored_to_default_studio_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_studio = root / "RobloxStudio"
            default_studio.write_bytes(b"studio")
            plugin = root / "candidate.rbxmx"
            receipt = root / gate.NATIVE_RECEIPT_FILENAME
            state_bytes = b'{"state":true}\n'
            receipt_bytes = b'{"receipt":true}\n'
            state = {
                "plugin_sha256": "a" * 64,
                "plugin_source_sha256": "b" * 64,
            }
            qualification = {
                "format": gate.NATIVE_QUALIFICATION_FORMAT,
                "gate_state_sha256": gate.sha256_bytes(
                    state_bytes
                ),
                "plugin_sha256": "a" * 64,
                "plugin_source_sha256": "b" * 64,
                "receipt_path": str(receipt),
                "receipt_sha256": gate.sha256_bytes(
                    receipt_bytes
                ),
                "studio_executable_path": str(
                    root / "redirected-studio"
                ),
                "studio_executable_sha256": "c" * 64,
            }
            with (
                mock.patch.object(
                    gate,
                    "read_private_record",
                    side_effect=[
                        (state_bytes, state),
                        (b"qualification", qualification),
                        (receipt_bytes, {"ok": True}),
                    ],
                ),
                mock.patch.object(
                    gate,
                    "DEFAULT_STUDIO_EXECUTABLE",
                    default_studio,
                ),
                mock.patch.object(
                    gate, "validate_native_compile_receipt"
                ) as validator,
            ):
                result = gate.native_qualification_status(
                    root, state, plugin, receipt
                )
            self.assertFalse(result["ok"])
            self.assertIn("path anchor drifted", result["error"])
            validator.assert_not_called()

    def test_status_does_not_authenticate_without_qualification(
        self,
    ) -> None:
        local = {
            "running": False,
            "condition": "stopped",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(
                    gate,
                    "_candidate_lifecycle_action",
                    return_value=local,
                ),
                mock.patch.object(
                    gate,
                    "read_private_record",
                    side_effect=RuntimeError("proof missing"),
                ),
                mock.patch.object(
                    gate, "load_candidate"
                ) as operational_loader,
            ):
                result = gate.status(root)
        self.assertFalse(result["ok"])
        self.assertEqual(local, result["local"])
        self.assertIsNone(result["authenticated"])
        self.assertIn(
            "proof missing",
            result["native_qualification"]["error"],
        )
        operational_loader.assert_not_called()

    def test_release_payload_manifest_rejects_extra_file_and_symlink_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            built = builder.build_release(
                PROJECT_ROOT, root / "dist"
            )
            extracted = root / "extracted"
            extracted.mkdir(mode=0o700)
            with tarfile.open(built.archive, "r:gz") as package:
                package.extractall(extracted)
            payload, manifest_sha256 = (
                gate._validated_candidate_release(
                    extracted,
                    expected_version="0.4.0-rc.5",
                )
            )
            self.assertTrue(payload.is_dir())
            self.assertRegex(manifest_sha256, r"^[0-9a-f]{64}$")
            extra = payload / "unmanifested.py"
            extra.write_text("raise RuntimeError\n", encoding="utf-8")
            os.chmod(extra, 0o644)
            with self.assertRaisesRegex(
                RuntimeError, "extracted tree drifted"
            ):
                gate._validated_candidate_release(extracted)

            linked = root / "linked-work-root"
            linked.symlink_to(extracted, target_is_directory=True)
            with self.assertRaisesRegex(
                RuntimeError, "real directory"
            ):
                gate._validated_candidate_release(linked)

    def test_prepare_binds_release_runtime_and_config_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            built = builder.build_release(
                PROJECT_ROOT, root / "dist"
            )
            work_root = root / "candidate"
            work_root.mkdir(mode=0o700)
            with tarfile.open(built.archive, "r:gz") as package:
                package.extractall(work_root)
            payload, manifest_sha256 = (
                gate._validated_candidate_release(
                    work_root,
                    expected_version="0.4.0-rc.5",
                )
            )
            durable_sha256 = gate.sha256_bytes(
                (
                    payload
                    / "config"
                    / "durable-tool-catalog.json"
                ).read_bytes()
            )
            rejected_args = argparse.Namespace(
                work_root=work_root,
                version="0.4.0-rc.5",
                durable_catalog_sha256=durable_sha256,
                release_manifest_sha256="0" * 64,
                port=44_757,
            )
            with (
                mock.patch.object(
                    gate, "_load_isolated_module"
                ) as module_loader,
                self.assertRaisesRegex(
                    RuntimeError,
                    "release manifest bytes drifted",
                ),
            ):
                gate.prepare(rejected_args)
            module_loader.assert_not_called()
            self.assertFalse((work_root / "support").exists())
            state = gate.prepare(
                argparse.Namespace(
                    work_root=work_root,
                    version="0.4.0-rc.5",
                    durable_catalog_sha256=durable_sha256,
                    release_manifest_sha256=manifest_sha256,
                    port=44_757,
                )
            )
            self.assertEqual(
                manifest_sha256,
                state["release_manifest_sha256"],
            )
            for field in (
                "runtime_config_sha256",
                "secrets_config_sha256",
                "upstream_catalog_file_sha256",
                "upstream_compatibility_file_sha256",
            ):
                self.assertRegex(
                    state[field], r"^[0-9a-f]{64}$"
                )
            validated = gate._validated_gate_state(
                work_root, state
            )
            self.assertEqual(
                work_root
                / "StudioMCPv2CandidateReadOnly.rbxmx",
                validated[2],
            )
            with self.assertRaisesRegex(
                RuntimeError, "not qualified"
            ):
                gate.load_candidate(work_root)

    def test_cleanup_broker_receipt_pins_exact_live_instance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            identity_bytes = b'{"identity":true}\n'
            identity = {
                "version": "0.4.0-rc.5",
                "port": 44_757,
                "run_id": "a" * 32,
                "durable_catalog_file_sha256": "b" * 64,
            }
            health = {
                "broker_instance_id": (
                    "123e4567-e89b-42d3-a456-426614174111"
                ),
                "pid": 123,
                "started_at": 1234.5,
                "version": "0.4.0-rc.5",
                "catalog_sha256": "c" * 64,
                "stop_safe": True,
            }
            expected = gate._cleanup_broker_record(
                identity_bytes, identity, health
            )
            self.assertEqual("c" * 64, expected["catalog_sha256"])
            invalid_health = dict(health)
            invalid_health["catalog_sha256"] = "not-a-sha256"
            with self.assertRaisesRegex(
                RuntimeError, "broker health drifted"
            ):
                gate._cleanup_broker_record(
                    identity_bytes, identity, invalid_health
                )
            drifted = dict(expected)
            drifted["broker_instance_id"] = (
                "123e4567-e89b-42d3-a456-426614174222"
            )
            receipt = root / gate.CLEANUP_BROKER_FILENAME
            receipt.write_text(
                json.dumps(
                    drifted, indent=2, sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(receipt, 0o600)
            local = {
                "running": True,
                "record_matches": True,
                "condition": "healthy_idle",
                "broker": health,
            }
            with self.assertRaisesRegex(
                RuntimeError, "receipt drifted"
            ):
                gate._require_cleanup_broker(
                    root,
                    identity_bytes,
                    identity,
                    local,
                )

    def test_absent_cleanup_never_calls_unfenced_lifecycle_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            lifecycle_stop = mock.Mock()
            fake_lifecycle = types.SimpleNamespace(
                InstallPaths=types.SimpleNamespace(
                    for_test=lambda support: "paths"
                ),
                RuntimeConfig=lambda **_kwargs: "config",
                SecretsConfig=lambda **_kwargs: "secrets",
                broker_status=lambda *_args: {
                    "running": False,
                    "condition": "stopped",
                },
                stop_broker=lifecycle_stop,
            )
            fake_package = types.SimpleNamespace(
                __version__="0.4.0-rc.5"
            )
            identity = {
                "version": "0.4.0-rc.5",
            }
            with (
                mock.patch.object(
                    gate,
                    "_cleanup_runtime_records",
                    return_value=(
                        root / "support",
                        {
                            "host": "127.0.0.1",
                            "port": 44_757,
                            "catalog": str(
                                root / "catalog.json"
                            ),
                            "allowed_studios": ["*"],
                            "allowed_tools": sorted(
                                gate.ALLOWED_PUBLIC_SCOPES
                            ),
                            "startup_timeout_seconds": 10.0,
                        },
                        {
                            "client_token": "c" * 48,
                            "studio_token": "s" * 48,
                        },
                        b"identity",
                        identity,
                    ),
                ),
                mock.patch.object(
                    gate,
                    "_load_isolated_package",
                    return_value=(
                        fake_package,
                        {"lifecycle": fake_lifecycle},
                    ),
                ),
                mock.patch.object(
                    gate, "_require_module_from_project"
                ),
            ):
                result = gate._candidate_lifecycle_action(
                    root, "stop"
                )
            self.assertEqual(
                {"running": False, "stopped": False},
                result,
            )
            lifecycle_stop.assert_not_called()

    def test_start_receipt_failure_stops_only_observed_instance(
        self,
    ) -> None:
        broker_instance_id = (
            "123e4567-e89b-42d3-a456-426614174333"
        )
        health = {
            "broker_instance_id": broker_instance_id,
        }
        exact_stop = mock.Mock(
            return_value={
                "running": False,
                "stopped": True,
            }
        )
        loaded = (
            {
                "version": "0.4.0-rc.5",
                "port": 44_757,
                "durable_catalog_file_sha256": "a" * 64,
                "plugin_sha256": "b" * 64,
                "plugin_source_sha256": "c" * 64,
                "allowed_tools": [],
                "public_to_remote": {},
            },
            "paths",
            "config",
            "secrets",
            object,
            object,
            mock.Mock(return_value=health),
            exact_stop,
            {"ok": True},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                mock.patch.object(
                    gate,
                    "load_candidate",
                    return_value=loaded,
                ),
                mock.patch.object(
                    gate,
                    "_pin_cleanup_broker",
                    side_effect=RuntimeError(
                        "receipt write failed"
                    ),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "exact newly observed broker was stopped",
                ),
            ):
                gate.start(root)
        exact_stop.assert_called_once_with(
            "paths",
            "config",
            "secrets",
            expected_broker_instance_id=broker_instance_id,
        )

    def test_isolated_package_loading_ignores_canonical_module_cache(
        self,
    ) -> None:
        canonical = __import__("studio_mcp_v2")
        package, modules = gate._load_isolated_package(
            PROJECT_ROOT / "studio_mcp_v2",
            "gate-test",
            ("frontend", "lifecycle"),
        )
        self.assertIsNot(package, canonical)
        self.assertNotEqual(
            package.__name__, canonical.__name__
        )
        self.assertTrue(
            modules["frontend"].__name__.startswith(
                package.__name__ + "."
            )
        )
        self.assertTrue(
            modules["lifecycle"].__name__.startswith(
                package.__name__ + "."
            )
        )

    def test_status_reports_qualified_authentication_race_as_not_ok(
        self,
    ) -> None:
        local = {
            "running": True,
            "condition": "healthy_idle",
        }
        state = {"version": "candidate"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                mock.patch.object(
                    gate,
                    "_candidate_lifecycle_action",
                    return_value=local,
                ),
                mock.patch.object(
                    gate,
                    "read_private_record",
                    return_value=(b"state", state),
                ),
                mock.patch.object(
                    gate,
                    "_validated_gate_state",
                    return_value=(
                        root / "payload",
                        root / "support",
                        root / "plugin.rbxmx",
                        root / "catalog.json",
                        root / gate.NATIVE_RECEIPT_FILENAME,
                    ),
                ),
                mock.patch.object(
                    gate,
                    "native_qualification_status",
                    return_value={"ok": True},
                ),
                mock.patch.object(
                    gate,
                    "load_candidate",
                    side_effect=RuntimeError(
                        "broker stopped during status"
                    ),
                ),
            ):
                result = gate.status(root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["authenticated"]["ok"])
        self.assertIn(
            "broker stopped during status",
            result["authenticated"]["error"],
        )

    def test_qualification_record_binds_state_receipt_and_studio(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "candidate.rbxmx"
            receipt = root / gate.NATIVE_RECEIPT_FILENAME
            state_bytes = b'{"state":true}\n'
            receipt_bytes = b'{"receipt":true}\n'
            state = {
                "plugin_sha256": "a" * 64,
                "plugin_source_sha256": "b" * 64,
            }
            studio = {
                "executable_path": "/Applications/"
                "RobloxStudio.app/Contents/MacOS/RobloxStudio",
                "executable_sha256": "c" * 64,
            }
            with (
                mock.patch.object(
                    gate,
                    "read_private_record",
                    side_effect=[
                        (state_bytes, state),
                        (receipt_bytes, {"ok": True}),
                    ],
                ),
                mock.patch.object(
                    gate,
                    "_validated_gate_state",
                    return_value=(
                        root / "payload",
                        root / "support",
                        plugin,
                        root / "catalog.json",
                        receipt,
                    ),
                ),
                mock.patch.object(
                    gate,
                    "validate_native_compile_receipt",
                    return_value={"studio": studio},
                ) as validator,
                mock.patch.object(
                    gate, "write_new"
                ) as writer,
                mock.patch.object(
                    gate,
                    "require_native_qualification",
                    return_value={"ok": True},
                ),
            ):
                result = gate.qualify_native(root)
            self.assertTrue(result["ok"])
            validator.assert_called_once_with(
                receipt,
                package_path=plugin,
                expected_package_sha256="a" * 64,
                expected_source_sha256="b" * 64,
            )
            qualification = json.loads(
                writer.call_args.args[1].decode("utf-8")
            )
            self.assertEqual(
                gate.sha256_bytes(state_bytes),
                qualification["gate_state_sha256"],
            )
            self.assertEqual(
                gate.sha256_bytes(receipt_bytes),
                qualification["receipt_sha256"],
            )
            self.assertEqual(
                studio["executable_sha256"],
                qualification[
                    "studio_executable_sha256"
                ],
            )
            self.assertEqual(0o600, writer.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
