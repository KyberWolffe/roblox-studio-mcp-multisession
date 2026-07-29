from __future__ import annotations

import asyncio
import copy
import json
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import (
    AuthenticationError,
    StaleGenerationError,
)
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)
RAW_MODE_PREDICATE_NAMES = (
    "is_studio",
    "is_edit",
    "is_running",
    "is_run_mode",
    "is_server",
    "is_client",
    "edit_mode_active",
)


class Phase2TreeStateIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.service = ProxyService(self.registry, self.catalog)
        self.a = await FakeStudio.create(
            self.registry,
            "Tree A",
            self.catalog.remote_names,
        )
        self.b = await FakeStudio.create(
            self.registry,
            "Tree B",
            self.catalog.remote_names,
        )
        for index, studio in enumerate((self.a, self.b), start=1):
            studio.session.metadata.update(
                {
                    "run_id": "TreeStateIsolationRun000" + str(index),
                    "session_tag": "00000000000" + format(index, "x"),
                    "place_id": 100 + index,
                    "game_id": 200 + index,
                }
            )

    async def call_tree(
        self,
        studio: FakeStudio,
        arguments,
        *,
        client_request_id=None,
    ):
        return await self.service.call_tool(
            ALLOW_ALL,
            "studio_list_tree_v2",
            {
                "studio_id": studio.studio_id,
                **copy.deepcopy(arguments),
            },
            client_request_id=client_request_id,
        )

    @staticmethod
    def valid_tree(
        studio: FakeStudio,
        request,
        *,
        items=None,
    ):
        arguments = request["args"]
        normalized_items = copy.deepcopy(items or [])
        output_bytes = sum(
            len(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            + 1
            for item in normalized_items
        )
        page_size = arguments.get(
            "page_size", arguments.get("max_results", 200)
        )
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": "studio_list_tree",
            "studio_id": studio.studio_id,
            "client_instance_id": studio.client_instance_id,
            "document_epoch": studio.registration.document_epoch,
            "generation": request["generation"],
            "request_id": request["request_id"],
            "root_path": copy.deepcopy(
                arguments.get("root_path", [])
            ),
            "items": normalized_items,
            "truncated": False,
            "has_more": False,
            "continuation_cursor": "",
            "truncation_reason": "complete",
            "max_depth": arguments.get("max_depth", 2),
            "max_results": page_size,
            "page_size": page_size,
            "scan_limit": arguments.get("scan_limit", 2_000),
            "scanned": len(normalized_items),
            "returned": len(normalized_items),
            "output_bytes": output_bytes,
            "name_filter": arguments.get("name_filter", ""),
            "class_filter": arguments.get("class_filter", ""),
            "class_is_a": arguments.get("class_is_a", False),
            "sort_version": "name-class-v1",
            "output_limit_bytes": 600_000,
        }

    @staticmethod
    def valid_state(studio: FakeStudio, mode: str):
        state = {
            "adapter": "studio-mcp-v2-durable-plugin",
            "source": "studio_controller",
            "connected": True,
            "studio_id": studio.studio_id,
            "client_instance_id": studio.client_instance_id,
            "document_epoch": studio.registration.document_epoch,
            "generation": studio.generation,
            "broker_instance_id": (
                "50000000-0000-4000-8000-000000000001"
            ),
            "run_id": studio.session.metadata["run_id"],
            "session_tag": studio.session.metadata["session_tag"],
            "name": studio.name,
            "place_id": studio.session.metadata["place_id"],
            "game_id": studio.session.metadata["game_id"],
            "mode": mode,
            "is_edit": False,
            "mode_source": "play_transition",
            "controller_context": {
                "role": "edit_controller",
                "datamodel_type": "Edit",
                "request_channel_available": True,
            },
            "available_datamodel_types": ["Edit"],
            "raw_mode_predicates": {
                name: {
                    "read_ok": True,
                    "value": (
                        name
                        in {
                            "is_studio",
                            "is_edit",
                            "edit_mode_active",
                        }
                    ),
                }
                for name in RAW_MODE_PREDICATE_NAMES
            },
            "play": {
                "active": mode in {"play", "stopping"},
                "state": mode,
                "accepted": True,
                "server_ready": True,
                "runner_finished": False,
                "transition_nonce": studio.studio_id,
            },
        }
        if mode == "stopping":
            state["play"]["stop_command_id"] = (
                studio.client_instance_id
            )
        return state

    async def test_two_studios_overlap_and_reverse_responses_do_not_swap(
        self,
    ) -> None:
        same_request_id = "phase2-tree-shared-request"
        call_a = asyncio.create_task(
            self.call_tree(
                self.a,
                {"root_path": ["Workspace", "A"]},
                client_request_id=same_request_id,
            )
        )
        call_b = asyncio.create_task(
            self.call_tree(
                self.b,
                {"root_path": ["Workspace", "B"]},
                client_request_id=same_request_id,
            )
        )
        request_a, request_b = await asyncio.gather(
            self.a.next_request(),
            self.b.next_request(),
        )

        self.assertEqual(same_request_id, request_a["request_id"])
        self.assertEqual(same_request_id, request_b["request_id"])
        self.assertEqual(self.a.studio_id, request_a["studio_id"])
        self.assertEqual(self.b.studio_id, request_b["studio_id"])
        self.assertEqual(
            {"root_path": ["Workspace", "A"]},
            request_a["args"],
        )
        self.assertEqual(
            {"root_path": ["Workspace", "B"]},
            request_b["args"],
        )

        result_a = self.valid_tree(self.a, request_a)
        result_b = self.valid_tree(self.b, request_b)
        self.assertTrue(self.b.respond(request_b, result_b))
        self.assertEqual(result_b, await call_b)
        self.assertFalse(call_a.done())
        self.assertTrue(self.a.respond(request_a, result_a))
        self.assertEqual(result_a, await call_a)

    async def test_two_tree_calls_to_one_session_are_fifo_serialized(
        self,
    ) -> None:
        first = asyncio.create_task(
            self.call_tree(
                self.a,
                {"name_filter": "first"},
                client_request_id="phase2-tree-first",
            )
        )
        first_request = await self.a.next_request()
        second = asyncio.create_task(
            self.call_tree(
                self.a,
                {"name_filter": "second"},
                client_request_id="phase2-tree-second",
            )
        )

        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        self.assertFalse(second.done())

        first_result = self.valid_tree(self.a, first_request)
        self.assertTrue(self.a.respond(first_request, first_result))
        self.assertEqual(first_result, await first)
        second_request = await self.a.next_request()
        self.assertEqual(
            "phase2-tree-second",
            second_request["request_id"],
        )
        self.assertEqual(
            {"name_filter": "second"},
            second_request["args"],
        )
        second_result = self.valid_tree(self.a, second_request)
        self.assertTrue(self.a.respond(second_request, second_result))
        self.assertEqual(second_result, await second)

    async def test_tree_query_and_cursor_are_inert_exact_envelope_data(
        self,
    ) -> None:
        query = {
            "root_path": [
                "Workspace",
                "Folder.[literal]$(not-executed)",
            ],
            "max_depth": 6,
            "name_filter": "Door.*[%](); return game",
            "class_filter": "BasePart",
            "class_is_a": True,
            "scan_limit": 4999,
            "page_size": 137,
            "continuation_cursor": (
                "eyJ2IjoxLCJvIjo3fQ==." + ("a" * 64)
            ),
        }
        expected_query = copy.deepcopy(query)
        operation = asyncio.create_task(
            self.call_tree(
                self.a,
                query,
                client_request_id="phase2-tree-inert-query",
            )
        )
        request = await self.a.next_request()

        query["root_path"][1] = "mutated-after-dispatch"
        query["name_filter"] = "mutated-after-dispatch"
        self.assertEqual(
            {
                "v",
                "kind",
                "studio_id",
                "document_epoch",
                "generation",
                "request_id",
                "operation",
                "args",
                "deadline_ms",
            },
            set(request),
        )
        self.assertEqual(2, request["v"])
        self.assertEqual("request", request["kind"])
        self.assertEqual(self.a.studio_id, request["studio_id"])
        self.assertEqual(
            self.a.registration.document_epoch,
            request["document_epoch"],
        )
        self.assertEqual(self.a.generation, request["generation"])
        self.assertEqual(
            "phase2-tree-inert-query",
            request["request_id"],
        )
        self.assertEqual("studio_list_tree", request["operation"])
        self.assertEqual(expected_query, request["args"])
        self.assertNotIn("studio_id", request["args"])
        self.assertEqual(30_000, request["deadline_ms"])
        self.assertTrue(self.b.transport._queue.empty())

        result = self.valid_tree(self.a, request)
        self.assertTrue(self.a.respond(request, result))
        self.assertEqual(result, await operation)

    async def test_reconnect_generation_fences_old_tree_response(
        self,
    ) -> None:
        old_generation = self.a.generation
        operation = asyncio.create_task(
            self.call_tree(
                self.a,
                {"page_size": 1},
                client_request_id="phase2-tree-old-generation",
            )
        )
        request = await self.a.next_request()
        self.assertEqual(old_generation, request["generation"])
        self.assertTrue(
            self.a.respond(
                request,
                self.valid_tree(self.a, request),
            )
        )

        self.assertTrue(self.a.disconnect())
        old_connection = await self.a.reconnect()
        self.assertEqual(old_generation + 1, self.a.generation)
        with self.assertRaises(StaleGenerationError):
            await operation
        self.assertTrue(self.a.transport._queue.empty())

        with self.assertRaises(AuthenticationError):
            self.registry.receive_response(
                old_connection.studio_id,
                old_connection.generation,
                old_connection.resume_token,
                request["request_id"],
                success=True,
                result={"late": True},
            )

    async def test_valid_state_responses_update_only_their_session(
        self,
    ) -> None:
        read_a = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "studio_get_state_v2",
                {"studio_id": self.a.studio_id},
                client_request_id="phase2-state-a",
            )
        )
        read_b = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "studio_get_state_v2",
                {"studio_id": self.b.studio_id},
                client_request_id="phase2-state-b",
            )
        )
        request_a, request_b = await asyncio.gather(
            self.a.next_request(),
            self.b.next_request(),
        )
        state_a = self.valid_state(self.a, "play")
        state_b = self.valid_state(self.b, "stopping")

        self.assertEqual(self.a.studio_id, state_a["studio_id"])
        self.assertEqual(
            self.a.client_instance_id,
            state_a["client_instance_id"],
        )
        self.assertEqual(
            self.a.registration.document_epoch,
            state_a["document_epoch"],
        )
        self.assertEqual(self.a.generation, state_a["generation"])
        self.assertEqual(self.b.studio_id, state_b["studio_id"])
        self.assertEqual(
            self.b.client_instance_id,
            state_b["client_instance_id"],
        )
        self.assertEqual(
            self.b.registration.document_epoch,
            state_b["document_epoch"],
        )
        self.assertEqual(self.b.generation, state_b["generation"])

        self.assertTrue(self.a.respond(request_a, state_a))
        self.assertEqual("play", self.a.session.mode)
        self.assertEqual("play", self.a.session.last_confirmed_mode)
        self.assertEqual("edit", self.b.session.mode)
        self.assertEqual("edit", self.b.session.last_confirmed_mode)
        self.assertEqual(state_a, await read_a)

        self.assertTrue(self.b.respond(request_b, state_b))
        self.assertEqual(state_b, await read_b)
        self.assertEqual("play", self.a.session.mode)
        self.assertEqual("play", self.a.session.last_confirmed_mode)
        self.assertEqual("stopping", self.b.session.mode)
        self.assertEqual("stopping", self.b.session.last_confirmed_mode)


if __name__ == "__main__":
    unittest.main()
