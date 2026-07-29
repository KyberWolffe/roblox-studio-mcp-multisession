from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scripts import render_studio_plugin
from studio_mcp_v2 import catalog_review
from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.catalog_review import (
    audit_installed_v1_cache,
    import_reviewed_catalog,
    load_catalog,
    load_compatibility_manifest,
    prepare_catalog_import,
    regenerate_durable_catalog,
    review_catalogs,
    rollback_catalog_import,
)
from studio_mcp_v2.errors import SessionConflictError, ValidationError
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.session import LongPollTransport


ROOT = Path(__file__).resolve().parent.parent
DURABLE_CATALOG = ROOT / "config" / "durable-tool-catalog.json"
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
COMPATIBILITY_MANIFEST = (
    ROOT / "config" / "upstream-compatibility-map.json"
)
FIXTURES = ROOT / "tests" / "fixtures"
TOKEN = "t" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"

DURABLE_OPERATIONS = {
    "studio_get_state",
    "studio_list_tree",
    "studio_inspect_instance",
    "studio_search_scripts",
    "studio_grep_scripts",
    "studio_read_script",
    "studio_update_script",
    "studio_multi_edit",
    "studio_recover_multi_edit",
    "studio_set_attribute",
    "studio_get_console",
    "studio_capture_screenshot",
    "studio_fire_input_binding",
    "studio_start_stop_play",
}


