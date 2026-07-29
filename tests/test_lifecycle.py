from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from studio_mcp_v2 import __version__
from studio_mcp_v2.frontend import HubTransportError
from studio_mcp_v2.lifecycle import (
    InstallPaths,
    LifecycleError,
    ManagedHubClient,
    SecretsConfig,
    diagnostics,
    build_parser,
    _catalog_diagnostics,
    broker_status,
    ensure_broker,
    load_install_config,
    main_for_installed_support_root,
    stop_broker,
)
from studio_mcp_v2.mcp_stdio import MCPStdioServer
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.session import JobRecord

from .helpers import CATALOG_PATH


CLIENT_TOKEN = "client-" + "c" * 48
STUDIO_TOKEN = "studio-" + "s" * 48


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class TemporaryInstall:
    def __init__(
        self, root: Path, port: int, catalog_source: Path = CATALOG_PATH
    ):
        self.paths = InstallPaths.for_test(root)
        self.paths.config_dir.mkdir(parents=True, mode=0o700)
        os.chmod(self.paths.root, 0o700)
        os.chmod(self.paths.config_dir, 0o700)
        catalog = self.paths.config_dir / "tool-catalog.json"
        shutil.copyfile(catalog_source, catalog)
        os.chmod(catalog, 0o600)
        self.paths.runtime_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "host": "127.0.0.1",
                    "port": port,
                    "catalog": str(catalog),
                    "allowed_studios": ["*"],
                    "allowed_tools": ["*"],
                    "startup_timeout_seconds": 8.0,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.paths.runtime_config, 0o600)
        self.paths.secrets_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "client_token": CLIENT_TOKEN,
                    "studio_token": STUDIO_TOKEN,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.paths.secrets_config, 0o600)


