from __future__ import annotations

import asyncio
import json
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import (
    AuthenticationError,
    UnsafeCancellationError,
)
from studio_mcp_v2.multi_edit import (
    MULTI_EDIT_ATOMICITY,
    MULTI_EDIT_ORDERING_VERSION,
    canonical_json_bytes,
    canonical_json_sha256,
    normalize_multi_edit_arguments,
)
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)
SEARCH_PUBLIC = "studio_search_scripts_v2"
SEARCH_REMOTE = "studio_search_scripts"
MULTI_EDIT_PUBLIC = "studio_multi_edit_v2"


class Phase2JobStateParityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.service = ProxyService(self.registry, self.catalog)
        self.studio = await FakeStudio.create(
            self.registry,
            "Job parity",
            self.catalog.remote_names,
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

    def valid_search_result(self, request):
        arguments = request["args"]
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": SEARCH_REMOTE,
            "studio_id": self.studio.studio_id,
            "client_instance_id": self.studio.client_instance_id,
            "document_epoch": self.studio.registration.document_epoch,
            "generation": self.studio.generation,
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

    async def start_search_job(self):
        initial = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            SEARCH_PUBLIC,
            self.search_arguments(),
            1_000,
        )
        request = await self.studio.next_request()
        record = self.studio.session.jobs[initial["job_id"]]
        return initial, record, request

    async def test_multi_edit_admission_receipt_pins_identity_schemas_and_revisions(
        self,
    ) -> None:
        arguments = {
            "datamodel_type": "Edit",
            "targets": [
                {
                    "path": ["ServerScriptService", "Main"],
                    "expected_sha256": "a" * 64,
                    "edits": [
                        {
                            "old_string": "local secretOld = 1",
                            "new_string": "local secretNew = 2",
                        }
                    ],
                }
            ],
        }
        normalized = normalize_multi_edit_arguments(arguments)
        definition = self.catalog.get(MULTI_EDIT_PUBLIC)

        receipt = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            MULTI_EDIT_PUBLIC,
            arguments,
            1_000,
        )

        self.assertEqual("studio-mcp-v2-job-receipt", receipt["format"])
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual(self.studio.studio_id, receipt["studio_id"])
        self.assertEqual(
            self.studio.client_instance_id,
            receipt["client_instance_id"],
        )
        self.assertEqual(
            self.studio.registration.document_epoch,
            receipt["document_epoch"],
        )
        self.assertEqual(self.studio.generation, receipt["generation"])
        self.assertEqual(MULTI_EDIT_PUBLIC, receipt["tool_name"])
        self.assertEqual("studio_multi_edit", receipt["remote_tool"])
        self.assertEqual(
            definition.input_schema_sha256,
            receipt["input_schema_sha256"],
        )
        self.assertEqual(
            definition.output_schema_sha256,
            receipt["output_schema_sha256"],
        )
        self.assertEqual(
            definition.handler_contract_sha256,
            receipt["handler_contract_sha256"],
        )
        self.assertEqual(
            canonical_json_sha256(normalized),
            receipt["arguments_sha256"],
        )
        self.assertEqual(
            {
                "contract_version": "studio-job-admission-v2",
                "operation": "studio_multi_edit",
                "datamodel_type": "Edit",
                "target_count": 1,
                "edit_count": 1,
                "create_count": 0,
                "ordering_version": MULTI_EDIT_ORDERING_VERSION,
                "atomicity": MULTI_EDIT_ATOMICITY,
                "targets": [
                    {
                        "index": 1,
                        "kind": "edit",
                        "path": ["ServerScriptService", "Main"],
                        "expected_sha256": "a" * 64,
                        "edit_count": 1,
                    }
                ],
            },
            receipt["admitted_contract"],
        )
        serialized_contract = json.dumps(
            receipt["admitted_contract"], sort_keys=True
        )
        self.assertNotIn("secretOld", serialized_contract)
        self.assertNotIn("secretNew", serialized_contract)
        self.assertEqual([], receipt["dispatched_request_ids"])
        self.assertEqual([], receipt["dispatched_phases"])
        self.assertIsNone(receipt["transaction_id"])
        self.assertFalse(receipt["terminal"])
        self.assertFalse(receipt["result_present"])

        cancelled = self.service.cancel_job(
            ALLOW_ALL,
            self.studio.studio_id,
            receipt["job_id"],
        )
        self.assertEqual("cancelled", cancelled["status"])
        self.assertTrue(cancelled["terminal"])
        self.assertEqual(
            "acknowledged_before_dispatch",
            cancelled["cancellation_state"],
        )
        await asyncio.sleep(0)
        self.assertTrue(self.studio.transport._queue.empty())

    async def test_completed_validated_job_receipt_pins_result_digest_and_size(
        self,
    ) -> None:
        initial, record, request = await self.start_search_job()
        definition = self.catalog.get(SEARCH_PUBLIC)
        running = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )

        self.assertEqual("running", running["status"])
        self.assertTrue(running["dispatched"])
        self.assertEqual(
            [request["request_id"]],
            running["dispatched_request_ids"],
        )
        self.assertEqual(["direct"], running["dispatched_phases"])
        self.assertEqual(
            definition.input_schema_sha256,
            running["input_schema_sha256"],
        )
        self.assertEqual(
            definition.output_schema_sha256,
            running["output_schema_sha256"],
        )
        self.assertEqual(
            definition.handler_contract_sha256,
            running["handler_contract_sha256"],
        )
        self.assertEqual(
            canonical_json_sha256(self.search_arguments()),
            running["arguments_sha256"],
        )
        self.assertEqual(
            canonical_json_sha256("Player"),
            running["admitted_contract"]["query_sha256"],
        )
        self.assertNotIn("keywords", running["admitted_contract"])

        result = self.valid_search_result(request)
        self.assertTrue(self.studio.respond(request, result))
        await record.task

        completed = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("completed", completed["status"])
        self.assertTrue(completed["terminal"])
        self.assertEqual("completed", completed["terminal_outcome"])
        self.assertTrue(completed["result_present"])
        self.assertFalse(completed["error_present"])
        self.assertEqual(result, completed["result"])
        self.assertEqual(
            canonical_json_sha256(result),
            completed["result_sha256"],
        )
        self.assertEqual(
            len(canonical_json_bytes(result)),
            completed["result_bytes"],
        )

    async def test_public_cancel_after_dispatch_is_refused_not_reported_cancelled(
        self,
    ) -> None:
        initial = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            SEARCH_PUBLIC,
            self.search_arguments(),
            1_000,
        )
        request = await self.studio.next_request()
        record = self.studio.session.jobs[initial["job_id"]]

        with self.assertRaises(UnsafeCancellationError):
            self.service.cancel_job(
                ALLOW_ALL,
                self.studio.studio_id,
                initial["job_id"],
            )

        refused = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("running", refused["status"])
        self.assertFalse(refused["terminal"])
        self.assertTrue(refused["dispatched"])
        self.assertEqual(
            "requested_after_dispatch_refused",
            refused["cancellation_state"],
        )
        self.assertFalse(refused["result_present"])

        result = self.valid_search_result(request)
        self.assertTrue(self.studio.respond(request, result))
        await record.task
        completed = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual(result, completed["result"])

    async def test_dispatched_wait_cancellation_is_outcome_unknown_until_valid_late_result(
        self,
    ) -> None:
        initial, record, request = await self.start_search_job()

        record.task.cancel()
        await record.task

        uncertain = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("outcome_unknown", uncertain["status"])
        self.assertFalse(uncertain["terminal"])
        self.assertEqual(
            "requested_after_dispatch_not_acknowledged",
            uncertain["cancellation_state"],
        )
        self.assertEqual(
            "local_wait_cancelled_after_dispatch",
            uncertain["terminal_outcome"],
        )
        self.assertIn(
            request["request_id"],
            self.studio.session.uncertain_requests,
        )
        self.assertFalse(uncertain["result_present"])

        result = self.valid_search_result(request)
        self.assertTrue(self.studio.respond(request, result))

        settled = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("completed", settled["status"])
        self.assertTrue(settled["terminal"])
        self.assertEqual("completed_late_receipt", settled["terminal_outcome"])
        self.assertEqual(result, settled["result"])
        self.assertEqual(
            canonical_json_sha256(result),
            settled["result_sha256"],
        )
        self.assertEqual(
            len(canonical_json_bytes(result)),
            settled["result_bytes"],
        )
        self.assertNotIn(
            request["request_id"],
            self.studio.session.uncertain_requests,
        )
        self.assertIsNone(self.studio.session.uncertainty_state)

    async def test_invalid_late_result_preserves_job_and_session_uncertainty(
        self,
    ) -> None:
        initial, record, request = await self.start_search_job()
        record.task.cancel()
        await record.task

        invalid = self.valid_search_result(request)
        invalid["studio_id"] = "00000000-0000-4000-8000-000000000001"
        self.assertFalse(self.studio.respond(request, invalid))

        still_uncertain = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("outcome_unknown", still_uncertain["status"])
        self.assertFalse(still_uncertain["terminal"])
        self.assertFalse(still_uncertain["result_present"])
        self.assertIsNone(still_uncertain["result_sha256"])
        self.assertIsNone(still_uncertain["result_bytes"])
        self.assertIn(
            request["request_id"],
            self.studio.session.uncertain_requests,
        )
        self.assertIn(
            request["request_id"],
            self.studio.session.uncertain_pending,
        )
        self.assertIsNotNone(self.studio.session.uncertainty_state)

        valid = self.valid_search_result(request)
        self.assertTrue(self.studio.respond(request, valid))
        self.assertEqual(
            "completed",
            self.studio.session.jobs[initial["job_id"]].status,
        )

    async def test_disconnect_and_reconnect_fence_admitted_job_generation(
        self,
    ) -> None:
        initial = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            SEARCH_PUBLIC,
            self.search_arguments(),
            1_000,
        )
        request = await self.studio.next_request()
        record = self.studio.session.jobs[initial["job_id"]]
        old_generation = self.studio.generation
        old_resume_token = self.studio.resume_token

        self.assertTrue(self.studio.disconnect())
        await record.task
        disconnected = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("outcome_unknown", disconnected["status"])
        self.assertFalse(disconnected["terminal"])
        self.assertEqual(old_generation, disconnected["generation"])
        self.assertEqual(
            "connection_or_generation_lost_after_dispatch",
            disconnected["terminal_outcome"],
        )
        self.assertIn(
            request["request_id"],
            self.studio.session.uncertain_requests,
        )

        await self.studio.reconnect()
        self.assertEqual(old_generation + 1, self.studio.generation)
        self.assertTrue(self.studio.transport._queue.empty())
        self.assertFalse(
            self.studio.respond(request, {"entries": ["forged-new-gen"]})
        )
        with self.assertRaises(AuthenticationError):
            self.registry.receive_response(
                self.studio.studio_id,
                old_generation,
                old_resume_token,
                request["request_id"],
                success=True,
                result={"entries": ["late-old-gen"]},
            )

        fenced = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("outcome_unknown", fenced["status"])
        self.assertEqual(old_generation, fenced["generation"])
        self.assertEqual(
            [request["request_id"]],
            fenced["dispatched_request_ids"],
        )
        self.assertFalse(fenced["result_present"])

    async def test_studio_job_event_cannot_forge_broker_owned_job_state(
        self,
    ) -> None:
        initial = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            SEARCH_PUBLIC,
            self.search_arguments(),
            1_000,
        )
        request = await self.studio.next_request()
        record = self.studio.session.jobs[initial["job_id"]]

        self.assertFalse(
            self.studio.event(
                "job",
                {
                    "job_id": initial["job_id"],
                    "status": "completed",
                    "result": {"forged": True},
                    "generation": self.studio.generation,
                },
            )
        )
        unchanged = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            initial["job_id"],
        )
        self.assertEqual("running", unchanged["status"])
        self.assertFalse(unchanged["terminal"])
        self.assertFalse(unchanged["result_present"])
        self.assertIsNone(unchanged["terminal_outcome"])
        self.assertEqual(
            [request["request_id"]],
            unchanged["dispatched_request_ids"],
        )

        legitimate = self.valid_search_result(request)
        self.assertTrue(self.studio.respond(request, legitimate))
        await record.task
        self.assertEqual("completed", record.status)
        self.assertEqual(legitimate, record.result)


if __name__ == "__main__":
    unittest.main()
