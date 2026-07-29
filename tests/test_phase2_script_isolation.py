from __future__ import annotations

import asyncio
import copy
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import (
    AuthenticationError,
    StaleGenerationError,
    ValidationError,
)
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = PROJECT_ROOT / "config" / "durable-tool-catalog.json"
SEARCH_REMOTE = "studio_search_scripts"
GREP_REMOTE = "studio_grep_scripts"
SEARCH_PUBLIC = SEARCH_REMOTE + "_v2"
GREP_PUBLIC = GREP_REMOTE + "_v2"


class Phase2ScriptIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        required = {SEARCH_REMOTE, GREP_REMOTE}
        if not required <= self.catalog.remote_names:
            self.skipTest(
                "script search/grep catalog entries are landing concurrently"
            )
        self.service = ProxyService(self.registry, self.catalog)
        self.a = await FakeStudio.create(
            self.registry,
            "Script Search A",
            self.catalog.remote_names,
        )
        self.b = await FakeStudio.create(
            self.registry,
            "Script Search B",
            self.catalog.remote_names,
        )

    async def call(
        self,
        studio: FakeStudio,
        public_name: str,
        arguments,
        *,
        request_id=None,
    ):
        return await self.service.call_tool(
            ALLOW_ALL,
            public_name,
            {
                "studio_id": studio.studio_id,
                **copy.deepcopy(arguments),
            },
            client_request_id=request_id,
        )

    @staticmethod
    def valid_empty_result(studio: FakeStudio, request):
        args = request["args"]
        operation = request["operation"]
        root_path = copy.deepcopy(args.get("root_path", []))
        common = {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": operation,
            "studio_id": studio.studio_id,
            "client_instance_id": studio.client_instance_id,
            "document_epoch": studio.registration.document_epoch,
            "generation": studio.generation,
            "request_id": request["request_id"],
            "root_path": root_path,
            "sort_version": "name-class-v1",
            "max_depth": args.get(
                "max_depth",
                64 - len(root_path),
            ),
            "scan_limit": args.get("scan_limit", 2_000),
            "page_size": args.get(
                "page_size",
                10 if operation == SEARCH_REMOTE else 50,
            ),
            "time_limit_ms": args.get(
                "time_limit_ms",
                3_000 if operation == SEARCH_REMOTE else 5_000,
            ),
            "items": [],
            "returned": 0,
            "scanned_instances": 0,
            "scanned_scripts": 0,
            "truncated": False,
            "has_more": False,
            "continuation_cursor": "",
            "truncation_reason": "complete",
            "output_limit_bytes": (
                200_000 if operation == SEARCH_REMOTE else 500_000
            ),
        }
        if operation == SEARCH_REMOTE:
            common.update(
                {
                    "keywords": [
                        token.strip(" ").lower()
                        for token in args["keywords"].split(",")
                    ],
                    "match_semantics": (
                        "all_keywords_ascii_case_insensitive_"
                        "literal_subsequence"
                    ),
                    "query_version": "script-name-query-v1",
                }
            )
        else:
            common.update(
                {
                    "query": args["query"],
                    "match_mode": "literal",
                    "case_sensitive": args.get(
                        "case_sensitive",
                        True,
                    ),
                    "query_version": "script-grep-query-v1",
                    "source_byte_limit": args.get(
                        "source_byte_limit",
                        1_048_576,
                    ),
                    "source_bytes_scanned": 0,
                }
            )
        return common

    async def test_cross_session_overlap_reverse_response_and_exact_args(
        self,
    ) -> None:
        search_args = {
            "keywords": "door,controller",
            "root_path": ["ServerScriptService"],
            "max_depth": 8,
            "scan_limit": 97,
            "page_size": 3,
            "time_limit_ms": 1_500,
        }
        grep_args = {
            "query": "DoorController:GetState()",
            "root_path": ["ReplicatedStorage"],
            "max_depth": 9,
            "case_sensitive": True,
            "scan_limit": 101,
            "source_byte_limit": 524_288,
            "page_size": 7,
            "time_limit_ms": 2_000,
        }
        shared_request_id = "phase2-script-shared-request"
        search = asyncio.create_task(
            self.call(
                self.a,
                SEARCH_PUBLIC,
                search_args,
                request_id=shared_request_id,
            )
        )
        grep = asyncio.create_task(
            self.call(
                self.b,
                GREP_PUBLIC,
                grep_args,
                request_id=shared_request_id,
            )
        )
        request_a, request_b = await asyncio.gather(
            self.a.next_request(),
            self.b.next_request(),
        )

        self.assertEqual(SEARCH_REMOTE, request_a["operation"])
        self.assertEqual(GREP_REMOTE, request_b["operation"])
        self.assertEqual(self.a.studio_id, request_a["studio_id"])
        self.assertEqual(self.b.studio_id, request_b["studio_id"])
        self.assertEqual(self.a.generation, request_a["generation"])
        self.assertEqual(self.b.generation, request_b["generation"])
        self.assertEqual(shared_request_id, request_a["request_id"])
        self.assertEqual(shared_request_id, request_b["request_id"])
        self.assertEqual(search_args, request_a["args"])
        self.assertEqual(grep_args, request_b["args"])
        for request in (request_a, request_b):
            self.assertNotIn("studio_id", request["args"])
            self.assertNotIn("active_studio", request["args"])
            self.assertNotIn("default_studio", request["args"])

        result_a = self.valid_empty_result(self.a, request_a)
        result_b = self.valid_empty_result(self.b, request_b)
        self.assertTrue(self.b.respond(request_b, result_b))
        self.assertEqual(result_b, await grep)
        self.assertFalse(search.done())
        self.assertTrue(self.a.respond(request_a, result_a))
        self.assertEqual(result_a, await search)

    async def test_same_session_search_then_grep_is_fifo(self) -> None:
        first = asyncio.create_task(
            self.call(
                self.a,
                SEARCH_PUBLIC,
                {"keywords": "first"},
                request_id="phase2-script-first",
            )
        )
        first_request = await self.a.next_request()
        second = asyncio.create_task(
            self.call(
                self.a,
                GREP_PUBLIC,
                {"query": "second"},
                request_id="phase2-script-second",
            )
        )
        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        self.assertFalse(second.done())

        first_result = self.valid_empty_result(self.a, first_request)
        self.assertTrue(self.a.respond(first_request, first_result))
        self.assertEqual(first_result, await first)
        second_request = await self.a.next_request()
        self.assertEqual(GREP_REMOTE, second_request["operation"])
        self.assertEqual(
            "phase2-script-second",
            second_request["request_id"],
        )
        self.assertEqual({"query": "second"}, second_request["args"])
        second_result = self.valid_empty_result(self.a, second_request)
        self.assertTrue(self.a.respond(second_request, second_result))
        self.assertEqual(second_result, await second)

    async def test_reconnect_generation_fences_old_script_response(
        self,
    ) -> None:
        old_generation = self.a.generation
        operation = asyncio.create_task(
            self.call(
                self.a,
                SEARCH_PUBLIC,
                {"keywords": "generation"},
                request_id="phase2-script-old-generation",
            )
        )
        request = await self.a.next_request()
        self.assertEqual(old_generation, request["generation"])
        result = self.valid_empty_result(self.a, request)
        self.assertTrue(self.a.respond(request, result))

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

    async def test_missing_or_symbolic_target_never_routes(self) -> None:
        with self.assertRaises(ValidationError):
            await self.service.call_tool(
                ALLOW_ALL,
                SEARCH_PUBLIC,
                {"keywords": "missing target"},
            )
        for symbolic in ("active", "default", "global"):
            with self.subTest(symbolic=symbolic):
                with self.assertRaises(ValidationError):
                    await self.service.call_tool(
                        ALLOW_ALL,
                        GREP_PUBLIC,
                        {
                            "studio_id": symbolic,
                            "query": "must not route",
                        },
                    )
        self.assertTrue(self.a.transport._queue.empty())
        self.assertTrue(self.b.transport._queue.empty())


if __name__ == "__main__":
    unittest.main()
