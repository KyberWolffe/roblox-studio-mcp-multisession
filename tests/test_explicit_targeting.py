from __future__ import annotations

import asyncio
import ast
import unittest
import uuid

from studio_mcp_v2.catalog import DISCOVERY_TOOL, JOB_TOOLS
from studio_mcp_v2.errors import (
    SessionNotFoundError,
    ValidationError,
)

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio, make_service


class ExplicitTargetingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry, self.catalog, self.service = make_service()
        self.remote_names = self.catalog.remote_names

    async def test_every_operational_schema_requires_studio_id(self):
        for tool in self.catalog.tools_for_mcp() + JOB_TOOLS:
            with self.subTest(tool=tool["name"]):
                required = tool["inputSchema"].get("required", [])
                self.assertIn("studio_id", required)
                self.assertIn(
                    "studio_id", tool["inputSchema"].get("properties", {})
                )
        self.assertNotIn(
            "studio_id", DISCOVERY_TOOL["inputSchema"].get("required", [])
        )
        description = DISCOVERY_TOOL["description"]
        self.assertIn("ordinary place/project name", description)
        self.assertIn("metadata.name", description)
        self.assertIn("metadata.place_id", description)
        self.assertIn("metadata.game_id", description)
        self.assertIn("never ask the user", description)
        self.assertIn("duplicate or unsaved names", description)

    async def test_missing_id_never_falls_back_with_zero_one_or_two_sessions(self):
        public_tool = "get_studio_state_v2"
        for count in (0, 1, 2):
            while self.registry.session_count() < count:
                await FakeStudio.create(
                    self.registry,
                    "session-" + str(self.registry.session_count()),
                    self.remote_names,
                )
            with self.subTest(session_count=count):
                with self.assertRaises(ValidationError):
                    await self.service.call_tool(ALLOW_ALL, public_tool, {})
                for session in self.registry._sessions.values():
                    self.assertEqual(0, len(session.pending))
                    self.assertTrue(session.transport._queue.empty())

    async def test_malformed_ids_fail_before_transport_send(self):
        studio = await FakeStudio.create(
            self.registry, "only", self.remote_names
        )
        malformed = [
            None,
            "",
            " ",
            3,
            [],
            {},
            "__proto__",
            "constructor",
            str(uuid.uuid4()).upper(),
            "x" * 500,
        ]
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    await self.service.call_tool(
                        ALLOW_ALL,
                        "get_studio_state_v2",
                        {"studio_id": value},
                    )
                self.assertTrue(studio.transport._queue.empty())

    async def test_unknown_id_does_not_route_to_valid_session(self):
        valid = await FakeStudio.create(
            self.registry, "valid", self.remote_names
        )
        with self.assertRaises(SessionNotFoundError):
            await self.service.call_tool(
                ALLOW_ALL,
                "get_studio_state_v2",
                {"studio_id": str(uuid.uuid4())},
            )
        self.assertTrue(valid.transport._queue.empty())

    async def test_representative_operation_families_route_only_to_target(self):
        target = await FakeStudio.create(
            self.registry, "target", self.remote_names
        )
        other = await FakeStudio.create(
            self.registry, "other", self.remote_names
        )
        representatives = {
            "search_game_tree_v2": {},
            "multi_edit_v2": {
                "file_path": "game.ServerScriptService.Example",
                "edits": [],
                "datamodel_type": "Edit",
            },
            "start_stop_play_v2": {"is_start": True},
            "user_keyboard_input_v2": {
                "actions": [{"action": "keyDown", "key": "W"}],
                "datamodel_type": "Client",
            },
            "get_console_output_v2": {},
            "screen_capture_v2": {"capture_id": "mock"},
            "execute_luau_v2": {
                "code": "return 1",
                "datamodel_type": "Edit",
            },
            "wait_job_finished_v2": {"generationId": "mock-job"},
        }
        for tool, payload in representatives.items():
            with self.subTest(tool=tool):
                args = {"studio_id": target.studio_id, **payload}
                task = asyncio.create_task(
                    self.service.call_tool(ALLOW_ALL, tool, args)
                )
                request = await target.next_request()
                self.assertTrue(other.transport._queue.empty())
                self.assertEqual(self.catalog.get(tool).remote_name, request["operation"])
                target.respond(request, {"tool": tool})
                self.assertEqual({"tool": tool}, await task)

    async def test_no_active_selection_tool_or_router_identifier_exists(self):
        public_names = {tool["name"] for tool in self.catalog.tools_for_mcp()}
        public_names.update(tool["name"] for tool in JOB_TOOLS)
        public_names.add(DISCOVERY_TOOL["name"])
        self.assertNotIn("set_active_studio", public_names)
        forbidden_identifiers = {
            "active_studio",
            "default_studio",
            "selected_studio",
            "only_session",
        }
        package = PROJECT_ROOT / "studio_mcp_v2"
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            identifiers = {
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name)
            }
            identifiers.update(
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
            )
            self.assertFalse(
                identifiers & forbidden_identifiers,
                f"forbidden implicit-target identifier in {path}",
            )


if __name__ == "__main__":
    unittest.main()