class DurableCatalogTests(unittest.TestCase):
    def test_catalog_is_versioned_closed_and_explicitly_targeted(self):
        payload = json.loads(DURABLE_CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(payload["catalog_version"], "0.4.0-rc.4")
        self.assertEqual(
            payload["upstream"]["compatibility"],
            "reviewed-local-subset-only",
        )
        self.assertEqual(
            {tool["name"] for tool in payload["tools"]},
            DURABLE_OPERATIONS,
        )
        catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.assertEqual(catalog.remote_names, frozenset(DURABLE_OPERATIONS))
        for tool in catalog.tools_for_mcp():
            schema = tool["inputSchema"]
            self.assertIn("studio_id", schema["required"])
            self.assertEqual(
                schema["properties"]["studio_id"]["format"],
                "uuid",
            )
            self.assertIn(
                "routing context, not authorization",
                schema["properties"]["studio_id"]["description"],
            )

    def test_every_exposed_operation_has_one_closed_handler_branch(self):
        source = HANDLERS.read_text(encoding="utf-8")
        for operation in DURABLE_OPERATIONS:
            self.assertEqual(
                source.count(
                    'request.operation == "' + operation + '"'
                ),
                1,
                operation,
            )
            self.assertIn(operation + " = table.freeze(", source)
        for test_only in (
            "rnd_marker_create",
            "rnd_marker_remove",
            "rnd_shutdown",
        ):
            self.assertNotIn(test_only, source)

    def test_mutations_require_optimistic_prior_state(self):
        payload = json.loads(DURABLE_CATALOG.read_text(encoding="utf-8"))
        tools = {tool["name"]: tool for tool in payload["tools"]}
        update = tools["studio_update_script"]
        self.assertIn(
            "expected_sha256",
            update["inputSchema"]["required"],
        )
        attribute = tools["studio_set_attribute"]
        self.assertIn(
            "expected_exists",
            attribute["inputSchema"]["required"],
        )
        self.assertIn(
            "expected_value_type",
            attribute["inputSchema"]["required"],
        )
        self.assertFalse(update["annotations"]["readOnlyHint"])
        self.assertTrue(update["annotations"]["destructiveHint"])
        self.assertFalse(attribute["annotations"]["readOnlyHint"])
        self.assertTrue(attribute["annotations"]["destructiveHint"])
        handlers = HANDLERS.read_text(encoding="utf-8")
        self.assertIn("Enum.HashAlgorithm.Sha256", handlers)
        self.assertIn("#binaryDigest ~= 32", handlers)
        self.assertIn(
            'string.byte(binaryDigest, index)',
            handlers,
        )
        self.assertIn("script_revision_conflict", handlers)
        self.assertIn("attribute_revision_conflict", handlers)


class DurableRendererTests(unittest.TestCase):
    def render(self, base_url: str = "http://127.0.0.1:44756") -> str:
        return render_studio_plugin.render_durable(
            TOKEN,
            RUN_ID,
            base_url=base_url,
        )

    def test_render_matches_phase2_0_4_0_rc_4_contract(self):
        rendered = self.render().encode("utf-8")
        self.assertEqual(
            hashlib.sha256(rendered).hexdigest(),
            "2cd30c4864178fece6ba082ce804219305ef54608963ef9aa590950cca65062e",
        )

    def test_durable_handlers_are_private_and_below_local_budget(self):
        handlers = HANDLERS.read_text(encoding="utf-8")
        handler_locals = (
            render_studio_plugin._top_level_luau_local_count(handlers)
        )
        self.assertLessEqual(
            handler_locals,
            render_studio_plugin.MAX_DURABLE_HANDLER_LOCALS,
        )
        self.assertLessEqual(
            render_studio_plugin.MAX_DURABLE_HANDLER_LOCALS,
            render_studio_plugin.MAX_LUAU_LOCALS_PER_FUNCTION
            - render_studio_plugin.LUAU_LOCAL_HEADROOM,
        )

        rendered = self.render()
        self.assertEqual(
            1,
            rendered.count("local DURABLE_HANDLERS = (function()"),
        )
        self.assertEqual(
            1,
            rendered.count(
                "local validateRequest = "
                "DURABLE_HANDLERS.validateRequest"
            ),
        )
        self.assertEqual(
            1,
            rendered.count(
                "local dispatch = DURABLE_HANDLERS.dispatch"
            ),
        )
        self.assertIn(
            "\treturn table.freeze({\n"
            "\t\tvalidateRequest = validateRequest,\n"
            "\t\tdispatch = dispatch,\n"
            "\t})\n"
            "end)()",
            rendered,
        )
        self.assertLessEqual(
            render_studio_plugin._top_level_luau_local_count(rendered),
            render_studio_plugin.MAX_RENDERED_CHUNK_LOCALS,
        )

    def test_static_local_budget_masks_inert_long_bracket_source(self):
        source = (
            "local first, second = 1, 2\n"
            "local EMBEDDED = [=[\n"
            "local not_code = true\n"
            "local function also_not_code()\n"
            "end\n"
            "]=]\n"
            "local function third()\n"
            "end\n"
        )
        self.assertEqual(
            4,
            render_studio_plugin._top_level_luau_local_count(source),
        )

    def test_handler_closure_fails_closed_on_export_or_budget_drift(self):
        handlers = HANDLERS.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "export drifted"):
            render_studio_plugin._durable_handlers_closure(
                handlers.replace(
                    "local function dispatch(request)",
                    "local function renamedDispatch(request)",
                    1,
                )
            )
        excess = handlers + "\n" + "\n".join(
            "local unexpected_" + str(index) + " = true"
            for index in range(
                render_studio_plugin.MAX_DURABLE_HANDLER_LOCALS
            )
        )
        with self.assertRaisesRegex(ValueError, "local budget"):
            render_studio_plugin._durable_handlers_closure(excess)

    def test_render_has_no_two_place_or_session_count_limit(self):
        source = self.render()
        self.assertNotIn("ALLOWED_DOCUMENTS", source)
        self.assertNotRegex(
            source, r"\[[0-9]{10,}\]\s*=\s*table\.freeze"
        )
        self.assertIn("EXPECTED_PLACE_ID < 0", source)
        self.assertIn("EXPECTED_GAME_ID < 0", source)
        self.assertIn("local CLIENT_INSTANCE_ID = newUuid()", source)
        self.assertIn("local DOCUMENT_EPOCH = newUuid()", source)
        self.assertIn("local SESSION_TAG = string.sub(", source)
        self.assertIn("studio_id = result.studio_id", source)

    def test_render_preserves_identity_and_fences_broker_restarts(self):
        source = self.render()
        self.assertIn("broker_instance_id = nil", source)
        self.assertIn(
            "result.broker_instance_id ~= peer.broker_instance_id",
            source,
        )
        self.assertIn("local function establishInitialConnection()", source)
        self.assertIn("math.min(delaySeconds * 2, 8)", source)
        self.assertIn("local function restoreConnection()", source)
        fresh_guard = source.index("if peer.active_play == nil then")
        fresh_connect = source.index(
            "local registered, registrationError = connect(false)",
            fresh_guard,
        )
        self.assertLess(fresh_guard, fresh_connect)
        self.assertNotIn("plugin:GetSetting", source)
        self.assertNotIn("plugin:SetSetting", source)

    def test_render_resolves_published_place_name_with_safe_fallback(self):
        source = self.render()
        self.assertIn(
            'local MarketplaceService = game:GetService("MarketplaceService")',
            source,
        )
        self.assertIn("local function resolveDocumentName()", source)
        self.assertIn("if EXPECTED_PLACE_ID <= 0 then", source)
        self.assertIn(
            "MarketplaceService:GetProductInfoAsync(",
            source,
        )
        self.assertIn("Enum.InfoType.Asset", source)
        self.assertIn('type(assetName) ~= "string" or assetName == ""', source)
        self.assertIn("local DOCUMENT_NAME = resolveDocumentName()", source)
        self.assertEqual(source.count("name = DOCUMENT_NAME"), 2)
        self.assertIn(
            "name = instance == game and game.Name or instance.Name",
            source,
        )

    def test_render_keeps_slow_play_alive_with_aligned_bounds(self):
        source = self.render()
        self.assertIn("local PLAY_PRE_ATTACH_TTL_SECONDS = 180", source)
        self.assertIn("local PLAY_ACTIVATION_SECONDS = 150", source)
        self.assertIn("local RUNNER_RETURN_SECONDS = 70", source)
        self.assertIn("local RUNNER_STATUS_INTERVAL_SECONDS = 1", source)
        self.assertIn(
            "activation_deadline_unix = os.time() + PLAY_ACTIVATION_SECONDS",
            source,
        )
        self.assertIn("ttl_seconds = PLAY_PRE_ATTACH_TTL_SECONDS", source)
        self.assertIn("local HARD_WATCHDOG_SECONDS = 180", source)
        self.assertIn("local MAX_ACTIVATION_WINDOW_SECONDS = 165", source)
        runner_wait = source[
            source.index("local function waitForRunner("):
            source.index("local function waitForStableEdit()")
        ]
        self.assertIn('"/v2/studios/play-bridge/status"', runner_wait)
        self.assertIn("transitionBody(transition)", runner_wait)
        self.assertIn("status.watchdog_armed == true", runner_wait)

    def test_render_uses_two_phase_play_acceptance_and_observation(self):
        source = self.render()
        start = source[
            source.index("local function startPlay("):
            source.index("local function stopPlay(")
        ]
        self.assertIn("monitorPlayTransition(transition)", start)
        self.assertIn("accepted = true", start)
        self.assertIn('state = "starting"', start)
        self.assertIn('mode = "starting"', start)
        self.assertNotIn("readyDeadline", start)
        self.assertNotIn("waitForRunner(", start)

        stop = source[
            source.index("local function stopPlay("):
            source.index("local function getState(")
        ]
        self.assertIn('state = "stopping"', stop)
        self.assertIn('mode = "stopping"', stop)
        self.assertNotIn("waitForRunner(", stop)
        self.assertIn("local function playStateSnapshot()", source)
        self.assertIn('return "recovery_required"', source)

    def test_render_uses_plugin_context_bridge_with_inert_marker(self):
        source = self.render()
        play_branch = source.index(
            "local function runPlayServerPluginBridge()"
        )
        edit_controller = source.index(
            "assert(plugin ~= nil",
            play_branch,
        )
        self.assertLess(play_branch, edit_controller)
        self.assertIn("initialIsRunning == true", source[:edit_controller])
        self.assertIn("initialIsServer == true", source[:edit_controller])
        self.assertIn("bridge.Enabled = false", source)
        self.assertIn(
            "local bridgeMarker = ServerScriptService:FindFirstChild(",
            source,
        )
        self.assertIn("bridgeMarker.Enabled ~= false", source)
        self.assertIn("request_exception_http_disabled", source)
        self.assertNotIn("HttpService.HttpEnabled =", source)

    def test_render_has_fixed_loopback_origin_and_no_host_execution(self):
        source = self.render("http://127.0.0.1:45123")
        self.assertEqual(
            source.count('local BASE_URL = "http://127.0.0.1:45123"'),
            3,
        )
        self.assertNotIn("loadstring", source.lower())
        self.assertNotIn("os.execute", source.lower())
        self.assertNotIn("io.popen", source.lower())
        self.assertNotIn("localhost", source)
        with self.assertRaises(ValueError):
            self.render("http://example.com:45123")
        with self.assertRaises(ValueError):
            self.render("https://127.0.0.1:45123")
        with self.assertRaises(ValueError):
            self.render("http://127.0.0.1:45123/path")

    def test_rendered_capabilities_match_durable_catalog(self):
        source = self.render()
        capability_region = source[
            source.index("local CAPABILITIES = table.freeze({"):
            source.index("local REQUEST_KEYS = table.freeze({")
        ]
        for operation in DURABLE_OPERATIONS:
            self.assertIn('"' + operation + '"', capability_region)
            self.assertIn(operation + " = true", capability_region)
        for validation_only in (
            '"rnd_get_state"',
            '"rnd_marker_create"',
            '"rnd_play_start"',
            '"rnd_play_stop"',
            '"rnd_shutdown"',
        ):
            self.assertNotIn(validation_only, capability_region)

    def test_screenshot_and_input_contracts_fail_closed(self):
        source = self.render()
        self.assertIn("local function optionalService(name)", source)
        self.assertIn(
            'optionalService("StudioCaptureService")',
            source,
        )
        self.assertIn("image_base64 = imageBase64", source)
        self.assertIn('mime_type = "image/png"', source)
        self.assertIn("width = math.floor(resolution.X)", source)
        self.assertIn("height = math.floor(resolution.Y)", source)
        self.assertIn("MAX_SCREENSHOT_BYTES = 600_000", source)
        self.assertIn("StudioCaptureService:CanCaptureScreenshot()", source)
        self.assertIn(
            "capture.BufferFormat ~= "
            "Enum.StudioCaptureScreenshotFormat.PNG",
            source,
        )
        self.assertIn('target:IsA("InputBinding")', source)
        self.assertIn(
            "target.Type ~= Enum.InputBindingType.Scriptable",
            source,
        )
        self.assertNotIn("VirtualInputManager", source)
        self.assertNotIn("SendKeyEvent", source)
        self.assertNotIn("SendMouse", source)

    def test_durable_bundle_can_reuse_installer_owned_credentials(self):
        first = render_studio_plugin.render_fresh_durable_bundle(
            studio_token=TOKEN,
            run_id=RUN_ID,
        )
        second = render_studio_plugin.render_fresh_durable_bundle(
            studio_token=TOKEN,
            run_id=RUN_ID,
        )
        self.assertEqual(first.plugin_source, second.plugin_source)
        self.assertEqual(first.studio_token, TOKEN)
        self.assertEqual(first.run_id, RUN_ID)
        self.assertIn("StudioMCPv2SideBySide", first.plugin_package_rbxmx)


class UpstreamCatalogReviewTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_compatibility_manifest(
            COMPATIBILITY_MANIFEST
        )
        self.durable, self.durable_bytes = load_catalog(DURABLE_CATALOG)

    @staticmethod
    def catalog(*tools):
        return {
            "format": "test-upstream",
            "tools": list(tools),
        }

    @staticmethod
    def tool(name, properties=None, family=None):
        result = {
            "name": name,
            "description": name,
            "inputSchema": {
                "type": "object",
                "properties": properties or {},
                "required": [],
                "additionalProperties": False,
            },
        }
        if family is not None:
            result["x_studio_mcp_v2_family"] = family
        return result

    def test_compatible_added_tool_is_review_only_not_auto_enabled(self):
        baseline, baseline_bytes = load_catalog(
            FIXTURES / "upstream-catalog-baseline.json"
        )
        candidate, candidate_bytes = load_catalog(
            FIXTURES / "upstream-catalog-compatible-addition.json"
        )
        review = review_catalogs(
            baseline,
            candidate,
            baseline_bytes=baseline_bytes,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )
        added = [
            change for change in review.changes if change.kind == "new"
        ]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].compatibility, "compatible_candidate")
        self.assertEqual(added[0].durable_handler, "studio_read_script")
        self.assertFalse(review.fail_closed)
        generated = regenerate_durable_catalog(
            self.durable,
            candidate,
            candidate_bytes,
            review,
            self.manifest,
        )
        generated_catalog = ToolCatalog(generated["tools"])
        self.assertIn("studio_read_script", generated_catalog.remote_names)
        self.assertNotIn(
            "studio_structured_script_read",
            generated_catalog.remote_names,
        )
        expected = json.loads(
            (
                FIXTURES / "upstream-compatible-generation.json"
            ).read_text(encoding="utf-8")
        )
        generation = generated["compatibility_generation"]
        self.assertEqual(len(generation), 1)
        self.assertEqual(
            generation[0]["upstream_name"],
            expected["upstream_name"],
        )
        self.assertEqual(
            generation[0]["durable_handler"],
            expected["durable_handler"],
        )
        durable = ToolCatalog.from_file(DURABLE_CATALOG)
        self.assertNotIn(
            "studio_structured_script_read",
            durable.remote_names,
        )

    def test_unknown_added_family_fails_closed(self):
        baseline = self.catalog(self.tool("existing"))
        candidate = self.catalog(
            self.tool("existing"),
            self.tool("do_anything", {}),
        )
        review = review_catalogs(
            baseline,
            candidate,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )
        self.assertTrue(review.fail_closed)
        self.assertEqual(
            [
                change.compatibility
                for change in review.changes
                if change.kind == "new"
            ],
            ["unknown_family"],
        )

    def test_reviewed_import_is_atomic_and_keeps_exact_backup(self):
        baseline_bytes = (
            FIXTURES / "upstream-catalog-baseline.json"
        ).read_bytes()
        candidate_bytes = (
            FIXTURES / "upstream-catalog-compatible-addition.json"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline_path = root / "upstream.json"
            candidate_path = root / "candidate.json"
            durable_path = root / "durable.json"
            baseline_path.write_bytes(baseline_bytes)
            candidate_path.write_bytes(candidate_bytes)
            durable_path.write_bytes(self.durable_bytes)
            prepared = prepare_catalog_import(
                baseline_path,
                candidate_path,
                compatibility_manifest_path=COMPATIBILITY_MANIFEST,
                durable_catalog_path=durable_path,
                regenerate_durable=True,
            )
            self.assertTrue(prepared["ready"])
            self.assertFalse(prepared["mutated"])
            self.assertEqual(
                prepared["generated_from"][0]["durable_handler"],
                "studio_read_script",
            )
            self.assertEqual(baseline_path.read_bytes(), baseline_bytes)
            self.assertEqual(durable_path.read_bytes(), self.durable_bytes)
            with self.assertRaises(ValidationError):
                import_reviewed_catalog(
                    baseline_path,
                    candidate_path,
                    approve_reviewed_changes=False,
                    compatibility_manifest_path=COMPATIBILITY_MANIFEST,
                    durable_catalog_path=durable_path,
                    regenerate_durable=True,
                )
            result = import_reviewed_catalog(
                baseline_path,
                candidate_path,
                approve_reviewed_changes=True,
                expected_candidate_sha256=hashlib.sha256(
                    candidate_bytes
                ).hexdigest(),
                compatibility_manifest_path=COMPATIBILITY_MANIFEST,
                durable_catalog_path=durable_path,
                regenerate_durable=True,
            )
            self.assertEqual(baseline_path.read_bytes(), candidate_bytes)
            generated, _generated_bytes = load_catalog(durable_path)
            generated_names = {
                tool["name"] for tool in generated["tools"]
            }
            self.assertEqual(generated_names, DURABLE_OPERATIONS)
            self.assertEqual(
                generated["upstream"]["version"],
                "fixture-2",
            )
            self.assertTrue(
                result["contract"]["all_operations_require_studio_id"]
            )
            self.assertEqual(
                Path(result["backups"][0]).read_bytes(),
                baseline_bytes,
            )
            rolled_back = rollback_catalog_import(
                Path(result["receipt"])
            )
            self.assertEqual(len(rolled_back["rolled_back"]), 2)
            self.assertEqual(baseline_path.read_bytes(), baseline_bytes)
            self.assertEqual(durable_path.read_bytes(), self.durable_bytes)

    def test_reviewed_import_rejects_candidate_changed_after_acknowledgement(self):
        baseline_bytes = (
            FIXTURES / "upstream-catalog-baseline.json"
        ).read_bytes()
        candidate_bytes = (
            FIXTURES / "upstream-catalog-compatible-addition.json"
        ).read_bytes()
        accepted_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline_path = root / "upstream.json"
            candidate_path = root / "candidate.json"
            durable_path = root / "durable.json"
            baseline_path.write_bytes(baseline_bytes)
            candidate_path.write_bytes(candidate_bytes)
            durable_path.write_bytes(self.durable_bytes)

            candidate_path.write_bytes(baseline_bytes)
            with self.assertRaisesRegex(
                ValidationError,
                "changed after checksum review",
            ):
                import_reviewed_catalog(
                    baseline_path,
                    candidate_path,
                    approve_reviewed_changes=True,
                    expected_candidate_sha256=accepted_sha256,
                    compatibility_manifest_path=COMPATIBILITY_MANIFEST,
                    durable_catalog_path=durable_path,
                    regenerate_durable=True,
                )

            self.assertEqual(baseline_path.read_bytes(), baseline_bytes)
            self.assertEqual(durable_path.read_bytes(), self.durable_bytes)
            self.assertEqual([], list(root.glob("*.backup-*")))
            self.assertEqual(
                [],
                list(root.glob("catalog-import-receipt-*.json")),
            )

    def test_pwd_resolved_installed_cache_audit_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake_home = Path(temporary)
            cache = (
                fake_home
                / "Library"
                / "Application Support"
                / "StudioMCP"
                / "tools-cache.json"
            )
            cache.parent.mkdir(parents=True)
            cache.write_bytes(
                (
                    FIXTURES
                    / "upstream-catalog-compatible-addition.json"
                ).read_bytes()
            )
            with mock.patch.object(
                catalog_review.pwd,
                "getpwuid",
                return_value=types.SimpleNamespace(
                    pw_dir=str(fake_home)
                ),
            ), mock.patch.dict(
                os.environ,
                {"HOME": "/must/not/be/used"},
            ):
                report = audit_installed_v1_cache(
                    baseline_path=(
                        FIXTURES / "upstream-catalog-baseline.json"
                    ),
                    compatibility_manifest_path=COMPATIBILITY_MANIFEST,
                    durable_catalog_path=DURABLE_CATALOG,
                )
            self.assertTrue(report["available"])
            self.assertEqual(report["path"], str(cache))
            self.assertEqual(report["tool_count"], 2)
            self.assertEqual(report["counts"]["unchanged"], 1)
            self.assertEqual(report["counts"]["added"], 1)
            self.assertFalse(report["fail_closed"])
            self.assertEqual(
                json.loads(cache.read_text(encoding="utf-8"))[
                    "catalog_version"
                ],
                "fixture-2",
            )

    def test_current_25_tool_cache_shape_is_nonblocking(self):
        baseline, baseline_bytes = load_catalog(
            ROOT / "config" / "tool-catalog.json"
        )
        removed_aliases = {
            "GetConsoleOutput",
            "GetStudioMode",
            "InsertModel",
            "RunCode",
            "RunScriptInPlayMode",
            "StartStopPlay",
        }
        candidate = {
            "tools": [
                tool
                for tool in baseline["tools"]
                if tool["name"] not in removed_aliases
            ],
            "date": "fixture-installed-cache",
        }
        candidate_bytes = json.dumps(
            candidate,
            separators=(",", ":"),
        ).encode("utf-8")
        review = review_catalogs(
            baseline,
            candidate,
            baseline_bytes=baseline_bytes,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )
        self.assertFalse(review.fail_closed)
        self.assertEqual(
            sum(change.kind == "unchanged" for change in review.changes),
            25,
        )
        self.assertEqual(
            {
                change.name
                for change in review.changes
                if change.kind == "removed"
            },
            removed_aliases,
        )


class DurablePlayBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = SessionRegistry()
        self.transport = LongPollTransport()
        self.session, self.registration = await self.registry.register(
            client_instance_id=str(uuid.uuid4()),
            registration_secret=secrets.token_urlsafe(48),
            document_epoch=str(uuid.uuid4()),
            metadata={
                "name": "Unpublished disposable",
                "mode": "edit",
                "place_id": 0,
                "game_id": 0,
            },
            capabilities={"studio_start_stop_play"},
            transport=self.transport,
        )

    async def invoke_pending(self, request_id, arguments):
        task = asyncio.create_task(
            self.session.invoke(
                "studio_start_stop_play",
                arguments,
                5_000,
                request_id=request_id,
            )
        )
        request = await self.transport.poll(1)
        self.assertIsNotNone(request)
        return task, request

    def prepare(self, request_id):
        return self.registry.prepare_play_bridge(
            self.registration.studio_id,
            self.registration.document_epoch,
            self.registration.generation,
            self.registration.resume_token,
            request_id,
        )

    def settle(self, request, result):
        self.registry.receive_response(
            self.registration.studio_id,
            self.registration.generation,
            self.registration.resume_token,
            request["request_id"],
            success=True,
            result=result,
        )

    async def test_start_phase_rejects_swapped_and_malformed_arguments(self):
        stop_task, stop_request = await self.invoke_pending(
            "wrong-phase-stop",
            {"is_start": False},
        )
        with self.assertRaises(SessionConflictError):
            self.prepare("wrong-phase-stop")
        self.settle(stop_request, {"stopped": True})
        await stop_task

        malformed_task, malformed_request = await self.invoke_pending(
            "malformed-start",
            {"is_start": True, "unexpected": True},
        )
        with self.assertRaises(SessionConflictError):
            self.prepare("malformed-start")
        self.settle(malformed_request, {"started": False})
        await malformed_task

    async def test_unpublished_zero_ids_and_exact_durable_phases_work(self):
        start_task, start_request = await self.invoke_pending(
            "durable-start",
            {"is_start": True},
        )
        prepared = self.prepare("durable-start")
        self.assertEqual(prepared["expected_place_id"], 0)
        self.assertEqual(prepared["expected_game_id"], 0)
        self.settle(start_request, {"started": True})
        await start_task

        stop_task, stop_request = await self.invoke_pending(
            "durable-stop",
            {"is_start": False},
        )
        stopped = self.registry.request_play_bridge_stop(
            self.registration.studio_id,
            self.registration.document_epoch,
            self.registration.generation,
            self.registration.resume_token,
            self.registration.generation,
            "durable-start",
            prepared["transition_nonce"],
            "durable-stop",
        )
        self.assertTrue(stopped["stop_requested"])
        self.settle(stop_request, {"stopped": True})
        await stop_task


if __name__ == "__main__":
    unittest.main()
