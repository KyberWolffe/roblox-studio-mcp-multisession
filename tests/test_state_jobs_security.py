from __future__ import annotations

import asyncio
import ast
import unittest

from studio_mcp_v2.auth import Principal
from studio_mcp_v2.errors import (
    AuthorizationError,
    JobNotFoundError,
    UnsafeCancellationError,
)
from studio_mcp_v2.session import JobRecord

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio, make_service


class StateJobsSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry, self.catalog, self.service = make_service()
        self.a = await FakeStudio.create(
            self.registry, "A", self.catalog.remote_names
        )
        self.b = await FakeStudio.create(
            self.registry, "B", self.catalog.remote_names
        )

    async def test_console_mode_and_events_are_generation_and_session_scoped(self):
        self.assertTrue(self.a.event("console", {"message": "A only"}))
        self.assertTrue(self.b.event("console", {"message": "B only"}))
        self.assertTrue(self.a.event("mode", {"mode": "play"}))
        self.assertEqual("play", self.a.session.mode)
        self.assertEqual("edit", self.b.session.mode)
        self.assertEqual("A only", self.a.session.console[0]["payload"]["message"])
        self.assertEqual("B only", self.b.session.console[0]["payload"]["message"])
        old_generation = self.a.generation
        old_token = self.a.resume_token
        self.a.disconnect()
        await self.a.reconnect()
        self.assertFalse(
            self.a.session.receive_event(
                old_generation, "mode", {"mode": "run_server"}
            )
        )
        self.assertEqual("edit", self.a.session.mode)
        self.assertNotEqual(old_token, self.a.resume_token)

    async def test_disconnected_state_read_uses_exact_broker_transition(self):
        prepared = self.registry.play_bridges.prepare(
            self.a.studio_id,
            self.a.client_instance_id,
            self.a.registration.document_epoch,
            self.a.generation,
            "accepted-start",
            0,
            0,
            ttl_seconds=180,
        )
        self.a.session.play_bridge_uncertain = prepared["transition_nonce"]
        self.a.disconnect()

        state = await self.service.call_tool(
            ALLOW_ALL,
            "get_studio_state_v2",
            {"studio_id": self.a.studio_id},
        )
        self.assertEqual("studio-mcp-v2-broker-recovery-view", state["adapter"])
        self.assertFalse(state["connected"])
        self.assertEqual("stopping", state["mode"])
        self.assertFalse(state["is_edit"])
        self.assertEqual("stopping", state["play"]["state"])
        self.assertFalse(state["play"]["active"])
        self.assertTrue(state["play"]["recovery_only"])
        self.assertEqual(
            prepared["transition_nonce"],
            state["play"]["transition_nonce"],
        )
        self.assertTrue(self.a.transport._queue.empty())

    async def test_discovery_uses_normalized_broker_transition_state(self):
        prepared = self.registry.play_bridges.prepare(
            self.a.studio_id,
            self.a.client_instance_id,
            self.a.registration.document_epoch,
            self.a.generation,
            "accepted-start",
            0,
            0,
            ttl_seconds=180,
        )

        snapshots = {
            item["studio_id"]: item
            for item in self.registry.snapshots()
        }
        observed = snapshots[self.a.studio_id]
        self.assertEqual("starting", observed["mode"])
        self.assertEqual("starting", observed["play"]["state"])
        self.assertFalse(observed["play"]["active"])
        self.assertEqual(
            prepared["transition_nonce"],
            observed["play"]["transition_nonce"],
        )

    async def test_jobs_are_scoped_and_dispatched_under_session_lock(self):
        job_a = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            "execute_luau_v2",
            {"code": "return 'A'", "datamodel_type": "Edit"},
            1000,
        )
        request_a = await self.a.next_request()
        with self.assertRaises(JobNotFoundError):
            self.service.get_job(ALLOW_ALL, self.b.studio_id, job_a["job_id"])
        self.a.respond(request_a, {"value": "A"})
        await self.a.session.jobs[job_a["job_id"]].task
        completed = self.service.get_job(
            ALLOW_ALL, self.a.studio_id, job_a["job_id"]
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual({"value": "A"}, completed["result"])

    async def test_identical_job_ids_do_not_collide_across_sessions(self):
        shared_id = "same-job-id"
        record_a = JobRecord(
            shared_id,
            self.a.studio_id,
            self.a.generation,
            "get_console_output_v2",
            "get_console_output",
            {},
            1000,
            status="completed",
            result="A",
        )
        record_b = JobRecord(
            shared_id,
            self.b.studio_id,
            self.b.generation,
            "get_console_output_v2",
            "get_console_output",
            {},
            1000,
            status="completed",
            result="B",
        )
        self.a.session.jobs[shared_id] = record_a
        self.b.session.jobs[shared_id] = record_b
        self.assertEqual(
            "A",
            self.service.get_job(ALLOW_ALL, self.a.studio_id, shared_id)["result"],
        )
        self.assertEqual(
            "B",
            self.service.get_job(ALLOW_ALL, self.b.studio_id, shared_id)["result"],
        )

    async def test_dispatched_job_is_not_unsafely_claimed_cancelled(self):
        job = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            "multi_edit_v2",
            {"file_path": "game.A", "edits": [], "datamodel_type": "Edit"},
            1000,
        )
        request = await self.a.next_request()
        with self.assertRaises(UnsafeCancellationError):
            self.service.cancel_job(ALLOW_ALL, self.a.studio_id, job["job_id"])
        self.a.respond(request, "finished")
        await self.a.session.jobs[job["job_id"]].task

    async def test_authorization_is_independent_from_routing(self):
        restricted = Principal.create(
            "A-only",
            allowed_studios=[self.a.studio_id],
            allowed_tools=["get_console_output_v2"],
        )
        with self.assertRaises(AuthorizationError):
            await self.service.call_tool(
                restricted,
                "get_console_output_v2",
                {"studio_id": self.b.studio_id},
            )
        self.assertTrue(self.b.transport._queue.empty())
        with self.assertRaises(AuthorizationError):
            self.service.start_job(
                restricted,
                self.a.studio_id,
                "get_console_output_v2",
                {},
                1000,
            )
        visible = self.service.list_studios(restricted)["studios"]
        self.assertEqual([self.a.studio_id], [item["studio_id"] for item in visible])

    async def test_queued_job_cannot_cross_reconnect_generation(self):
        job = self.service.start_job(
            ALLOW_ALL,
            self.a.studio_id,
            "multi_edit_v2",
            {"file_path": "game.A", "edits": [], "datamodel_type": "Edit"},
            1000,
        )
        # Do not yield to the job task before fencing generation 1.
        self.a.disconnect()
        await self.a.reconnect()
        await asyncio.sleep(0)
        record = self.a.session.jobs[job["job_id"]]
        self.assertEqual("disconnected", record.status)
        self.assertFalse(record.dispatched)
        self.assertTrue(self.a.transport._queue.empty())

    async def test_shell_like_luau_is_forwarded_as_inert_data(self):
        payload = "`touch /tmp/never` $(id); os.execute('nope')"
        task = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                "RunCode_v2",
                {"studio_id": self.a.studio_id, "command": payload},
            )
        )
        request = await self.a.next_request()
        self.assertEqual(payload, request["args"]["command"])
        self.a.respond(request, "mock-only")
        self.assertEqual("mock-only", await task)

    async def test_no_host_execution_primitives_in_proxy_package(self):
        forbidden_calls = {"eval", "exec", "compile", "__import__"}
        forbidden_attributes = {"system", "popen", "spawn", "fork", "execv", "execve"}
        for path in (PROJECT_ROOT / "studio_mcp_v2").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, forbidden_calls, str(path))
                    if isinstance(node.func, ast.Attribute):
                        self.assertNotIn(
                            node.func.attr, forbidden_attributes, str(path)
                        )


if __name__ == "__main__":
    unittest.main()
