from __future__ import annotations

import asyncio
import secrets
import unittest
import uuid

from studio_mcp_v2.errors import (
    AuthenticationError,
    RequestTimeoutError,
    SessionConflictError,
    ValidationError,
)
from studio_mcp_v2.play_bridge import PlayBridgeManager
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.session import LongPollTransport


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class PlayBridgeStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.manager = PlayBridgeManager(
            clock=self.clock,
            default_ttl_seconds=10,
            stop_watchdog_seconds=3,
            token_key=b"k" * 32,
        )
        self.studio_id = str(uuid.uuid4())
        self.client_instance_id = str(uuid.uuid4())
        self.context = (
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            "play-request",
            101,
            201,
        )

    def prepare(self):
        return self.manager.prepare(*self.context)

    def attach(self, prepared, attach_id="attach-a", server_id="server-a"):
        full = self.context + (prepared["transition_nonce"],)
        return self.manager.attach(
            *full,
            attach_id,
            server_id,
            prepared["bridge_token"],
        )

    def test_full_lifecycle_is_idempotent_and_burns_bootstrap(self):
        prepared = self.prepare()
        retried = self.prepare()
        self.assertEqual(
            prepared["transition_nonce"], retried["transition_nonce"]
        )
        self.assertEqual(prepared["bridge_token"], retried["bridge_token"])
        self.assertTrue(retried["idempotent"])

        attached = self.attach(prepared)
        attached_retry = self.attach(prepared)
        self.assertEqual(
            attached["server_token"], attached_retry["server_token"]
        )
        full = self.context + (prepared["transition_nonce"],)
        armed = self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        self.assertTrue(armed["watchdog_armed"])
        self.assertTrue(armed["bootstrap_burned"])
        with self.assertRaises(AuthenticationError):
            self.attach(prepared)

        stopped = self.manager.request_stop(*full, "stop-request")
        stopped_retry = self.manager.request_stop(*full, "stop-request")
        self.assertEqual(
            stopped["stop_command_id"], stopped_retry["stop_command_id"]
        )
        polled = self.manager.server_poll(
            *full, "server-a", attached["server_token"]
        )
        self.assertEqual("stop", polled["command"])
        acked = self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "stop-ack",
            stopped["stop_command_id"],
            attached["server_token"],
        )
        self.assertEqual("stop_acked", acked["state"])

        completed = self.manager.complete(
            *full,
            "completion",
            "stopped_edit_confirmed",
            stopped["stop_command_id"],
            True,
            2,
            True,
        )
        completed_retry = self.manager.complete(
            *full,
            "completion",
            "stopped_edit_confirmed",
            stopped["stop_command_id"],
            True,
            2,
            True,
        )
        self.assertEqual("completed", completed["state"])
        self.assertEqual("completion", completed["completion_id"])
        self.assertEqual(
            stopped["stop_command_id"],
            completed["end_test_correlation"],
        )
        self.assertTrue(completed_retry["idempotent"])
        reconnect_view = self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="Studio connection generation was replaced",
        )
        self.assertEqual("completed", reconnect_view["state"])
        self.assertEqual("completion", reconnect_view["completion_id"])
        completion_after_reconnect = self.manager.complete(
            *full,
            "completion",
            "stopped_edit_confirmed",
            stopped["stop_command_id"],
            True,
            2,
            True,
        )
        self.assertTrue(completion_after_reconnect["idempotent"])
        status = self.manager.status(*full)
        self.assertNotIn("bridge_token", status)
        self.assertNotIn("server_token", status)

    def test_pre_attach_abort_is_strict_terminal_and_idempotent(self):
        prepared = self.prepare()
        full = self.context + (prepared["transition_nonce"],)
        aborted = self.manager.abort_pre_attach(
            *full,
            "abort-a",
            False,
            True,
        )
        retried = self.manager.abort_pre_attach(
            *full,
            "abort-a",
            False,
            True,
        )
        self.assertEqual("completed", aborted["state"])
        self.assertEqual("pre_attach_aborted", aborted["completion_outcome"])
        self.assertEqual("abort-a", aborted["completion_id"])
        self.assertTrue(retried["idempotent"])
        self.assertTrue(aborted["bootstrap_burned"] is False)
        reconnect_view = self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="Studio connection generation was replaced",
        )
        self.assertEqual("completed", reconnect_view["state"])
        self.assertEqual("abort-a", reconnect_view["completion_id"])
        abort_after_reconnect = self.manager.abort_pre_attach(
            *full,
            "abort-a",
            False,
            True,
        )
        self.assertTrue(abort_after_reconnect["idempotent"])
        with self.assertRaises(SessionConflictError):
            self.manager.abort_pre_attach(
                *full,
                "abort-b",
                False,
                True,
            )
        with self.assertRaises(AuthenticationError):
            self.manager.attach(
                *full,
                "attach-after-abort",
                "server-after-abort",
                prepared["bridge_token"],
            )

        next_context = (
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            "next-play-request",
            101,
            201,
        )
        next_prepared = self.manager.prepare(*next_context)
        self.assertEqual("prepared", next_prepared["state"])

    def test_pre_attach_abort_rejects_runner_or_server_progress(self):
        prepared = self.prepare()
        full = self.context + (prepared["transition_nonce"],)
        with self.assertRaises(ValidationError):
            self.manager.abort_pre_attach(
                *full,
                "abort-runner-started",
                True,
                True,
            )
        with self.assertRaises(ValidationError):
            self.manager.abort_pre_attach(
                *full,
                "abort-script-not-clean",
                False,
                False,
            )
        self.attach(prepared)
        with self.assertRaises(SessionConflictError):
            self.manager.abort_pre_attach(
                *full,
                "abort-after-attach",
                False,
                True,
            )

    def test_uncommitted_pre_attach_abort_is_reconnect_fenced(self):
        prepared = self.prepare()
        full = self.context + (prepared["transition_nonce"],)
        recovered = self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="Studio connection generation was replaced",
        )
        self.assertEqual("stop_requested", recovered["state"])
        with self.assertRaises(SessionConflictError):
            self.manager.abort_pre_attach(
                *full,
                "late-abort",
                False,
                True,
            )

    def test_stale_context_and_replays_fail_closed(self):
        prepared = self.prepare()
        full = self.context + (prepared["transition_nonce"],)
        with self.assertRaises(SessionConflictError):
            self.manager.status(
                self.studio_id,
                self.client_instance_id,
                "wrong-document",
                1,
                "play-request",
                101,
                201,
                prepared["transition_nonce"],
            )
        with self.assertRaises(AuthenticationError):
            self.manager.attach(
                *full, "attach-a", "server-a", "x" * 48
            )
        attached = self.attach(prepared)
        with self.assertRaises(SessionConflictError):
            self.manager.attach(
                *full,
                "different-attach",
                "server-b",
                prepared["bridge_token"],
            )
        with self.assertRaises(AuthenticationError):
            self.manager.server_poll(
                *full, "server-b", attached["server_token"]
            )
        with self.assertRaises(SessionConflictError):
            self.manager.prepare(
                self.studio_id,
                self.client_instance_id,
                "document-a",
                1,
                "second-play",
                101,
                201,
            )

    def test_watchdogs_request_stop_but_never_claim_edit_completion(self):
        prepared = self.prepare()
        attached = self.attach(prepared)
        full = self.context + (prepared["transition_nonce"],)
        self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        self.clock.advance(11)
        polled = self.manager.server_poll(
            *full, "server-a", attached["server_token"]
        )
        self.assertEqual("stop", polled["command"])
        self.assertEqual("play_ttl_watchdog", polled["stop_source"])
        self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "stop-ack",
            polled["stop_command_id"],
            attached["server_token"],
        )
        self.clock.advance(4)
        changed = self.manager.sweep_watchdogs()[self.studio_id]
        self.assertEqual("stop_acked", changed["state"])
        self.assertEqual(
            "edit_completion_watchdog_expired",
            changed["watchdog_expired_reason"],
        )
        with self.assertRaises(SessionConflictError):
            self.manager.assert_document_can_retire(
                self.studio_id, self.client_instance_id
            )

    def test_recovery_retains_only_stop_path_until_proven_complete(self):
        prepared = self.prepare()
        attached = self.attach(prepared)
        full = self.context + (prepared["transition_nonce"],)
        recovered = self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="controller disconnected",
        )
        self.assertTrue(recovered["recovery_only"])
        polled = self.manager.server_poll(
            *full, "server-a", attached["server_token"]
        )
        self.assertEqual("stop", polled["command"])
        armed = self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "late-watchdog",
            None,
            attached["server_token"],
        )
        self.assertTrue(armed["watchdog_armed"])
        with self.assertRaises(SessionConflictError):
            self.manager.complete(
                *full,
                "recovery-complete",
                "stopped_edit_confirmed",
                polled["stop_command_id"],
                True,
                3,
                True,
            )
        self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "recovery-stop-ack",
            polled["stop_command_id"],
            attached["server_token"],
        )
        self.manager.complete(
            *full,
            "recovery-complete",
            "stopped_edit_confirmed",
            polled["stop_command_id"],
            True,
            3,
            True,
        )
        self.manager.assert_document_can_retire(
            self.studio_id, self.client_instance_id
        )

    def test_recovery_after_stop_ack_preserves_completion_path(self):
        prepared = self.prepare()
        attached = self.attach(prepared)
        full = self.context + (prepared["transition_nonce"],)
        self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        stopped = self.manager.request_stop(*full, "stop-request")
        self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "stop-ack",
            stopped["stop_command_id"],
            attached["server_token"],
        )

        recovered = self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="controller disconnected after stop acknowledgement",
        )
        self.assertEqual("stop_acked", recovered["state"])
        self.assertTrue(recovered["recovery_only"])
        completed = self.manager.complete(
            *full,
            "completion-after-recovery",
            "stopped_edit_confirmed",
            stopped["stop_command_id"],
            True,
            3,
            True,
        )
        self.assertEqual("completed", completed["state"])

    def test_completion_outcome_cannot_bypass_required_ack_chain(self):
        prepared = self.prepare()
        attached = self.attach(prepared)
        full = self.context + (prepared["transition_nonce"],)

        with self.assertRaises(SessionConflictError):
            self.manager.complete(
                *full,
                "natural-without-watchdog",
                "natural_stop_edit_confirmed",
                prepared["transition_nonce"],
                True,
                3,
            True,
        )

    def test_recovery_natural_completion_requires_undelivered_stop(self):
        prepared = self.prepare()
        attached = self.attach(prepared)
        full = self.context + (prepared["transition_nonce"],)
        self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        recovered = self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="controller disconnected",
        )
        self.assertEqual("stop_requested", recovered["state"])
        self.assertFalse(recovered["stop_acked"])
        completed = self.manager.complete(
            *full,
            "recovery-natural-completion",
            "recovery_natural_stop_edit_confirmed",
            prepared["transition_nonce"],
            True,
            3,
            True,
        )
        self.assertEqual("completed", completed["state"])
        self.assertEqual(
            "recovery_natural_stop_edit_confirmed",
            completed["completion_outcome"],
        )

    def test_recovery_natural_completion_rejects_delivered_stop(self):
        prepared = self.prepare()
        attached = self.attach(prepared)
        full = self.context + (prepared["transition_nonce"],)
        self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        self.manager.enter_recovery(
            self.studio_id,
            self.client_instance_id,
            "document-a",
            1,
            reason="controller disconnected",
        )
        polled = self.manager.server_poll(
            *full, "server-a", attached["server_token"]
        )
        self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "stop-ack",
            polled["stop_command_id"],
            attached["server_token"],
        )
        with self.assertRaises(SessionConflictError):
            self.manager.complete(
                *full,
                "invalid-recovery-natural-completion",
                "recovery_natural_stop_edit_confirmed",
                prepared["transition_nonce"],
                True,
                3,
                True,
            )

        self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        stopped = self.manager.request_stop(*full, "stop-request")
        with self.assertRaises(SessionConflictError):
            self.manager.complete(
                *full,
                "natural-with-pending-stop",
                "natural_stop_edit_confirmed",
                prepared["transition_nonce"],
                True,
                3,
                True,
            )
        with self.assertRaises(SessionConflictError):
            self.manager.complete(
                *full,
                "failed-after-attach",
                "start_failed_edit_confirmed",
                "play-request",
                True,
                3,
                True,
            )

        self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "stop-ack",
            stopped["stop_command_id"],
            attached["server_token"],
        )
        completed = self.manager.complete(
            *full,
            "proper-stop",
            "stopped_edit_confirmed",
            stopped["stop_command_id"],
            True,
            3,
            True,
        )
        self.assertEqual("completed", completed["state"])

    def test_stop_before_attach_can_ack_watchdog_then_stop(self):
        prepared = self.prepare()
        full = self.context + (prepared["transition_nonce"],)
        stopped = self.manager.request_stop(*full, "stop-before-attach")
        attached = self.attach(prepared)
        armed = self.manager.server_ack(
            *full,
            "server-a",
            "watchdog_armed",
            "watchdog-ack",
            None,
            attached["server_token"],
        )
        self.assertTrue(armed["watchdog_armed"])
        polled = self.manager.server_poll(
            *full, "server-a", attached["server_token"]
        )
        self.assertEqual(stopped["stop_command_id"], polled["stop_command_id"])
        self.manager.server_ack(
            *full,
            "server-a",
            "stop_received",
            "stop-ack",
            stopped["stop_command_id"],
            attached["server_token"],
        )
        completed = self.manager.complete(
            *full,
            "completion",
            "stopped_edit_confirmed",
            stopped["stop_command_id"],
            True,
            2,
            True,
        )
        self.assertEqual("completed", completed["state"])

    def test_two_studios_with_same_request_id_are_independent(self):
        other_studio = str(uuid.uuid4())
        other_client = str(uuid.uuid4())
        other_context = (
            other_studio,
            other_client,
            "document-b",
            1,
            "play-request",
            102,
            202,
        )
        first = self.prepare()
        second = self.manager.prepare(*other_context)
        first_full = self.context + (first["transition_nonce"],)
        second_full = other_context + (second["transition_nonce"],)
        self.manager.request_stop(*first_full, "same-stop-id")
        self.assertEqual(
            "stop_requested", self.manager.status(*first_full)["state"]
        )
        self.assertEqual(
            "prepared", self.manager.status(*second_full)["state"]
        )

    def test_completion_requires_closed_positive_proof(self):
        prepared = self.prepare()
        full = self.context + (prepared["transition_nonce"],)
        with self.assertRaises(ValidationError):
            self.manager.complete(
                *full,
                "completion",
                "start_failed_edit_confirmed",
                "play-request",
                False,
                2,
                True,
            )
        with self.assertRaises(ValidationError):
            self.manager.complete(
                *full,
                "completion",
                "start_failed_edit_confirmed",
                "play-request",
                True,
                1,
                True,
            )
        with self.assertRaises(SessionConflictError):
            self.manager.complete(
                *full,
                "completion",
                "start_failed_edit_confirmed",
                "wrong-request",
                True,
                2,
                True,
            )


class RegistryPlayBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_pre_attach_abort_clears_recovery_gate(self):
        manager = PlayBridgeManager(token_key=b"a" * 32)
        registry = SessionRegistry(play_bridges=manager)
        transport = LongPollTransport()
        client_instance_id = str(uuid.uuid4())
        session, registration = await registry.register(
            client_instance_id=client_instance_id,
            registration_secret=secrets.token_urlsafe(48),
            document_epoch="abort-document",
            metadata={
                "name": "Disposable",
                "mode": "edit",
                "place_id": 301,
                "game_id": 401,
            },
            capabilities={"rnd_play_start"},
            transport=transport,
        )
        start = asyncio.create_task(
            session.invoke(
                "rnd_play_start", {}, 20, request_id="abort-start-request"
            )
        )
        self.assertIsNotNone(await transport.poll(1))
        prepared = registry.prepare_play_bridge(
            registration.studio_id,
            registration.document_epoch,
            registration.generation,
            registration.resume_token,
            "abort-start-request",
        )
        self.assertEqual(
            prepared["transition_nonce"], session.play_bridge_uncertain
        )
        with self.assertRaises(RequestTimeoutError):
            await start
        self.assertNotIn("abort-start-request", session.pending)
        aborted = registry.abort_play_bridge_pre_attach(
            registration.studio_id,
            registration.document_epoch,
            registration.generation,
            registration.resume_token,
            registration.generation,
            "abort-start-request",
            prepared["transition_nonce"],
            "abort-receipt",
            False,
            True,
        )
        self.assertEqual("pre_attach_aborted", aborted["completion_outcome"])
        self.assertIsNone(session.play_bridge_uncertain)

    async def test_pending_start_binding_and_reconnect_recovery_gate(self):
        manager = PlayBridgeManager(token_key=b"r" * 32)
        registry = SessionRegistry(play_bridges=manager)
        transport = LongPollTransport()
        client_instance_id = str(uuid.uuid4())
        registration_secret = secrets.token_urlsafe(48)
        session, registration = await registry.register(
            client_instance_id=client_instance_id,
            registration_secret=registration_secret,
            document_epoch="registry-document",
            metadata={
                "name": "Disposable",
                "mode": "edit",
                "place_id": 301,
                "game_id": 401,
            },
            capabilities={"rnd_play_start", "rnd_play_stop"},
            transport=transport,
        )

        start = asyncio.create_task(
            session.invoke(
                "rnd_play_start", {}, 1000, request_id="start-request"
            )
        )
        request = await transport.poll(1)
        self.assertIsNotNone(request)
        with self.assertRaises(SessionConflictError):
            registry.prepare_play_bridge(
                registration.studio_id,
                registration.document_epoch,
                registration.generation,
                registration.resume_token,
                "not-pending",
            )
        prepared = registry.prepare_play_bridge(
            registration.studio_id,
            registration.document_epoch,
            registration.generation,
            registration.resume_token,
            "start-request",
        )
        full = (
            registration.studio_id,
            client_instance_id,
            registration.document_epoch,
            registration.generation,
            "start-request",
            301,
            401,
            prepared["transition_nonce"],
        )
        attached = registry.attach_play_bridge(
            *full,
            "attach",
            "server",
            prepared["bridge_token"],
        )
        registry.receive_response(
            registration.studio_id,
            registration.generation,
            registration.resume_token,
            "start-request",
            success=True,
            result={"started": True},
        )
        self.assertEqual({"started": True}, await start)

        registry.disconnect(
            registration.studio_id,
            registration.generation,
            registration.resume_token,
            "test disconnect",
        )
        next_transport = LongPollTransport()
        _, replacement = await registry.register(
            client_instance_id=client_instance_id,
            registration_secret=registration_secret,
            document_epoch=registration.document_epoch,
            metadata={
                "name": "Disposable",
                "mode": "edit",
                "place_id": 301,
                "game_id": 401,
            },
            capabilities={"rnd_play_start", "rnd_play_stop"},
            studio_id=registration.studio_id,
            resume_token=registration.resume_token,
            reconnect_id=str(uuid.uuid4()),
            transport=next_transport,
        )
        recovered = registry.play_bridge_status(
            replacement.studio_id,
            replacement.document_epoch,
            replacement.generation,
            replacement.resume_token,
            registration.generation,
            "start-request",
            prepared["transition_nonce"],
        )
        self.assertTrue(recovered["recovery_only"])
        polled = registry.poll_play_bridge_server(
            *full, "server", attached["server_token"]
        )
        self.assertEqual("stop", polled["command"])
        registry.acknowledge_play_bridge_stop(
            *full,
            "server",
            "watchdog_armed",
            "recovery-watchdog-ack",
            None,
            attached["server_token"],
        )
        registry.acknowledge_play_bridge_stop(
            *full,
            "server",
            "stop_received",
            "recovery-stop-ack",
            polled["stop_command_id"],
            attached["server_token"],
        )
        registry.complete_play_bridge(
            replacement.studio_id,
            replacement.document_epoch,
            replacement.generation,
            replacement.resume_token,
            registration.generation,
            "start-request",
            prepared["transition_nonce"],
            "completion",
            "stopped_edit_confirmed",
            polled["stop_command_id"],
            True,
            2,
            True,
        )
        self.assertIsNone(session.play_bridge_uncertain)
