from __future__ import annotations

import asyncio
import unittest
import uuid

from studio_mcp_v2.errors import (
    AuthenticationError,
    RequestTimeoutError,
    SessionConflictError,
    SessionDisconnectedError,
    StaleGenerationError,
    ValidationError,
)
from studio_mcp_v2.session import LongPollTransport

from .helpers import ALLOW_ALL, FakeStudio, make_service


class ReconnectAndCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry, self.catalog, self.service = make_service()
        self.a = await FakeStudio.create(
            self.registry, "A", self.catalog.remote_names
        )
        self.b = await FakeStudio.create(
            self.registry, "B", self.catalog.remote_names
        )

    async def test_same_request_id_on_two_studios_is_composite_correlated(self):
        task_a = asyncio.create_task(
            self.a.session.invoke(
                "get_studio_state", {}, 1000, request_id="same-client-id"
            )
        )
        task_b = asyncio.create_task(
            self.b.session.invoke(
                "get_studio_state", {}, 1000, request_id="same-client-id"
            )
        )
        request_a, request_b = await asyncio.gather(
            self.a.next_request(), self.b.next_request()
        )
        self.b.respond(request_b, {"from": "B"})
        self.a.respond(request_a, {"from": "A"})
        self.assertEqual({"from": "A"}, await task_a)
        self.assertEqual({"from": "B"}, await task_b)

    async def test_inflight_and_queued_calls_fail_and_never_replay(self):
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
        old_request = await self.a.next_request()
        queued = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.a.studio_id, "is_start": True},
            )
        )
        await asyncio.sleep(0)
        old_generation = self.a.generation
        old_token = self.a.resume_token
        self.a.disconnect()
        with self.assertRaises(SessionDisconnectedError):
            await first
        await self.a.reconnect()
        self.assertEqual(old_generation + 1, self.a.generation)
        with self.assertRaises((StaleGenerationError, SessionDisconnectedError)):
            await queued
        self.assertTrue(self.a.transport._queue.empty())
        # Old credentials and old responses are fenced after token rotation.
        with self.assertRaises(AuthenticationError):
            self.registry.receive_response(
                self.a.studio_id,
                old_generation,
                old_token,
                old_request["request_id"],
                success=True,
                result="late",
            )
        with self.assertRaises(SessionConflictError):
            await self.service.call_tool(
                ALLOW_ALL,
                "get_studio_state_v2",
                {"studio_id": self.a.studio_id},
            )
        # A trusted Studio adapter must settle its prior request ledger before
        # the broker admits new work after an uncertain disconnect.
        self.a.disconnect()
        await self.a.reconnect(settled_request_ids=[old_request["request_id"]])
        recovered = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_studio_state_v2",
                {"studio_id": self.a.studio_id},
            )
        )
        recovered_request = await self.a.next_request()
        self.a.respond(recovered_request, {"mode": "edit"})
        self.assertEqual({"mode": "edit"}, await recovered)

    async def test_reconnected_session_accepts_new_calls(self):
        self.a.disconnect()
        old = await self.a.reconnect()
        task = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_studio_state_v2",
                {"studio_id": self.a.studio_id},
            )
        )
        request = await self.a.next_request()
        self.assertEqual(self.a.generation, request["generation"])
        self.assertNotEqual(old.generation, request["generation"])
        self.a.respond(request, {"mode": "edit"})
        self.assertEqual({"mode": "edit"}, await task)

    async def test_disconnect_a_does_not_clear_b(self):
        task_a = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_console_output_v2",
                {"studio_id": self.a.studio_id},
            )
        )
        task_b = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_console_output_v2",
                {"studio_id": self.b.studio_id},
            )
        )
        await asyncio.gather(self.a.next_request(), self.b.next_request())
        self.a.disconnect()
        with self.assertRaises(SessionDisconnectedError):
            await task_a
        request_b = next(iter(self.b.session.pending.values()))
        accepted = self.registry.receive_response(
            self.b.studio_id,
            self.b.generation,
            self.b.resume_token,
            request_b.request_id,
            success=True,
            result="B still connected",
        )
        self.assertTrue(accepted)
        self.assertEqual("B still connected", await task_b)

    async def test_stale_lease_expires_only_its_session_and_can_reconnect(self):
        self.a.session.last_seen_monotonic -= (
            self.registry.lease_timeout_seconds + 1
        )
        snapshots = {
            item["studio_id"]: item for item in self.registry.snapshots()
        }
        self.assertFalse(snapshots[self.a.studio_id]["connected"])
        self.assertTrue(snapshots[self.b.studio_id]["connected"])
        with self.assertRaises(SessionDisconnectedError):
            self.registry.require(self.a.studio_id)

        await self.a.reconnect()
        self.assertTrue(self.a.session.connected)
        self.assertTrue(self.b.session.connected)

    async def test_duplicate_claim_requires_rotating_resume_credential(self):
        with self.assertRaises(AuthenticationError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata={"name": "attacker"},
                capabilities=self.catalog.remote_names,
                studio_id=self.a.studio_id,
                resume_token="wrong-token",
            )
        with self.assertRaises(SessionConflictError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch="different-document",
                metadata={"name": "wrong-document"},
                capabilities=self.catalog.remote_names,
                studio_id=self.a.studio_id,
                resume_token=self.a.resume_token,
            )
        old_token = self.a.resume_token
        self.a.disconnect()
        await self.a.reconnect()
        with self.assertRaises(AuthenticationError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata={"name": "stale-peer"},
                capabilities=self.catalog.remote_names,
                studio_id=self.a.studio_id,
                resume_token=old_token,
            )

    async def test_lost_reconnect_response_is_idempotent_until_first_poll(self):
        self.a.session.mark_seen(polled=True)
        old_token = self.a.resume_token
        old_generation = self.a.generation
        self.a.disconnect()
        metadata = {"name": self.a.name, "mode": "edit", "mock": True}
        reconnect_id = str(uuid.uuid4())

        session, replacement = await self.registry.register(
            client_instance_id=self.a.client_instance_id,
            registration_secret=self.a.registration_secret,
            document_epoch=self.a.registration.document_epoch,
            metadata=metadata,
            capabilities=self.a.capabilities,
            studio_id=self.a.studio_id,
            resume_token=old_token,
            reconnect_id=reconnect_id,
            transport=LongPollTransport(),
        )
        _, retry = await self.registry.register(
            client_instance_id=self.a.client_instance_id,
            registration_secret=self.a.registration_secret,
            document_epoch=self.a.registration.document_epoch,
            metadata=metadata,
            capabilities=self.a.capabilities,
            studio_id=self.a.studio_id,
            resume_token=old_token,
            reconnect_id=reconnect_id,
            transport=LongPollTransport(),
        )

        self.assertEqual(old_generation + 1, replacement.generation)
        self.assertEqual(replacement.generation, retry.generation)
        self.assertEqual(replacement.resume_token, retry.resume_token)
        with self.assertRaises(AuthenticationError):
            self.registry.authenticate_studio(
                self.a.studio_id,
                replacement.generation,
                old_token,
            )

        session.mark_seen(polled=True)
        with self.assertRaises(AuthenticationError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata=metadata,
                capabilities=self.a.capabilities,
                studio_id=self.a.studio_id,
                resume_token=old_token,
                reconnect_id=reconnect_id,
                transport=LongPollTransport(),
            )
        self.assertTrue(
            self.registry.disconnect(
                self.a.studio_id,
                replacement.generation,
                replacement.resume_token,
                "exercise reconnect-id replay fence",
            )
        )
        with self.assertRaises(SessionConflictError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata=metadata,
                capabilities=self.a.capabilities,
                studio_id=self.a.studio_id,
                resume_token=replacement.resume_token,
                reconnect_id=reconnect_id,
                transport=LongPollTransport(),
            )

    async def test_timeout_quarantines_until_late_response_settles(self):
        timed_out = asyncio.create_task(
            self.a.session.invoke(
                "multi_edit",
                {"file_path": "game.A", "edits": [], "datamodel_type": "Edit"},
                10,
            )
        )
        request = await self.a.next_request()
        with self.assertRaises(RequestTimeoutError):
            await timed_out
        with self.assertRaises(SessionConflictError):
            await self.service.call_tool(
                ALLOW_ALL,
                "get_studio_state_v2",
                {"studio_id": self.a.studio_id},
            )
        self.assertTrue(self.a.respond(request, "late but terminal"))
        with self.assertRaises(StaleGenerationError):
            await self.a.session.invoke(
                "get_studio_state",
                {},
                1000,
                request_id=request["request_id"],
            )
        recovered = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "get_studio_state_v2",
                {"studio_id": self.a.studio_id},
            )
        )
        recovered_request = await self.a.next_request()
        self.a.respond(recovered_request, {"mode": "edit"})
        self.assertEqual({"mode": "edit"}, await recovered)

    async def test_response_then_reconnect_race_cannot_mutate_new_generation(self):
        operation = asyncio.create_task(
            self.a.session.invoke(
                "start_stop_play",
                {"is_start": True},
                1000,
                request_id="race-request",
            )
        )
        request = await self.a.next_request()
        self.assertTrue(self.a.respond(request, "old generation completed"))
        # Registry reconnect has no internal suspension, so it fences the old
        # generation before the woken invoke coroutine can observe its result.
        self.a.disconnect()
        await self.a.reconnect()
        with self.assertRaises(StaleGenerationError):
            await operation
        self.assertEqual("edit", self.a.session.mode)

    async def test_cancel_after_dispatch_quarantines_conflicting_work(self):
        operation = asyncio.create_task(
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
        request = await self.a.next_request()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        with self.assertRaises(SessionConflictError):
            await self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.a.studio_id, "is_start": True},
            )
        self.assertTrue(self.a.respond(request, "late terminal response"))

    async def test_mode_event_after_response_wins_arrival_order(self):
        operation = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "start_stop_play_v2",
                {"studio_id": self.a.studio_id, "is_start": True},
            )
        )
        request = await self.a.next_request()
        self.assertTrue(self.a.respond(request, "started"))
        self.assertEqual("play", self.a.session.mode)
        self.assertTrue(self.a.event("mode", {"mode": "edit"}))
        await operation
        self.assertEqual("edit", self.a.session.mode)

    async def test_registration_identity_is_idempotent_then_unique(self):
        first_id = self.a.studio_id
        retry_session, retry = await self.registry.register(
            client_instance_id=self.a.client_instance_id,
            registration_secret=self.a.registration_secret,
            document_epoch=self.a.registration.document_epoch,
            metadata={"name": "A retry"},
            capabilities=self.catalog.remote_names,
        )
        self.assertEqual(first_id, retry.studio_id)
        self.assertEqual(self.a.resume_token, retry.resume_token)
        self.assertEqual(2, self.registry.session_count())  # Includes B.
        self.a.session.mark_seen(polled=True)
        with self.assertRaises(SessionConflictError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata={"name": "duplicate live A"},
                capabilities=self.catalog.remote_names,
            )
        old_token = self.a.resume_token
        old_epoch = self.a.registration.document_epoch
        self.a.disconnect()
        _, replacement = await self.registry.register(
            client_instance_id=self.a.client_instance_id,
            registration_secret=self.a.registration_secret,
            document_epoch="new-document-epoch",
            metadata={"name": "A new document"},
            capabilities=self.catalog.remote_names,
        )
        self.assertNotEqual(first_id, replacement.studio_id)
        with self.assertRaises(SessionConflictError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=old_epoch,
                metadata={"name": "retired A"},
                capabilities=self.catalog.remote_names,
                studio_id=first_id,
                resume_token=old_token,
            )

    async def test_live_generation_takeover_is_rejected(self):
        with self.assertRaises(SessionConflictError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata={"name": "duplicate live peer"},
                capabilities=self.catalog.remote_names,
                studio_id=self.a.studio_id,
                resume_token=self.a.resume_token,
                reconnect_id=str(uuid.uuid4()),
            )

    async def test_invalid_settlement_list_cannot_disconnect_live_session(self):
        original_generation = self.a.generation
        with self.assertRaises(ValidationError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch=self.a.registration.document_epoch,
                metadata={"name": "bad reconnect"},
                capabilities=self.catalog.remote_names,
                studio_id=self.a.studio_id,
                resume_token=self.a.resume_token,
                settled_request_ids=None,
                transport=LongPollTransport(),
            )
        self.assertTrue(self.a.session.connected)
        self.assertEqual(original_generation, self.a.session.generation)

    async def test_document_rollover_requires_old_outcome_settlement(self):
        operation = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "multi_edit_v2",
                {
                    "studio_id": self.a.studio_id,
                    "file_path": "game.OldDocument",
                    "edits": [],
                    "datamodel_type": "Edit",
                },
            )
        )
        request = await self.a.next_request()
        self.a.disconnect()
        with self.assertRaises(SessionDisconnectedError):
            await operation
        with self.assertRaises(SessionConflictError):
            await self.registry.register(
                client_instance_id=self.a.client_instance_id,
                registration_secret=self.a.registration_secret,
                document_epoch="replacement-document",
                metadata={"name": "replacement"},
                capabilities=self.catalog.remote_names,
            )
        _, replacement = await self.registry.register(
            client_instance_id=self.a.client_instance_id,
            registration_secret=self.a.registration_secret,
            document_epoch="replacement-document",
            metadata={"name": "replacement"},
            capabilities=self.catalog.remote_names,
            settled_request_ids=[request["request_id"]],
        )
        self.assertNotEqual(self.a.studio_id, replacement.studio_id)


if __name__ == "__main__":
    unittest.main()
