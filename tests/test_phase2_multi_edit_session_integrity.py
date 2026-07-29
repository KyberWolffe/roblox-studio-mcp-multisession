from __future__ import annotations

import asyncio
import copy
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import (
    AuthorizationError,
    SessionConflictError,
)
from studio_mcp_v2.multi_edit import (
    MULTI_EDIT_ATOMICITY,
    MULTI_EDIT_ORDERING_VERSION,
    MULTI_EDIT_RECEIPT_CONTRACT,
    canonical_json_bytes,
    canonical_json_sha256,
    mutation_receipt_sha256,
    prepare_receipt_sha256,
)
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService
from studio_mcp_v2.session import PendingRequest

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)
MULTI_EDIT_PUBLIC = "studio_multi_edit_v2"
RECOVER_MULTI_EDIT_PUBLIC = "studio_recover_multi_edit_v2"


class Phase2MultiEditSessionIntegrityTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.service = ProxyService(self.registry, self.catalog)
        self.studio = await FakeStudio.create(
            self.registry,
            "Multi-edit integrity",
            self.catalog.remote_names,
        )
        self.arguments = {
            "datamodel_type": "Edit",
            "targets": [
                {
                    "path": ["ServerScriptService", "Main"],
                    "expected_sha256": "1" * 64,
                    "edits": [
                        {
                            "old_string": "old",
                            "new_string": "new",
                        }
                    ],
                }
            ],
        }

    def prepare_receipt(self, request):
        target = {
            "index": 1,
            "path": copy.deepcopy(
                request["args"]["targets"][0]["path"]
            ),
            "expected_sha256": "1" * 64,
            "prepared_sha256": "1" * 64,
            "planned_sha256": "2" * 64,
            "source_length": 3,
            "planned_source_length": 3,
            "edit_count": 1,
            "replacement_count": 1,
            "status": "prepared",
        }
        receipt = {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": "studio_multi_edit",
            "phase": "prepare",
            "studio_id": self.studio.studio_id,
            "client_instance_id": self.studio.client_instance_id,
            "document_epoch": self.studio.registration.document_epoch,
            "generation": request["generation"],
            "request_id": request["request_id"],
            "transaction_id": request["args"]["transaction_id"],
            "ordering_version": MULTI_EDIT_ORDERING_VERSION,
            "atomicity": MULTI_EDIT_ATOMICITY,
            "target_count": 1,
            "edit_count": 1,
            "aggregate_source_bytes": 3,
            "aggregate_planned_source_bytes": 3,
            "targets": [target],
            "expires_in_ms": 120_000,
        }
        receipt["prepare_sha256"] = prepare_receipt_sha256(
            receipt
        )
        return receipt

    def mutation_receipt(
        self,
        request,
        prepared,
        *,
        recovery=False,
        outcome=None,
    ):
        if outcome is None:
            outcome = "recovered" if recovery else "applied"
        if outcome == "applied":
            status = "applied"
            before = "1" * 64
            after = "2" * 64
        elif outcome == "recovered":
            status = "rolled_back"
            before = "2" * 64
            after = "1" * 64
        elif outcome == "recovery_required":
            status = "recovery_required"
            before = "2" * 64
            after = "2" * 64
        else:
            raise AssertionError("unsupported test outcome")
        receipt = {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": (
                "studio_recover_multi_edit"
                if recovery
                else "studio_multi_edit"
            ),
            "phase": "recover" if recovery else "apply",
            "studio_id": self.studio.studio_id,
            "client_instance_id": self.studio.client_instance_id,
            "document_epoch": self.studio.registration.document_epoch,
            "generation": request["generation"],
            "request_id": request["request_id"],
            "transaction_id": prepared["transaction_id"],
            "prepare_request_id": prepared["request_id"],
            "prepare_sha256": prepared["prepare_sha256"],
            "ordering_version": MULTI_EDIT_ORDERING_VERSION,
            "atomicity": MULTI_EDIT_ATOMICITY,
            "receipt_contract": MULTI_EDIT_RECEIPT_CONTRACT,
            "outcome": outcome,
            "safe_terminal": outcome != "recovery_required",
            "recovery_required": outcome == "recovery_required",
            "target_count": 1,
            "edit_count": 1,
            "targets": [
                {
                    "index": 1,
                    "path": copy.deepcopy(
                        prepared["targets"][0]["path"]
                    ),
                    "expected_sha256": "1" * 64,
                    "prepared_sha256": "1" * 64,
                    "planned_sha256": "2" * 64,
                    "observed_before_sha256": before,
                    "observed_after_sha256": after,
                    "source_length": 3,
                    "planned_source_length": 3,
                    "edit_count": 1,
                    "replacement_count": 1,
                    "status": status,
                }
            ],
        }
        receipt["receipt_sha256"] = mutation_receipt_sha256(
            receipt
        )
        return receipt

    async def run_successful_multi_edit(self):
        operation = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                MULTI_EDIT_PUBLIC,
                {
                    "studio_id": self.studio.studio_id,
                    **copy.deepcopy(self.arguments),
                },
            )
        )
        prepare_request = await self.studio.next_request()
        self.assertEqual("prepare", prepare_request["args"]["_phase"])
        prepared = self.prepare_receipt(prepare_request)
        self.assertTrue(
            self.studio.respond(prepare_request, prepared)
        )
        apply_request = await self.studio.next_request()
        self.assertEqual("apply", apply_request["args"]["_phase"])
        applied = self.mutation_receipt(
            apply_request, prepared
        )
        self.assertTrue(
            self.studio.respond(apply_request, applied)
        )
        return prepared, applied, await operation

    async def test_two_phase_success_returns_only_validated_apply_receipt(
        self,
    ) -> None:
        prepared, applied, result = (
            await self.run_successful_multi_edit()
        )
        self.assertEqual(applied, result)
        self.assertNotEqual(
            prepared["request_id"], applied["request_id"]
        )
        self.assertIsNone(
            self.studio.session.multi_edit_prepared_receipt
        )
        self.assertIsNone(self.studio.session.multi_edit_recovery)
        self.assertFalse(self.studio.session.uncertain_requests)

    async def test_recovery_fence_blocks_every_nonmatching_operation_even_without_uncertain_map(
        self,
    ) -> None:
        transaction_id = (
            "00000000-0000-4000-8000-000000000111"
        )
        self.studio.session._set_multi_edit_recovery(
            transaction_id,
            "apply-request",
            self.studio.generation,
            "test_recovery_fence",
        )

        with self.assertRaises(SessionConflictError):
            self.studio.session.assert_operation_admissible(
                self.studio.generation,
                "studio_get_state",
                {},
            )
        with self.assertRaises(SessionConflictError):
            self.studio.session.assert_operation_admissible(
                self.studio.generation,
                "studio_recover_multi_edit",
                {
                    "transaction_id": (
                        "00000000-0000-4000-8000-000000000222"
                    )
                },
            )
        self.studio.session.assert_operation_admissible(
            self.studio.generation,
            "studio_recover_multi_edit",
            {"transaction_id": transaction_id},
        )

    async def test_apply_reauthorization_failure_clears_source_free_prepare_without_recovery(
        self,
    ) -> None:
        authorization_count = 0

        def authorize_each_phase():
            nonlocal authorization_count
            authorization_count += 1
            if authorization_count == 2:
                raise AuthorizationError(
                    "authorization revoked before apply"
                )

        operation = asyncio.create_task(
            self.studio.session.invoke(
                "studio_multi_edit",
                copy.deepcopy(self.arguments),
                1_000,
                before_dispatch=authorize_each_phase,
            )
        )
        prepare_request = await self.studio.next_request()
        prepared = self.prepare_receipt(prepare_request)
        self.assertTrue(
            self.studio.respond(prepare_request, prepared)
        )
        with self.assertRaises(AuthorizationError):
            await operation
        self.assertEqual(2, authorization_count)
        self.assertTrue(self.studio.transport._queue.empty())
        self.assertIsNone(
            self.studio.session.multi_edit_prepared_receipt
        )
        self.assertIsNone(self.studio.session.multi_edit_recovery)
        self.assertFalse(self.studio.session.uncertain_requests)

    async def test_recovery_admission_rejects_mismatched_uncertainty_context(
        self,
    ) -> None:
        transaction_id = (
            "00000000-0000-4000-8000-000000000111"
        )
        other_transaction_id = (
            "00000000-0000-4000-8000-000000000222"
        )
        request_id = "uncertain-apply"
        future = asyncio.get_running_loop().create_future()
        pending = PendingRequest(
            request_id=request_id,
            generation=self.studio.generation,
            remote_tool="studio_multi_edit",
            arguments={
                "_phase": "apply",
                "transaction_id": other_transaction_id,
            },
            future=future,
        )
        self.studio.session.uncertain_requests[request_id] = {
            "generation": self.studio.generation,
            "operation": "studio_multi_edit",
            "transaction_id": other_transaction_id,
        }
        self.studio.session.uncertain_pending[request_id] = pending
        self.studio.session._set_multi_edit_recovery(
            transaction_id,
            request_id,
            self.studio.generation,
            "test_recovery_fence",
        )

        with self.assertRaises(SessionConflictError):
            self.studio.session.assert_operation_admissible(
                self.studio.generation,
                "studio_recover_multi_edit",
                {"transaction_id": transaction_id},
            )
        future.cancel()

    async def test_reconnect_cannot_rebind_old_generation_recovery(
        self,
    ) -> None:
        operation = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                MULTI_EDIT_PUBLIC,
                {
                    "studio_id": self.studio.studio_id,
                    **copy.deepcopy(self.arguments),
                },
            )
        )
        prepare_request = await self.studio.next_request()
        prepared = self.prepare_receipt(prepare_request)
        self.assertTrue(
            self.studio.respond(prepare_request, prepared)
        )
        apply_request = await self.studio.next_request()
        uncertain = self.mutation_receipt(
            apply_request,
            prepared,
            outcome="recovery_required",
        )
        self.assertTrue(
            self.studio.respond(apply_request, uncertain)
        )
        self.assertEqual(uncertain, await operation)
        old_generation = self.studio.generation
        self.assertEqual(
            old_generation,
            self.studio.session.multi_edit_recovery[
                "generation"
            ],
        )
        self.assertTrue(self.studio.disconnect())
        await self.studio.reconnect()
        self.assertEqual(
            old_generation + 1,
            self.studio.generation,
        )

        with self.assertRaises(SessionConflictError):
            await self.service.call_tool(
                ALLOW_ALL,
                RECOVER_MULTI_EDIT_PUBLIC,
                {
                    "studio_id": self.studio.studio_id,
                    "transaction_id": prepared[
                        "transaction_id"
                    ],
                },
            )
        self.assertTrue(self.studio.transport._queue.empty())
        self.assertEqual(
            old_generation,
            self.studio.session.multi_edit_recovery[
                "generation"
            ],
        )
        self.assertTrue(
            all(
                pending.generation == old_generation
                for pending in (
                    self.studio.session.uncertain_pending.values()
                )
            )
        )

    async def test_post_dispatch_observer_failure_is_quarantined_with_exact_context(
        self,
    ) -> None:
        def fail_observer(*_args):
            raise RuntimeError("observer failed")

        with self.assertRaises(RuntimeError):
            await self.studio.session.invoke(
                "studio_get_console",
                {},
                1_000,
                on_dispatched=fail_observer,
            )
        request = await self.studio.next_request()
        request_id = request["request_id"]
        self.assertIn(
            request_id,
            self.studio.session.uncertain_requests,
        )
        self.assertIn(
            request_id,
            self.studio.session.uncertain_pending,
        )
        self.assertEqual(
            "post_dispatch_observer_failed",
            self.studio.session.uncertain_requests[
                request_id
            ]["reason"],
        )

    async def test_apply_observer_failure_sets_exact_recovery_fence(
        self,
    ) -> None:
        transaction_id = (
            "00000000-0000-4000-8000-000000000111"
        )
        prepared = {
            "transaction_id": transaction_id,
            "request_id": "prepare-request",
            "prepare_sha256": "3" * 64,
        }
        self.studio.session.multi_edit_prepared_receipt = prepared

        def fail_observer(*_args):
            raise RuntimeError("observer failed")

        with self.assertRaises(RuntimeError):
            await self.studio.session.invoke(
                "studio_multi_edit",
                {
                    "_phase": "apply",
                    "transaction_id": transaction_id,
                    "prepare_request_id": "prepare-request",
                    "prepare_sha256": "3" * 64,
                    "prepared_targets": [],
                },
                1_000,
                on_dispatched=fail_observer,
            )
        request = await self.studio.next_request()
        self.assertEqual(
            transaction_id,
            self.studio.session.multi_edit_recovery[
                "transaction_id"
            ],
        )
        self.assertEqual(
            request["request_id"],
            self.studio.session.multi_edit_recovery["request_id"],
        )
        self.assertIn(
            request["request_id"],
            self.studio.session.uncertain_pending,
        )

    async def test_mutation_validator_requires_exact_internal_phase(
        self,
    ) -> None:
        operation = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                MULTI_EDIT_PUBLIC,
                {
                    "studio_id": self.studio.studio_id,
                    **copy.deepcopy(self.arguments),
                },
            )
        )
        prepare_request = await self.studio.next_request()
        prepared = self.prepare_receipt(prepare_request)
        self.assertTrue(
            self.studio.respond(prepare_request, prepared)
        )
        apply_request = await self.studio.next_request()
        applied = self.mutation_receipt(
            apply_request, prepared
        )
        pending = self.studio.session.pending[
            apply_request["request_id"]
        ]

        missing_apply_phase = PendingRequest(
            request_id=pending.request_id,
            generation=pending.generation,
            remote_tool=pending.remote_tool,
            arguments=copy.deepcopy(pending.arguments),
            future=pending.future,
        )
        missing_apply_phase.arguments.pop("_phase")
        self.assertFalse(
            self.studio.session._valid_multi_edit_mutation_result(
                missing_apply_phase, applied
            )
        )
        self.studio.session.multi_edit_prepared_receipt[
            "generation"
        ] = pending.generation + 1
        self.assertFalse(
            self.studio.session._valid_multi_edit_mutation_result(
                pending, applied
            )
        )
        self.studio.session.multi_edit_prepared_receipt[
            "generation"
        ] = pending.generation

        recovery_pending = PendingRequest(
            request_id=pending.request_id,
            generation=pending.generation,
            remote_tool="studio_recover_multi_edit",
            arguments=copy.deepcopy(pending.arguments),
            future=pending.future,
        )
        recovery_pending.arguments.pop("_phase")
        recovered = self.mutation_receipt(
            apply_request,
            prepared,
            recovery=True,
        )
        recovered["operation"] = "studio_recover_multi_edit"
        recovered["phase"] = "recover"
        recovered["receipt_sha256"] = mutation_receipt_sha256(
            recovered
        )
        self.assertTrue(
            self.studio.session._valid_multi_edit_mutation_result(
                recovery_pending, recovered
            )
        )
        recovery_pending.arguments["_phase"] = "apply"
        self.assertFalse(
            self.studio.session._valid_multi_edit_mutation_result(
                recovery_pending, recovered
            )
        )

        self.assertTrue(
            self.studio.respond(apply_request, applied)
        )
        self.assertEqual(applied, await operation)

    async def test_exact_recovery_links_job_without_overwriting_original_result_provenance(
        self,
    ) -> None:
        initial = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            MULTI_EDIT_PUBLIC,
            copy.deepcopy(self.arguments),
            1_000,
        )
        job = self.studio.session.jobs[initial["job_id"]]
        prepare_request = await self.studio.next_request()
        prepared = self.prepare_receipt(prepare_request)
        self.assertTrue(
            self.studio.respond(prepare_request, prepared)
        )
        apply_request = await self.studio.next_request()
        uncertain_result = self.mutation_receipt(
            apply_request,
            prepared,
            outcome="recovery_required",
        )
        self.assertTrue(
            self.studio.respond(apply_request, uncertain_result)
        )
        await job.task
        before_recovery = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            job.job_id,
        )
        self.assertEqual(
            "outcome_unknown", before_recovery["status"]
        )
        self.assertEqual(
            uncertain_result, before_recovery["result"]
        )
        self.assertEqual([], before_recovery["resolution_receipts"])

        recovery = asyncio.create_task(
            self.service.call_tool(
                ALLOW_ALL,
                RECOVER_MULTI_EDIT_PUBLIC,
                {
                    "studio_id": self.studio.studio_id,
                    "transaction_id": prepared[
                        "transaction_id"
                    ],
                },
            )
        )
        recovery_request = await self.studio.next_request()
        recovered = self.mutation_receipt(
            recovery_request,
            prepared,
            recovery=True,
        )
        self.assertTrue(
            self.studio.respond(recovery_request, recovered)
        )
        self.assertEqual(recovered, await recovery)

        resolved = self.service.get_job(
            ALLOW_ALL,
            self.studio.studio_id,
            job.job_id,
        )
        self.assertEqual("completed", resolved["status"])
        self.assertEqual(
            "resolved_by_exact_recovery:recovered",
            resolved["terminal_outcome"],
        )
        self.assertEqual(uncertain_result, resolved["result"])
        self.assertEqual(
            apply_request["request_id"],
            resolved["result"]["request_id"],
        )
        self.assertNotIn(
            recovery_request["request_id"],
            resolved["dispatched_request_ids"],
        )
        self.assertEqual(1, len(resolved["resolution_receipts"]))
        resolution = resolved["resolution_receipts"][0]
        self.assertEqual(
            recovery_request["request_id"],
            resolution["request_id"],
        )
        self.assertEqual("direct", resolution["source"])
        self.assertEqual("", resolution["resolver_job_id"])
        self.assertEqual(
            recovered["receipt_sha256"],
            resolution["receipt_sha256"],
        )
        self.assertEqual(recovered, resolution["result"])
        self.assertEqual(
            canonical_json_sha256(recovered),
            resolution["result_sha256"],
        )
        self.assertEqual(
            len(canonical_json_bytes(recovered)),
            resolution["result_bytes"],
        )
        self.assertIsNone(self.studio.session.multi_edit_recovery)
        self.assertFalse(self.studio.session.uncertain_requests)


if __name__ == "__main__":
    unittest.main()
