from __future__ import annotations

import asyncio
import unittest

from .helpers import ALLOW_ALL, FakeStudio, make_service


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry, self.catalog, self.service = make_service()
        capabilities = self.catalog.remote_names
        self.a = await FakeStudio.create(self.registry, "A", capabilities)
        self.b = await FakeStudio.create(self.registry, "B", capabilities)

    async def test_different_studios_overlap_without_global_lock(self):
        call_a = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "multi_edit_v2",
                {
                    "studio_id": self.a.studio_id,
                    "file_path": "game.A",
                    "edits": [],
                    "datamodel_type": "Edit",
                },
            )
        )
        request_a = await self.a.next_request()
        call_b = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.b.studio_id, "is_start": True},
            )
        )
        # B reaches its independent transport while A remains held.
        request_b = await self.b.next_request()
        self.assertFalse(call_a.done())
        self.b.respond(request_b, "B finished")
        self.assertEqual("B finished", await call_b)
        self.assertFalse(call_a.done())
        self.a.respond(request_a, "A finished")
        self.assertEqual("A finished", await call_a)

    async def test_session_map_is_not_limited_to_two_studios(self):
        c = await FakeStudio.create(
            self.registry, "C", self.catalog.remote_names
        )
        targets = (self.a, self.b, c)
        calls = [
            asyncio.create_task(
                self.service.call_tool(
                    ALLOW_ALL,
                    "get_console_output_v2",
                    {"studio_id": studio.studio_id},
                )
            )
            for studio in targets
        ]
        requests = await asyncio.gather(
            *(studio.next_request() for studio in targets)
        )
        self.assertEqual(3, self.registry.session_count())
        for studio, request in zip(targets, requests):
            self.assertEqual(studio.studio_id, request["studio_id"])
            studio.respond(request, studio.name)
        self.assertEqual(["A", "B", "C"], await asyncio.gather(*calls))

    async def test_same_studio_calls_are_fifo_serialized(self):
        first = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "multi_edit_v2",
                {
                    "studio_id": self.a.studio_id,
                    "file_path": "game.First",
                    "edits": [],
                    "datamodel_type": "Edit",
                },
            )
        )
        first_request = await self.a.next_request()
        second = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.a.studio_id, "is_start": False},
            )
        )
        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        self.assertFalse(second.done())
        self.a.respond(first_request, "first")
        self.assertEqual("first", await first)
        second_request = await self.a.next_request()
        self.assertEqual("start_stop_play", second_request["operation"])
        self.a.respond(second_request, "second")
        self.assertEqual("second", await second)

    async def test_failure_releases_only_target_session_lock(self):
        first = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "execute_luau_v2",
                {
                    "studio_id": self.a.studio_id,
                    "code": "error('mock')",
                    "datamodel_type": "Edit",
                },
            )
        )
        request = await self.a.next_request()
        queued = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_console_output_v2",
                {"studio_id": self.a.studio_id},
            )
        )
        other = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_console_output_v2",
                {"studio_id": self.b.studio_id},
            )
        )
        other_request = await self.b.next_request()
        self.b.respond(other_request, "B")
        self.assertEqual("B", await other)
        self.a.respond(request, None, success=False, error={"message": "mock failure"})
        with self.assertRaises(Exception):
            await first
        queued_request = await self.a.next_request()
        self.a.respond(queued_request, "A recovered")
        self.assertEqual("A recovered", await queued)

    async def test_play_stop_transitions_are_session_scoped(self):
        play_a = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.a.studio_id, "is_start": True},
            )
        )
        play_request = await self.a.next_request()
        stop_a = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.a.studio_id, "is_start": False},
            )
        )
        play_b = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.b.studio_id, "is_start": True},
            )
        )
        request_b = await self.b.next_request()
        self.assertTrue(self.a.transport._queue.empty())
        self.b.respond(request_b, "B playing")
        await play_b
        self.a.respond(play_request, "A playing")
        await play_a
        stop_request = await self.a.next_request()
        self.a.respond(stop_request, "A stopped")
        await stop_a


if __name__ == "__main__":
    unittest.main()
