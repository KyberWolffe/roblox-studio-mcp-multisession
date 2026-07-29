from __future__ import annotations

import asyncio
import copy
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)
SEARCH_PUBLIC = "studio_search_scripts_v2"


class Phase2JobFifoIsolationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.service = ProxyService(self.registry, self.catalog)
        self.a = await FakeStudio.create(
            self.registry, "FIFO A", self.catalog.remote_names
        )
        self.b = await FakeStudio.create(
            self.registry, "FIFO B", self.catalog.remote_names
        )

    @staticmethod
    def arguments(label: str):
        return {
            "keywords": label,
            "root_path": ["Workspace"],
            "max_depth": 3,
            "scan_limit": 10,
            "page_size": 2,
            "time_limit_ms": 1_000,
        }

    @staticmethod
    def result(studio: FakeStudio, request):
        arguments = request["args"]
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": "studio_search_scripts",
            "studio_id": studio.studio_id,
            "client_instance_id": studio.client_instance_id,
            "document_epoch": studio.registration.document_epoch,
            "generation": studio.generation,
            "request_id": request["request_id"],
            "root_path": copy.deepcopy(arguments["root_path"]),
            "sort_version": "name-class-v1",
            "max_depth": arguments["max_depth"],
            "scan_limit": arguments["scan_limit"],
            "page_size": arguments["page_size"],
            "time_limit_ms": arguments["time_limit_ms"],
            "items": [],
            "returned": 0,
            "scanned_instances": 0,
            "scanned_scripts": 0,
            "truncated": False,
            "has_more": False,
            "continuation_cursor": "",
            "truncation_reason": "complete",
            "output_limit_bytes": 200_000,
            "keywords": [arguments["keywords"].lower()],
            "match_semantics": (
                "all_keywords_ascii_case_insensitive_"
                "literal_subsequence"
            ),
            "query_version": "script-name-query-v1",
        }

    async def direct(self, studio: FakeStudio, label: str):
        return await self.service.call_tool(
            ALLOW_ALL,
            SEARCH_PUBLIC,
            {
                "studio_id": studio.studio_id,
                **self.arguments(label),
            },
        )

    async def test_direct_then_job_is_same_session_fifo(self) -> None:
        first = asyncio.create_task(self.direct(self.a, "First"))
        first_request = await self.a.next_request()
        second = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            SEARCH_PUBLIC,
            self.arguments("Second"),
            1_000,
        )
        second_record = self.a.session.jobs[second["job_id"]]

        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        first_result = self.result(self.a, first_request)
        self.assertTrue(
            self.a.respond(first_request, first_result)
        )
        self.assertEqual(first_result, await first)
        second_request = await self.a.next_request()
        self.assertEqual(
            "Second", second_request["args"]["keywords"]
        )
        self.assertTrue(
            self.a.respond(
                second_request,
                self.result(self.a, second_request),
            )
        )
        await second_record.task
        self.assertEqual("completed", second_record.status)

    async def test_job_then_direct_is_same_session_fifo(self) -> None:
        first = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            SEARCH_PUBLIC,
            self.arguments("First"),
            1_000,
        )
        first_record = self.a.session.jobs[first["job_id"]]
        second = asyncio.create_task(self.direct(self.a, "Second"))

        first_request = await self.a.next_request()
        self.assertEqual(
            "First", first_request["args"]["keywords"]
        )
        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        self.assertTrue(
            self.a.respond(
                first_request,
                self.result(self.a, first_request),
            )
        )
        await first_record.task
        second_request = await self.a.next_request()
        self.assertEqual(
            "Second", second_request["args"]["keywords"]
        )
        second_result = self.result(self.a, second_request)
        self.assertTrue(
            self.a.respond(second_request, second_result)
        )
        self.assertEqual(second_result, await second)

    async def test_different_sessions_job_and_direct_overlap(self) -> None:
        held = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            SEARCH_PUBLIC,
            self.arguments("Held"),
            1_000,
        )
        held_record = self.a.session.jobs[held["job_id"]]
        independent = asyncio.create_task(
            self.direct(self.b, "Independent")
        )

        request_a, request_b = await asyncio.gather(
            self.a.next_request(), self.b.next_request()
        )
        self.assertFalse(held_record.task.done())
        result_b = self.result(self.b, request_b)
        self.assertTrue(self.b.respond(request_b, result_b))
        self.assertEqual(result_b, await independent)
        self.assertFalse(held_record.task.done())

        self.assertTrue(
            self.a.respond(
                request_a, self.result(self.a, request_a)
            )
        )
        await held_record.task
        self.assertEqual("completed", held_record.status)

    async def test_two_jobs_have_monotonic_admission_and_dispatch_order(
        self,
    ) -> None:
        first = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            SEARCH_PUBLIC,
            self.arguments("First"),
            1_000,
        )
        second = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            SEARCH_PUBLIC,
            self.arguments("Second"),
            1_000,
        )
        self.assertLess(
            first["admission_sequence"],
            second["admission_sequence"],
        )
        first_record = self.a.session.jobs[first["job_id"]]
        second_record = self.a.session.jobs[second["job_id"]]

        first_request = await self.a.next_request()
        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        self.assertTrue(
            self.a.respond(
                first_request,
                self.result(self.a, first_request),
            )
        )
        await first_record.task
        second_request = await self.a.next_request()
        self.assertEqual(
            "Second", second_request["args"]["keywords"]
        )
        self.assertTrue(
            self.a.respond(
                second_request,
                self.result(self.a, second_request),
            )
        )
        await second_record.task


if __name__ == "__main__":
    unittest.main()
