from __future__ import annotations

import asyncio
import copy
import hashlib
import unittest

from studio_mcp_v2.errors import RemoteToolError, ValidationError
from studio_mcp_v2.multi_edit import (
    MAX_MULTI_EDIT_SOURCE_BYTES,
    MAX_MULTI_EDIT_TARGETS,
    MULTI_EDIT_ATOMICITY,
    MULTI_EDIT_ORDERING_VERSION,
    MULTI_EDIT_RECEIPT_CONTRACT,
    mutation_receipt_sha256,
    normalize_multi_edit_arguments,
    prepare_receipt_sha256,
)
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.session import PendingRequest, _job_admitted_contract

from .helpers import FakeStudio


EDIT_SHA = "1" * 64
EDIT_PLANNED_SHA = "2" * 64


def _create(
    name: str = "CreatedByTransaction",
    *,
    parent_path=None,
    class_name: str = "ModuleScript",
    source: str = "return true\n",
):
    return {
        "parent_path": list(
            parent_path
            if parent_path is not None
            else ["ReplicatedStorage"]
        ),
        "name": name,
        "class_name": class_name,
        "expected_absent": True,
        "source": source,
    }


def _edit():
    return {
        "path": ["ServerScriptService", "Existing"],
        "expected_sha256": EDIT_SHA,
        "edits": [{"old_string": "old", "new_string": "new"}],
    }


