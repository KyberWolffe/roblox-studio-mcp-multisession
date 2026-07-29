from __future__ import annotations

import asyncio
import ast
import unittest

from studio_mcp_v2.auth import Principal
from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import (
    AuthorizationError,
    JobNotFoundError,
    UnsafeCancellationError,
)
from studio_mcp_v2.session import JobRecord
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService

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
        self.job_registry = SessionRegistry()
        self.job_catalog = ToolCatalog.from_file(
            PROJECT_ROOT / "config" / "durable-tool-catalog.json"
        )
        self.job_service = ProxyService(
            self.job_registry, self.job_catalog
        )
        self.job_a = await FakeStudio.create(
            self.job_registry,
            "Job A",
            self.job_catalog.remote_names,
        )
        self.job_b = await FakeStudio.create(
            self.job_registry,
            "Job B",
            self.job_catalog.remote_names,
        )

    @staticmethod
    def search_arguments():
        return {
            "keywords": "Player",
            "root_path": ["Workspace"],
            "max_depth": 3,
            "scan_limit": 10,
            "page_size": 2,
            "time_limit_ms": 1_000,
        }

    @staticmethod
    def valid_search_result(studio, request):
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
            "root_path": list(arguments["root_path"]),
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
            "keywords": ["player"],
            "match_semantics": (
                "all_keywords_ascii_case_insensitive_"
                "literal_subsequence"
            ),
            "query_version": "script-name-query-v1",
        }

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
        job_a = self.job_service.start_job(
            ALLOW_ALL,
            self.job_a.studio_id,
            "studio_search_scripts_v2",
            self.search_arguments(),
            1000,
        )
        request_a = await self.job_a.next_request()
        with self.assertRaises(JobNotFoundError):
            self.job_service.get_job(
                ALLOW_ALL,
                self.job_b.studio_id,
                job_a["job_id"],
            )
        result = self.valid_search_result(
            self.job_a, request_a
        )
        self.job_a.respond(request_a, result)
        await self.job_a.session.jobs[job_a["job_id"]].task
        completed = self.job_service.get_job(
            ALLOW_ALL,
            self.job_a.studio_id,
            job_a["job_id"],
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual(result, completed["result"])

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
        job = self.job_service.start_job(
            ALLOW_ALL,
            self.job_a.studio_id,
            "studio_search_scripts_v2",
            self.search_arguments(),
            1000,
        )
        request = await self.job_a.next_request()
        with self.assertRaises(UnsafeCancellationError):
            self.job_service.cancel_job(
                ALLOW_ALL,
                self.job_a.studio_id,
                job["job_id"],
            )
        self.job_a.respond(
            request,
            self.valid_search_result(self.job_a, request),
        )
        await self.job_a.session.jobs[job["job_id"]].task

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
        job = self.job_service.start_job(
            ALLOW_ALL,
            self.job_a.studio_id,
            "studio_search_scripts_v2",
            self.search_arguments(),
            1000,
        )
        # Do not yield to the job task before fencing generation 1.
        self.job_a.disconnect()
        await self.job_a.reconnect()
        await asyncio.sleep(0)
        record = self.job_a.session.jobs[job["job_id"]]
        self.assertEqual("failed", record.status)
        self.assertEqual(
            "not_dispatched_connection_lost",
            record.terminal_outcome,
        )
        self.assertFalse(record.dispatched)
        self.assertTrue(self.job_a.transport._queue.empty())

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