class LifecycleConfigurationTests(unittest.TestCase):
    def test_public_lifecycle_commands_are_stable(self):
        parser = build_parser()
        for command in (
            "stdio",
            "start",
            "status",
            "doctor",
            "diagnostics",
            "stop",
        ):
            self.assertEqual(command, parser.parse_args([command]).command)

    def test_installed_default_ignores_ambient_home_redirect(self):
        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/tmp/attacker-selected-home",
                "STUDIO_MCP_V2_HOME": "/tmp/attacker-selected-v2-home",
            },
        ):
            paths = InstallPaths.default()
        self.assertNotEqual(
            Path("/tmp/attacker-selected-v2-home"), paths.root
        )
        self.assertNotIn("attacker-selected-home", str(paths.root))
        self.assertTrue(str(paths.root).endswith("RobloxStudioMCPv2"))

    def test_pinned_installed_root_must_be_absolute_and_exact(self):
        with self.assertRaisesRegex(LifecycleError, "must be absolute"):
            main_for_installed_support_root(
                Path("relative-support-root"), ["status", "--json"]
            )

    def test_secrets_are_private_and_repr_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            install = TemporaryInstall(Path(directory), 44756)
            _, secrets = load_install_config(install.paths)
            rendered = repr(secrets)
            self.assertNotIn(CLIENT_TOKEN, rendered)
            self.assertNotIn(STUDIO_TOKEN, rendered)
            self.assertIn("<redacted>", rendered)

            os.chmod(install.paths.secrets_config, 0o644)
            with self.assertRaisesRegex(LifecycleError, "permissions"):
                SecretsConfig.load(install.paths)

    def test_catalog_must_be_absolute_and_owned_under_config(self):
        with tempfile.TemporaryDirectory() as directory:
            install = TemporaryInstall(Path(directory), 44756)
            payload = json.loads(
                install.paths.runtime_config.read_text(encoding="utf-8")
            )
            payload["catalog"] = str(CATALOG_PATH.resolve())
            install.paths.runtime_config.write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaisesRegex(LifecycleError, "remain under"):
                load_install_config(install.paths)

    def test_doctor_reports_separate_reviewed_upstream_identity(self):
        durable_catalog = (
            CATALOG_PATH.parent / "durable-tool-catalog.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            install = TemporaryInstall(
                Path(directory), 44756, durable_catalog
            )
            config, _ = load_install_config(install.paths)
            with mock.patch(
                "studio_mcp_v2.lifecycle.audit_installed_v1_cache",
                return_value={
                    "available": True,
                    "status": "review_candidate",
                    "source_sha256": "a" * 64,
                    "tool_count": 25,
                    "counts": {"unchanged": 25, "removed": 6},
                    "fail_closed": False,
                    "changed": [
                        {
                            "name": "legacy_alias",
                            "kind": "removed",
                            "compatibility": "review_required",
                        }
                    ],
                    "changed_truncated": False,
                },
            ):
                report = _catalog_diagnostics(config)
        self.assertEqual(
            "studio-mcp-v2-durable-catalog", report["format"]
        )
        self.assertEqual("0.4.0-dev.2", report["catalog_version"])
        self.assertEqual(
            "production-v1-snapshot-2026-07-26",
            report["upstream"]["version"],
        )
        self.assertEqual(64, len(report["upstream"]["source_sha256"]))
        self.assertEqual(
            25, report["installed_v1_cache"]["tool_count"]
        )
        self.assertNotIn("inputSchema", json.dumps(report))

    def test_detached_environment_strips_python_and_legacy_secrets(self):
        import studio_mcp_v2.lifecycle as lifecycle

        ambient = {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/tmp/inject",
            "PYTHONHOME": "/tmp/python",
            "PYTHONSTARTUP": "/tmp/start.py",
            "DYLD_INSERT_LIBRARIES": "/tmp/inject.dylib",
            "STUDIO_MCP_V2_CLIENT_TOKEN": CLIENT_TOKEN,
            "STUDIO_MCP_V2_STUDIO_TOKEN": STUDIO_TOKEN,
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            child = lifecycle._broker_environment()
        self.assertEqual("/usr/bin:/bin", child["PATH"])
        for name in ambient:
            if name != "PATH":
                self.assertNotIn(name, child)
        self.assertNotIn(CLIENT_TOKEN, json.dumps(child))
        self.assertNotIn(STUDIO_TOKEN, json.dumps(child))


class ManagedHubClientTests(unittest.TestCase):
    @staticmethod
    def _managed_with_delegate(delegate):
        managed = object.__new__(ManagedHubClient)
        managed.paths = mock.sentinel.paths
        managed.config = mock.sentinel.config
        managed.secrets = mock.sentinel.secrets
        managed._client = delegate
        return managed

    def test_discovery_retries_once_after_recovery(self):
        class First:
            def tools(self):
                raise HubTransportError("offline")

        class Replacement:
            def tools(self):
                return {"tools": [{"name": "replacement"}]}

        managed = self._managed_with_delegate(First())

        def recover():
            managed._client = Replacement()

        with mock.patch.object(managed, "_recover", side_effect=recover) as called:
            result = managed.tools()
        self.assertEqual("replacement", result["tools"][0]["name"])
        called.assert_called_once_with()

    def test_operational_call_is_never_replayed_after_transport_loss(self):
        class Failing:
            def __init__(self):
                self.calls = 0

            def call(self, *args):
                self.calls += 1
                raise HubTransportError("response lost")

        delegate = Failing()
        managed = self._managed_with_delegate(delegate)
        with mock.patch.object(managed, "_recover") as recovered:
            with self.assertRaisesRegex(
                HubTransportError, "operation was not replayed"
            ) as raised:
                managed.call(
                    "execute_luau_v2",
                    {"studio_id": "00000000-0000-4000-8000-000000000001"},
                    "request-id",
                )
        self.assertEqual(1, delegate.calls)
        recovered.assert_called_once_with()
        self.assertTrue(raised.exception.details["replacement_ready"])


class ScreenshotMCPResultTests(unittest.TestCase):
    def test_screenshot_becomes_image_block_without_structured_base64_copy(self):
        encoded = base64.b64encode(b"small-png").decode("ascii")
        payload = MCPStdioServer._tool_result(
            {
                "image_base64": encoded,
                "mime_type": "image/png",
                "width": 320,
                "height": 180,
            },
            False,
        )
        self.assertEqual("image", payload["content"][0]["type"])
        self.assertEqual(encoded, payload["content"][0]["data"])
        self.assertEqual("image/png", payload["content"][0]["mimeType"])
        self.assertNotIn("image_base64", payload["structuredContent"])
        self.assertNotIn(encoded, payload["content"][1]["text"])

    def test_invalid_screenshot_is_sanitized_error(self):
        payload = MCPStdioServer._tool_result(
            {
                "image_base64": "not valid base64!",
                "mime_type": "image/png",
                "width": 1,
                "height": 1,
            },
            False,
        )
        self.assertTrue(payload["isError"])
        self.assertNotIn(
            "not valid base64!", json.dumps(payload, sort_keys=True)
        )


class LifecycleSafetySummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_is_bounded_without_limiting_session_map(self):
        registry = SessionRegistry()
        for index in range(260):
            await registry.register(
                client_instance_id=str(uuid.uuid4()),
                registration_secret=("x" * 40) + f"{index:08d}",
                document_epoch=f"bounded-status-document-{index}",
                metadata={"name": "session", "mode": "edit"},
                capabilities=[],
            )
        summary = registry.lifecycle_summary()
        self.assertEqual(260, summary["session_count"])
        self.assertEqual(256, len(summary["sessions"]))
        self.assertTrue(summary["sessions_truncated"])
        self.assertTrue(summary["stop_safe"])

    async def test_requests_jobs_and_uncertainty_are_stop_blockers(self):
        registry = SessionRegistry()
        session, _ = await registry.register(
            client_instance_id=str(uuid.uuid4()),
            registration_secret="r" * 48,
            document_epoch="lifecycle-document",
            metadata={"name": "safe-place", "mode": "edit"},
            capabilities=[],
        )
        session.pending["pending-request"] = mock.sentinel.pending
        session.uncertain_requests["uncertain-request"] = {
            "remote_tool": "execute_luau"
        }
        session.jobs["job"] = JobRecord(
            job_id=str(uuid.uuid4()),
            studio_id=session.studio_id,
            generation=session.generation,
            public_tool="execute_luau_v2",
            remote_tool="execute_luau",
            arguments={},
            timeout_ms=1000,
            status="running",
            dispatched=True,
        )
        summary = registry.lifecycle_summary()
        reasons = summary["stop_blockers"][0]["reasons"]
        self.assertIn("pending_requests", reasons)
        self.assertIn("uncertain_requests", reasons)
        self.assertIn("nonterminal_jobs", reasons)
        self.assertFalse(summary["stop_safe"])
        rendered = json.dumps(summary)
        self.assertNotIn("execute_luau", rendered)

    async def test_noncompleted_play_transition_is_a_stop_blocker(self):
        registry = SessionRegistry()
        session, _ = await registry.register(
            client_instance_id=str(uuid.uuid4()),
            registration_secret="p" * 48,
            document_epoch="play-lifecycle-document",
            metadata={"name": "safe-place", "mode": "edit"},
            capabilities=[],
        )
        registry.play_bridges.prepare(
            session.studio_id,
            session.client_instance_id,
            session.document_epoch,
            session.generation,
            str(uuid.uuid4()),
            1001,
            2002,
        )
        summary = registry.lifecycle_summary()
        self.assertFalse(summary["stop_safe"])
        self.assertEqual(1, summary["unsafe_transition_count"])
        self.assertIn(
            "play_transition_active",
            summary["stop_blockers"][0]["reasons"],
        )

    async def test_terminal_disconnected_session_is_safe_then_audited_and_retired(
        self,
    ):
        registry = SessionRegistry(terminal_retirement_grace_seconds=60)
        session, _ = await registry.register(
            client_instance_id=str(uuid.uuid4()),
            registration_secret="t" * 48,
            document_epoch="terminal-disconnected-document",
            metadata={"name": "terminal", "mode": "edit"},
            capabilities=[],
        )
        self.assertTrue(
            session.disconnect(session.generation, "clean plugin shutdown")
        )

        retained = registry.lifecycle_summary()
        self.assertTrue(retained["stop_safe"])
        self.assertEqual(1, retained["session_count"])
        self.assertTrue(
            retained["sessions"][0]["retained_terminal_disconnected"]
        )
        self.assertEqual([], retained["sessions"][0]["blockers"])

        assert session.disconnected_at_monotonic is not None
        session.disconnected_at_monotonic -= 61
        compacted = registry.lifecycle_summary()
        self.assertTrue(compacted["stop_safe"])
        self.assertEqual(0, compacted["session_count"])
        self.assertEqual(1, compacted["retired_session_count"])
        audit = compacted["retired_session_audit"][0]
        self.assertEqual(session.studio_id, audit["studio_id"])
        self.assertEqual(session.document_epoch, audit["document_epoch"])
        self.assertEqual("edit", audit["last_confirmed_mode"])
        self.assertIn("retirement_grace_elapsed", audit["basis"])

    async def test_uncertain_disconnected_session_is_never_compacted(self):
        registry = SessionRegistry(terminal_retirement_grace_seconds=0)
        session, _ = await registry.register(
            client_instance_id=str(uuid.uuid4()),
            registration_secret="u" * 48,
            document_epoch="uncertain-disconnected-document",
            metadata={"name": "uncertain", "mode": "edit"},
            capabilities=[],
        )
        session.uncertain_requests["unknown-outcome"] = {
            "reason": "response_timeout_after_dispatch"
        }
        session.disconnect(session.generation, "connection lost")

        summary = registry.lifecycle_summary()
        self.assertFalse(summary["stop_safe"])
        self.assertEqual(1, summary["session_count"])
        self.assertEqual(0, summary["retired_session_count"])
        self.assertIn(
            "uncertain_requests",
            summary["stop_blockers"][0]["reasons"],
        )

    async def test_active_play_transition_prevents_terminal_retirement(self):
        registry = SessionRegistry(terminal_retirement_grace_seconds=0)
        session, _ = await registry.register(
            client_instance_id=str(uuid.uuid4()),
            registration_secret="a" * 48,
            document_epoch="active-play-disconnected-document",
            metadata={"name": "active", "mode": "edit"},
            capabilities=[],
        )
        registry.play_bridges.prepare(
            session.studio_id,
            session.client_instance_id,
            session.document_epoch,
            session.generation,
            str(uuid.uuid4()),
            1001,
            2002,
        )
        session.disconnect(session.generation, "connection lost")

        summary = registry.lifecycle_summary()
        self.assertFalse(summary["stop_safe"])
        self.assertEqual(1, summary["session_count"])
        self.assertEqual(0, summary["retired_session_count"])
        self.assertIn(
            "play_transition_active",
            summary["stop_blockers"][0]["reasons"],
        )


class LifecycleProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.install = TemporaryInstall(
            Path(self.temporary.name), _unused_loopback_port()
        )
        self.config, self.secrets = load_install_config(self.install.paths)

    def tearDown(self):
        try:
            stop_broker(self.install.paths, self.config, self.secrets)
        except Exception:
            pass
        self.temporary.cleanup()

    def test_concurrent_ensure_reuses_one_authenticated_broker(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    ensure_broker,
                    self.install.paths,
                    self.config,
                    self.secrets,
                )
                for _ in range(2)
            ]
            first, second = [future.result(timeout=15) for future in futures]
        self.assertEqual(
            first["broker_instance_id"], second["broker_instance_id"]
        )
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(__version__, first["version"])
        self.assertTrue(first["stop_safe"])

        state = self.install.paths.broker_state.read_text(encoding="utf-8")
        log = self.install.paths.broker_log.read_text(encoding="utf-8")
        combined = state + log
        self.assertNotIn(CLIENT_TOKEN, combined)
        self.assertNotIn(STUDIO_TOKEN, combined)

    def test_corrupt_state_is_repaired_from_authenticated_health(self):
        running = ensure_broker(
            self.install.paths, self.config, self.secrets
        )
        self.install.paths.broker_state.write_text(
            '{"corrupt":true}\n', encoding="utf-8"
        )
        os.chmod(self.install.paths.broker_state, 0o600)
        repaired = ensure_broker(
            self.install.paths, self.config, self.secrets
        )
        self.assertEqual(
            running["broker_instance_id"], repaired["broker_instance_id"]
        )
        state = json.loads(
            self.install.paths.broker_state.read_text(encoding="utf-8")
        )
        self.assertEqual(
            running["broker_instance_id"], state["broker_instance_id"]
        )

    def test_diagnostics_are_secret_free_and_targeting_is_explicit(self):
        before = broker_status(
            self.install.paths, self.config, self.secrets
        )
        self.assertEqual("stopped", before["condition"])
        stopped_report = diagnostics(
            self.install.paths, self.config, self.secrets
        )
        self.assertTrue(stopped_report["ok"])
        self.assertFalse(
            stopped_report["observations"]["authenticated_health"]
        )
        ensure_broker(self.install.paths, self.config, self.secrets)
        report = diagnostics(self.install.paths, self.config, self.secrets)
        encoded = json.dumps(report, sort_keys=True)
        self.assertTrue(report["ok"])
        self.assertTrue(
            report["catalog"]["all_operations_require_studio_id"]
        )
        self.assertEqual(
            [], report["catalog"]["forbidden_active_or_default_tools"]
        )
        self.assertEqual(
            "healthy_idle", report["lifecycle"]["condition"]
        )
        self.assertNotIn(CLIENT_TOKEN, encoded)
        self.assertNotIn(STUDIO_TOKEN, encoded)

    def test_authenticated_stop_and_fresh_restart_change_instance(self):
        first = ensure_broker(
            self.install.paths, self.config, self.secrets
        )
        stopped = stop_broker(
            self.install.paths, self.config, self.secrets
        )
        self.assertTrue(stopped["stopped"])
        second = ensure_broker(
            self.install.paths, self.config, self.secrets
        )
        self.assertNotEqual(
            first["broker_instance_id"], second["broker_instance_id"]
        )

    def test_unverified_loopback_listener_is_never_adopted_or_claimed_stopped(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.config.host, self.config.port))
            listener.listen(1)
            status = broker_status(
                self.install.paths, self.config, self.secrets
            )
            self.assertEqual(
                "unauthenticated_or_unexpected_listener",
                status["condition"],
            )
            with self.assertRaisesRegex(
                LifecycleError, "did not pass authenticated"
            ):
                ensure_broker(
                    self.install.paths, self.config, self.secrets
                )
            with self.assertRaisesRegex(
                LifecycleError, "refusing to claim it is stopped"
            ):
                stop_broker(
                    self.install.paths, self.config, self.secrets
                )

    def test_pinned_installed_entry_routes_status_to_that_exact_root(self):
        exact_root = self.install.paths.root.resolve(strict=True)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            main_for_installed_support_root(
                exact_root, ["status", "--json"]
            )
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(str(exact_root), payload["support_root"])
        self.assertEqual("stopped", payload["lifecycle"]["condition"])