class ScriptLifecycleModelTests(unittest.TestCase):
    def test_normalizes_creation_only_and_mixed_transactions(self) -> None:
        creation_only = {
            "datamodel_type": "Edit",
            "targets": [],
            "creates": [_create(source="")],
        }
        self.assertEqual(
            normalize_multi_edit_arguments(creation_only),
            creation_only,
        )

        mixed = {
            "datamodel_type": "Edit",
            "targets": [_edit()],
            "creates": [
                _create("Client", class_name="LocalScript"),
                _create("Server", class_name="Script"),
            ],
        }
        normalized = normalize_multi_edit_arguments(mixed)
        self.assertEqual(
            [["ServerScriptService", "Existing"]],
            [target["path"] for target in normalized["targets"]],
        )
        self.assertFalse(
            normalized["targets"][0]["edits"][0]["replace_all"]
        )
        self.assertEqual(
            ["Client", "Server"],
            [create["name"] for create in normalized["creates"]],
        )
        self.assertEqual(
            "edit-target-input-then-create-input-v2",
            MULTI_EDIT_ORDERING_VERSION,
        )

    def test_create_schema_and_expected_absent_fail_closed(self) -> None:
        invalid = []
        for field in (
            "parent_path",
            "name",
            "class_name",
            "expected_absent",
            "source",
        ):
            value = _create()
            value.pop(field)
            invalid.append(value)
        invalid.extend(
            [
                {**_create(), "extra": True},
                {**_create(), "expected_absent": False},
                {**_create(), "expected_absent": 1},
                _create(class_name="Folder"),
                _create(name=""),
                _create(name="line\nbreak"),
                _create(name="unicode\u0085control"),
                _create(
                    parent_path=[
                        "Replicated\u0085Storage"
                    ]
                ),
                _create(parent_path=[]),
                _create(parent_path=["Part"] * 64),
                _create(source="\ud800"),
                _create(source="x" * (MAX_MULTI_EDIT_SOURCE_BYTES + 1)),
            ]
        )
        for create in invalid:
            with self.subTest(create=create):
                with self.assertRaises(ValidationError):
                    normalize_multi_edit_arguments(
                        {
                            "datamodel_type": "Edit",
                            "targets": [],
                            "creates": [create],
                        }
                    )

    def test_create_paths_cannot_collide_or_depend_on_transaction_create(
        self,
    ) -> None:
        duplicate_edit_path = {
            "datamodel_type": "Edit",
            "targets": [
                {
                    **_edit(),
                    "path": [
                        "ReplicatedStorage",
                        "CreatedByTransaction",
                    ],
                }
            ],
            "creates": [_create()],
        }
        duplicate_create_path = {
            "datamodel_type": "Edit",
            "targets": [],
            "creates": [_create(), _create()],
        }
        created_parent = {
            "datamodel_type": "Edit",
            "targets": [],
            "creates": [
                _create("Parent"),
                _create(
                    "Child",
                    parent_path=["ReplicatedStorage", "Parent"],
                ),
            ],
        }
        for payload in (
            duplicate_edit_path,
            duplicate_create_path,
            created_parent,
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    normalize_multi_edit_arguments(payload)

    def test_combined_count_and_optional_array_bounds(self) -> None:
        invalid = [
            {"datamodel_type": "Edit", "targets": []},
            {
                "datamodel_type": "Edit",
                "targets": [_edit()],
                "creates": [],
            },
            {
                "datamodel_type": "Edit",
                "targets": [],
                "creates": [
                    _create(f"Create-{index}")
                    for index in range(MAX_MULTI_EDIT_TARGETS + 1)
                ],
            },
            {
                "datamodel_type": "Edit",
                "targets": [_edit()],
                "creates": [
                    _create(f"Create-{index}")
                    for index in range(MAX_MULTI_EDIT_TARGETS)
                ],
            },
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    normalize_multi_edit_arguments(payload)

    def test_create_identity_fields_are_bound_into_both_receipt_hashes(
        self,
    ) -> None:
        source = b"return true\n"
        planned_sha = hashlib.sha256(source).hexdigest()
        prepared_target = {
            "index": 1,
            "kind": "create",
            "path": ["ReplicatedStorage", "Created"],
            "parent_path": ["ReplicatedStorage"],
            "name": "Created",
            "class_name": "ModuleScript",
            "expected_absent": True,
            "prepared_absent": True,
            "planned_sha256": planned_sha,
            "planned_source_length": len(source),
            "status": "prepared",
        }
        prepared = {
            "studio_id": "studio",
            "client_instance_id": "client",
            "document_epoch": "document",
            "generation": 1,
            "request_id": "prepare",
            "transaction_id": "transaction",
            "ordering_version": MULTI_EDIT_ORDERING_VERSION,
            "atomicity": MULTI_EDIT_ATOMICITY,
            "target_count": 1,
            "edit_count": 0,
            "create_count": 1,
            "aggregate_source_bytes": 0,
            "aggregate_planned_source_bytes": len(source),
            "targets": [prepared_target],
        }
        prepare_hash = prepare_receipt_sha256(prepared)
        mutation_target = {
            key: copy.deepcopy(value)
            for key, value in prepared_target.items()
            if key not in {"prepared_absent", "status"}
        }
        mutation_target.update(
            {
                "observed_before_state": "absent",
                "observed_after_state": "created_exact",
                "observed_after_class_name": "ModuleScript",
                "observed_after_sha256": planned_sha,
                "status": "created",
            }
        )
        mutation = {
            "phase": "apply",
            "studio_id": "studio",
            "client_instance_id": "client",
            "document_epoch": "document",
            "generation": 1,
            "request_id": "apply",
            "transaction_id": "transaction",
            "prepare_request_id": "prepare",
            "prepare_sha256": prepare_hash,
            "ordering_version": MULTI_EDIT_ORDERING_VERSION,
            "atomicity": MULTI_EDIT_ATOMICITY,
            "receipt_contract": MULTI_EDIT_RECEIPT_CONTRACT,
            "evidence_mode": "apply_execution",
            "prior_terminal_outcome": "",
            "prior_terminal_receipt_sha256": "",
            "outcome": "applied",
            "safe_terminal": True,
            "recovery_required": False,
            "target_count": 1,
            "edit_count": 0,
            "create_count": 1,
            "targets": [mutation_target],
        }
        mutation_hash = mutation_receipt_sha256(mutation)

        prepare_fields = (
            "kind",
            "path",
            "parent_path",
            "name",
            "class_name",
            "expected_absent",
            "prepared_absent",
            "planned_sha256",
            "planned_source_length",
            "status",
        )
        for field in prepare_fields:
            drifted = copy.deepcopy(prepared)
            value = drifted["targets"][0][field]
            if isinstance(value, bool):
                drifted["targets"][0][field] = not value
            elif isinstance(value, int):
                drifted["targets"][0][field] = value + 1
            elif isinstance(value, list):
                drifted["targets"][0][field] = value + ["Drift"]
            else:
                drifted["targets"][0][field] = value + "-drift"
            with self.subTest(receipt="prepare", field=field):
                self.assertNotEqual(
                    prepare_hash, prepare_receipt_sha256(drifted)
                )

        mutation_fields = (
            "observed_before_state",
            "observed_after_state",
            "observed_after_class_name",
            "observed_after_sha256",
            "status",
        )
        for field in mutation_fields:
            drifted = copy.deepcopy(mutation)
            drifted["targets"][0][field] += "-drift"
            with self.subTest(receipt="mutation", field=field):
                self.assertNotEqual(
                    mutation_hash, mutation_receipt_sha256(drifted)
                )

    def test_job_admission_hashes_source_without_retaining_it(self) -> None:
        arguments = normalize_multi_edit_arguments(
            {
                "datamodel_type": "Edit",
                "targets": [_edit()],
                "creates": [_create(source="private source marker")],
            }
        )
        contract = _job_admitted_contract(
            "studio_multi_edit", arguments
        )
        self.assertEqual("studio-job-admission-v2", contract["contract_version"])
        self.assertEqual(2, contract["target_count"])
        self.assertEqual(1, contract["edit_count"])
        self.assertEqual(1, contract["create_count"])
        self.assertEqual(
            ["edit", "create"],
            [target["kind"] for target in contract["targets"]],
        )
        create = contract["targets"][1]
        self.assertEqual(
            hashlib.sha256(b"private source marker").hexdigest(),
            create["source_sha256"],
        )
        self.assertEqual(21, create["source_length"])
        self.assertNotIn("source", create)
        self.assertNotIn("private source marker", repr(contract))


class ScriptLifecycleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.studio = await FakeStudio.create(
            self.registry,
            "Lifecycle host",
            {"studio_multi_edit", "studio_recover_multi_edit"},
        )
        self.source = "return {created = true}\n"
        self.arguments = {
            "datamodel_type": "Edit",
            "targets": [],
            "creates": [_create(source=self.source)],
        }

    def _prepared(self, request):
        create = request["args"]["creates"][0]
        source_bytes = create["source"].encode("utf-8")
        target = {
            "index": 1,
            "kind": "create",
            "path": create["parent_path"] + [create["name"]],
            "parent_path": copy.deepcopy(create["parent_path"]),
            "name": create["name"],
            "class_name": create["class_name"],
            "expected_absent": True,
            "prepared_absent": True,
            "planned_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "planned_source_length": len(source_bytes),
            "status": "prepared",
        }
        receipt = {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 2,
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
            "edit_count": 0,
            "create_count": 1,
            "aggregate_source_bytes": 0,
            "aggregate_planned_source_bytes": len(source_bytes),
            "targets": [target],
            "expires_in_ms": 120_000,
        }
        receipt["prepare_sha256"] = prepare_receipt_sha256(receipt)
        return receipt

    def _mutation(
        self,
        request,
        prepared,
        *,
        recovery: bool = False,
        outcome: str = "applied",
        before: str = "absent",
        after: str = "created_exact",
        status: str = "created",
        evidence_mode: str | None = None,
        prior_terminal_outcome: str = "",
        prior_terminal_receipt_sha256: str = "",
    ):
        expected = prepared["targets"][0]
        exact_after = after == "created_exact"
        receipt = {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 2,
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
            "evidence_mode": (
                evidence_mode
                if evidence_mode is not None
                else (
                    "live_recovery"
                    if recovery
                    else "apply_execution"
                )
            ),
            "prior_terminal_outcome": prior_terminal_outcome,
            "prior_terminal_receipt_sha256": (
                prior_terminal_receipt_sha256
            ),
            "outcome": outcome,
            "safe_terminal": outcome != "recovery_required",
            "recovery_required": outcome == "recovery_required",
            "target_count": 1,
            "edit_count": 0,
            "create_count": 1,
            "targets": [
                {
                    "index": 1,
                    "kind": "create",
                    "path": copy.deepcopy(expected["path"]),
                    "parent_path": copy.deepcopy(
                        expected["parent_path"]
                    ),
                    "name": expected["name"],
                    "class_name": expected["class_name"],
                    "expected_absent": True,
                    "planned_sha256": expected["planned_sha256"],
                    "planned_source_length": expected[
                        "planned_source_length"
                    ],
                    "observed_before_state": before,
                    "observed_after_state": after,
                    "observed_after_class_name": (
                        expected["class_name"] if exact_after else ""
                    ),
                    "observed_after_sha256": (
                        expected["planned_sha256"] if exact_after else ""
                    ),
                    "status": status,
                }
            ],
        }
        receipt["receipt_sha256"] = mutation_receipt_sha256(receipt)
        return receipt

    async def test_creation_only_two_phase_receipts_are_identity_bound(
        self,
    ) -> None:
        operation = asyncio.create_task(
            self.studio.session.invoke(
                "studio_multi_edit",
                copy.deepcopy(self.arguments),
                2_000,
            )
        )
        prepare_request = await self.studio.next_request()
        self.assertEqual([], prepare_request["args"]["targets"])
        self.assertEqual(
            self.arguments["creates"],
            prepare_request["args"]["creates"],
        )
        prepared = self._prepared(prepare_request)
        self.assertTrue(self.studio.respond(prepare_request, prepared))

        apply_request = await self.studio.next_request()
        self.assertEqual(
            prepared["targets"],
            apply_request["args"]["prepared_targets"],
        )
        applied = self._mutation(apply_request, prepared)
        self.assertTrue(self.studio.respond(apply_request, applied))
        self.assertEqual(applied, await operation)
        self.assertIsNone(
            self.studio.session.multi_edit_prepared_receipt
        )
        self.assertIsNone(self.studio.session.multi_edit_recovery)

    async def test_changed_creation_requires_exact_same_generation_recovery(
        self,
    ) -> None:
        operation = asyncio.create_task(
            self.studio.session.invoke(
                "studio_multi_edit",
                copy.deepcopy(self.arguments),
                2_000,
            )
        )
        prepare_request = await self.studio.next_request()
        prepared = self._prepared(prepare_request)
        self.assertTrue(self.studio.respond(prepare_request, prepared))
        apply_request = await self.studio.next_request()
        uncertain = self._mutation(
            apply_request,
            prepared,
            outcome="recovery_required",
            before="absent",
            after="present_unproven",
            status="recovery_required",
        )
        self.assertTrue(self.studio.respond(apply_request, uncertain))
        self.assertEqual(uncertain, await operation)
        fence = self.studio.session.multi_edit_recovery
        self.assertEqual(
            prepared["transaction_id"], fence["transaction_id"]
        )
        self.assertEqual(self.studio.generation, fence["generation"])

        recovery_task = asyncio.create_task(
            self.studio.session.invoke(
                "studio_recover_multi_edit",
                {"transaction_id": prepared["transaction_id"]},
                2_000,
            )
        )
        recovery_request = await self.studio.next_request()
        recovered = self._mutation(
            recovery_request,
            prepared,
            recovery=True,
            outcome="recovered",
            before="created_exact",
            after="absent",
            status="rolled_back",
        )
        self.assertTrue(
            self.studio.respond(recovery_request, recovered)
        )
        self.assertEqual(recovered, await recovery_task)
        self.assertIsNone(self.studio.session.multi_edit_recovery)
        self.assertFalse(self.studio.session.uncertain_requests)

    async def test_tampered_create_readback_is_rejected_and_quarantined(
        self,
    ) -> None:
        operation = asyncio.create_task(
            self.studio.session.invoke(
                "studio_multi_edit",
                copy.deepcopy(self.arguments),
                2_000,
            )
        )
        prepare_request = await self.studio.next_request()
        prepared = self._prepared(prepare_request)
        self.assertTrue(self.studio.respond(prepare_request, prepared))
        apply_request = await self.studio.next_request()
        tampered = self._mutation(apply_request, prepared)
        tampered["targets"][0]["observed_after_sha256"] = "f" * 64
        tampered["receipt_sha256"] = mutation_receipt_sha256(tampered)
        self.assertTrue(self.studio.respond(apply_request, tampered))
        with self.assertRaises(RemoteToolError):
            await operation
        self.assertIsNotNone(self.studio.session.multi_edit_recovery)
        self.assertIn(
            apply_request["request_id"],
            self.studio.session.uncertain_requests,
        )

    async def test_apply_compensation_receipt_accepts_only_exact_absence(
        self,
    ) -> None:
        operation = asyncio.create_task(
            self.studio.session.invoke(
                "studio_multi_edit",
                copy.deepcopy(self.arguments),
                2_000,
            )
        )
        prepare_request = await self.studio.next_request()
        prepared = self._prepared(prepare_request)
        self.assertTrue(self.studio.respond(prepare_request, prepared))
        apply_request = await self.studio.next_request()
        rolled_back = self._mutation(
            apply_request,
            prepared,
            outcome="rolled_back",
            before="created_exact",
            after="absent",
            status="rolled_back",
        )
        self.assertTrue(
            self.studio.respond(apply_request, rolled_back)
        )
        self.assertEqual(rolled_back, await operation)

        target = copy.deepcopy(rolled_back["targets"][0])
        target["observed_after_state"] = "present_unproven"
        self.assertFalse(
            self.studio.session._valid_multi_edit_create_outcome(
                target,
                prepared["targets"][0],
                1,
                expected_phase="apply",
                outcome="rolled_back",
                evidence_mode="apply_execution",
            )[0]
        )

    async def test_cached_safe_terminal_create_conflict_is_explicitly_audited(
        self,
    ) -> None:
        request = {
            "generation": self.studio.generation,
            "request_id": "recovery-request",
        }
        prepared_request = {
            "generation": self.studio.generation,
            "request_id": "prepare-request",
            "args": {
                "transaction_id": (
                    "00000000-0000-4000-8000-000000000111"
                ),
                "creates": copy.deepcopy(self.arguments["creates"]),
            },
        }
        prepared = self._prepared(prepared_request)
        self.studio.session.multi_edit_prepared_receipt = copy.deepcopy(
            prepared
        )
        pending = PendingRequest(
            request_id=request["request_id"],
            generation=request["generation"],
            remote_tool="studio_recover_multi_edit",
            arguments={
                "transaction_id": prepared["transaction_id"]
            },
            future=asyncio.get_running_loop().create_future(),
        )
        cached = self._mutation(
            request,
            prepared,
            recovery=True,
            outcome="recovered",
            before="present_unproven",
            after="present_unproven",
            status="not_created",
            evidence_mode="cached_safe_terminal",
            prior_terminal_outcome="aborted_preflight",
            prior_terminal_receipt_sha256="a" * 64,
        )
        self.assertTrue(
            self.studio.session._valid_multi_edit_mutation_result(
                pending, cached
            )
        )

        for state in ("absent", "unavailable"):
            with self.subTest(state=state):
                variant = self._mutation(
                    request,
                    prepared,
                    recovery=True,
                    outcome="recovered",
                    before=state,
                    after=state,
                    status="not_created",
                    evidence_mode="cached_safe_terminal",
                    prior_terminal_outcome="recovered",
                    prior_terminal_receipt_sha256="b" * 64,
                )
                self.assertTrue(
                    self.studio.session._valid_multi_edit_mutation_result(
                        pending, variant
                    )
                )

        live_conflict = copy.deepcopy(cached)
        live_conflict["evidence_mode"] = "live_recovery"
        live_conflict["prior_terminal_outcome"] = ""
        live_conflict["prior_terminal_receipt_sha256"] = ""
        live_conflict["receipt_sha256"] = mutation_receipt_sha256(
            live_conflict
        )
        self.assertFalse(
            self.studio.session._valid_multi_edit_mutation_result(
                pending, live_conflict
            )
        )

        invalid_prior = copy.deepcopy(cached)
        invalid_prior["prior_terminal_receipt_sha256"] = "not-a-sha"
        invalid_prior["receipt_sha256"] = mutation_receipt_sha256(
            invalid_prior
        )
        self.assertFalse(
            self.studio.session._valid_multi_edit_mutation_result(
                pending, invalid_prior
            )
        )
        pending.future.cancel()


if __name__ == "__main__":
    unittest.main()
