from __future__ import annotations

import dataclasses
import re
import unittest
from pathlib import Path

from scripts import render_studio_plugin


ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
TOKEN = "t" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"

MAX_REPLACEMENT_SPANS = 1_024
MAX_AGGREGATE_PATH_BYTES = 8_192
MAX_AGGREGATE_SOURCE_BYTES = 1_048_576
MAX_RECEIPT_BYTES = 100_000


class ModelOverlapError(ValueError):
    pass


class ModelBoundError(ValueError):
    pass


@dataclasses.dataclass
class TaintedRange:
    start: int
    end: int
    zero_width: bool


def _unique_match(source: bytes, old: bytes) -> tuple[int, int]:
    start = source.find(old)
    if start < 0:
        raise ValueError("missing match")
    if source.find(old, start + 1) >= 0:
        raise ValueError("ambiguous match")
    return start, start + len(old)


def _overlaps_taint(
    match: tuple[int, int],
    taint: TaintedRange,
) -> bool:
    start, end = match
    if taint.zero_width:
        return start < taint.start < end
    return start < taint.end and taint.start < end


def sequential_edit_model(
    source: bytes,
    edits: list[tuple[bytes, bytes]],
) -> bytes:
    """Model the handler's byte ranges and deletion-boundary provenance."""

    tainted: list[TaintedRange] = []
    for old, new in edits:
        match = _unique_match(source, old)
        if any(_overlaps_taint(match, prior) for prior in tainted):
            raise ModelOverlapError("edit overlaps an earlier changed range")

        start, end = match
        delta = len(new) - (end - start)
        for prior in tainted:
            if prior.start >= end:
                prior.start += delta
                prior.end += delta

        source = source[:start] + new + source[end:]
        if new:
            tainted.append(
                TaintedRange(
                    start=start,
                    end=start + len(new),
                    zero_width=False,
                )
            )
        else:
            tainted.append(
                TaintedRange(
                    start=start,
                    end=start,
                    zero_width=True,
                )
            )
    return source


def enforce_aggregate_model(
    *,
    path_bytes: list[int],
    replacement_counts: list[int],
    source_bytes: list[int],
    planned_source_bytes: list[int],
    receipt: bytes,
) -> None:
    checks = (
        (sum(path_bytes), MAX_AGGREGATE_PATH_BYTES, "path"),
        (
            sum(replacement_counts),
            MAX_REPLACEMENT_SPANS,
            "replacement",
        ),
        (
            sum(source_bytes),
            MAX_AGGREGATE_SOURCE_BYTES,
            "source",
        ),
        (
            sum(planned_source_bytes),
            MAX_AGGREGATE_SOURCE_BYTES,
            "planned source",
        ),
        (len(receipt), MAX_RECEIPT_BYTES, "receipt"),
    )
    for observed, maximum, label in checks:
        if observed > maximum:
            raise ModelBoundError(f"{label} bound exceeded")


def create_compensation_allowed(
    *,
    same_retained_instance: bool,
    exact_unique_path: bool,
    class_matches: bool,
    source_bytes_match: bool,
    source_sha256_matches: bool,
) -> bool:
    """Model the handler's deliberately narrow create-compensation fence."""

    return all(
        (
            same_retained_instance,
            exact_unique_path,
            class_matches,
            source_bytes_match,
            source_sha256_matches,
        )
    )


def created_cleanup_allowed(
    *,
    same_generation: bool,
    same_retained_instance: bool,
    same_retained_parent: bool,
    exact_unique_path: bool,
    name_matches: bool,
    class_matches: bool,
    source_bytes_match: bool,
    source_sha256_matches: bool,
    no_property_change_observed: bool,
    property_fingerprint_matches: bool,
    change_tracker_connected: bool,
    no_children: bool,
    no_attributes: bool,
    no_tags: bool,
) -> bool:
    """Model the cleanup grant's all-target destructive fence."""

    return all(
        (
            same_generation,
            same_retained_instance,
            same_retained_parent,
            exact_unique_path,
            name_matches,
            class_matches,
            source_bytes_match,
            source_sha256_matches,
            no_property_change_observed,
            property_fingerprint_matches,
            change_tracker_connected,
            no_children,
            no_attributes,
            no_tags,
        )
    )


def cleanup_retry_model(states: list[str]) -> tuple[str, list[str]]:
    """Model same-grant cleanup reconciliation after a partial first pass."""

    if any(state not in {"created_exact", "absent"} for state in states):
        return "refused", [
            (
                "already_absent"
                if state == "absent"
                else (
                    "not_deleted"
                    if state == "created_exact"
                    else "preserved_conflict"
                )
            )
            for state in states
        ]
    return "cleaned", [
        "deleted" if state == "created_exact" else "already_absent"
        for state in states
    ]


def create_failure_status(*, mutated: bool, observed_after_state: str) -> str:
    if mutated or observed_after_state != "absent":
        return "recovery_required"
    return "not_created"


def recovery_plan_status_allowed(status: str) -> bool:
    return status in {
        "prepared",
        "applying",
        "applied",
        "rolled_back",
        "aborted_preflight",
        "recovery_required",
        "recovered",
    }


def created_content_empty(
    *,
    read_succeeded: bool,
    child_count: int,
    attribute_count: int,
    tag_count: int,
) -> bool:
    return (
        read_succeeded
        and child_count == 0
        and attribute_count == 0
        and tag_count == 0
    )


def aggregate_recovery_required(statuses: list[str]) -> bool:
    return any(status == "recovery_required" for status in statuses)


def _section(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


class Phase2MultiEditLuauTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HANDLERS.read_text(encoding="utf-8")
        cls.rendered = render_studio_plugin.render_durable(TOKEN, RUN_ID)

    def test_zero_width_deletion_allows_edit_at_boundary(self) -> None:
        self.assertEqual(
            b"B",
            sequential_edit_model(
                b"ab",
                [
                    (b"a", b""),
                    (b"b", b"B"),
                ],
            ),
        )

    def test_match_spanning_deleted_boundary_is_rejected(self) -> None:
        with self.assertRaises(ModelOverlapError):
            sequential_edit_model(
                b"abc",
                [
                    (b"b", b""),
                    (b"ac", b"AC"),
                ],
            )

    def test_aggregate_model_accepts_exact_limits(self) -> None:
        enforce_aggregate_model(
            path_bytes=[6_400, 1_792],
            replacement_counts=[512, 512],
            source_bytes=[262_144] * 4,
            planned_source_bytes=[262_144] * 4,
            receipt=b"x" * 100_000,
        )

    def test_each_aggregate_model_limit_rejects_plus_one(self) -> None:
        valid = {
            "path_bytes": [8_192],
            "replacement_counts": [1_024],
            "source_bytes": [262_144] * 4,
            "planned_source_bytes": [262_144] * 4,
            "receipt": b"x" * 100_000,
        }
        overflows = {
            "path_bytes": [8_192, 1],
            "replacement_counts": [1_024, 1],
            "source_bytes": [262_144] * 4 + [1],
            "planned_source_bytes": [262_144] * 4 + [1],
            "receipt": b"x" * 100_001,
        }
        for field, overflow in overflows.items():
            arguments = dict(valid)
            arguments[field] = overflow
            with self.subTest(field=field), self.assertRaises(
                ModelBoundError
            ):
                enforce_aggregate_model(**arguments)

    def test_luau_deletion_boundary_contract_matches_model(self) -> None:
        planner = _section(
            self.source,
            "local function multiEditRangesOverlap(",
            "local function prepareMultiEdit(",
        )
        for marker in (
            "tainted.zero_width == true",
            "match.start_index < tainted.start_index",
            "match.end_index >= tainted.start_index",
            "zero_width = false",
            "zero_width = true",
            "end_index = match.start_index - 1",
            "A deletion changes the boundary, not an occupied byte",
            "a→\"\" followed by",
        ):
            self.assertIn(marker, planner)
        self.assertNotIn(
            "local taintedEnd = newLength > 0",
            planner,
        )

    def test_luau_enforces_all_transaction_wide_bounds(self) -> None:
        for marker in (
            "MAX_MULTI_EDIT_REPLACEMENT_SPANS = 1_024",
            "MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES = 8_192",
            "MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES = 1_048_576",
            "MAX_MULTI_EDIT_RECEIPT_BYTES = 100_000",
        ):
            self.assertIn(marker, self.source)

        validation = _section(
            self.source,
            "local function validateMultiEditTargets(",
            "local function validateDurableArgs(",
        )
        self.assertEqual(
            3,
            validation.count(
                "aggregatePathBytes > "
                "DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES"
            ),
        )

        prepare = _section(
            self.source,
            "local function prepareMultiEdit(",
            "local function multiEditCasReplace(",
        )
        self.assertIn(
            "aggregateReplacementCount + replacementCount",
            prepare,
        )
        self.assertIn(
            "aggregateReplacementCount "
            "> DURABLE_BOUNDS.MAX_MULTI_EDIT_REPLACEMENT_SPANS",
            prepare,
        )
        self.assertIn(
            "aggregateSourceBytes "
            "> DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES",
            prepare,
        )
        self.assertIn(
            "aggregatePlannedSourceBytes\n"
            "\t\t\t\t> "
            "DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES",
            prepare,
        )

    def test_receipt_bounds_are_proved_before_mutation(self) -> None:
        self.assertEqual(
            3,
            self.source.count(
                "#encodedReceipt > "
                "DURABLE_BOUNDS.MAX_MULTI_EDIT_RECEIPT_BYTES"
            ),
        )
        prepare = _section(
            self.source,
            "local function prepareMultiEdit(",
            "local function multiEditCasReplace(",
        )
        self.assertLess(
            prepare.index(
                "#encodedReceipt > "
                "DURABLE_BOUNDS.MAX_MULTI_EDIT_RECEIPT_BYTES"
            ),
            prepare.index("peer.multi_edit_plan = {"),
        )

        apply = _section(
            self.source,
            "local function applyMultiEdit(",
            "local function recoverMultiEdit(",
        )
        self.assertLess(
            apply.index(
                'assertMultiEditMutationReceiptBound(plan, requestId, "apply")'
            ),
            apply.index("local outcomes = {}"),
        )
        self.assertLess(
            apply.index(
                'assertMultiEditMutationReceiptBound(plan, requestId, "apply")'
            ),
            apply.index("multiEditCasReplace("),
        )

        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function newMultiEditCleanupTargetOutcome(",
        )
        self.assertLess(
            recovery.index(
                'assertMultiEditMutationReceiptBound(plan, requestId, "recover")'
            ),
            recovery.index("local outcomes = {}"),
        )
        self.assertLess(
            recovery.index(
                'assertMultiEditMutationReceiptBound(plan, requestId, "recover")'
            ),
            recovery.index("multiEditCasReplace("),
        )

    def test_deadline_is_threaded_through_prepare_planner(self) -> None:
        planner = _section(
            self.source,
            "local function requireMultiEditTime(",
            "local function multiEditCasReplace(",
        )
        self.assertIn("if os.clock() > deadline then", planner)
        self.assertIn('"multi_edit_deadline"', planner)
        self.assertIn(
            "local function prepareMultiEdit(args, requestId, deadlineMs)",
            planner,
        )
        self.assertIn(
            "local deadline = os.clock() + (deadlineMs / 1_000)",
            planner,
        )
        self.assertGreaterEqual(
            planner.count("requireMultiEditTime(deadline)"),
            5,
        )
        self.assertIn(
            "planMultiEditSource(\n"
            "\t\tsource,\n"
            "\t\trequested.edits,\n"
            "\t\tdeadline",
            planner,
        )

    def test_apply_deadline_checks_preflight_before_write_and_fifo(self) -> None:
        apply = _section(
            self.source,
            "local function applyMultiEdit(",
            "local function recoverMultiEdit(",
        )
        self.assertIn(
            "local function applyMultiEdit(args, requestId, deadlineMs)",
            apply,
        )
        self.assertEqual(3, apply.count("if os.clock() > deadline then"))

        preflight_deadline = apply.index("if os.clock() > deadline then")
        first_resolve = apply.index("requireLuaSourceContainer(planTarget.path)")
        before_write_deadline = apply.index(
            "if os.clock() > deadline then",
            preflight_deadline + 1,
        )
        apply_loop_deadline = apply.index(
            "if os.clock() > deadline then",
            before_write_deadline + 1,
        )
        first_cas = apply.index("multiEditCasReplace(")
        self.assertLess(preflight_deadline, first_resolve)
        self.assertLess(first_resolve, before_write_deadline)
        self.assertLess(before_write_deadline, apply_loop_deadline)
        self.assertLess(apply_loop_deadline, first_cas)
        self.assertIn('plan.status = "rolled_back"', apply)
        self.assertIn(
            '"apply",\n\t\t\t"rolled_back",\n\t\t\ttrue,',
            apply,
        )

    def test_apply_cas_cannot_claim_an_already_planned_revision(self) -> None:
        cas = _section(
            self.source,
            "local function multiEditCasReplace(",
            "local function newMultiEditTargetOutcome(",
        )
        self.assertIn("allowAlreadyReplacement", cas)
        self.assertIn(
            "allowAlreadyReplacement == true\n"
            "\t\t\t\tand currentSha256 == replacementSha256",
            cas,
        )
        self.assertIn(
            "allowAlreadyReplacement == true\n"
            "\t\t\t\tand confirmedSha256 == replacementSha256",
            cas,
        )

        apply = _section(
            self.source,
            "local function applyMultiEdit(",
            "local function recoverMultiEdit(",
        )
        self.assertEqual(
            1,
            apply.count(
                "planTarget.prepared_sha256,\n"
                "\t\t\t\tplanTarget.planned_source,\n"
                "\t\t\t\tfalse"
            ),
        )
        self.assertEqual(
            1,
            apply.count(
                "planTarget.planned_sha256,\n"
                "\t\t\t\t\tplanTarget.original_source,\n"
                "\t\t\t\t\ttrue"
            ),
        )

        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function newMultiEditCleanupTargetOutcome(",
        )
        self.assertEqual(
            1,
            recovery.count(
                "planTarget.planned_sha256,\n"
                "\t\t\t\t\t\tplanTarget.original_source,\n"
                "\t\t\t\t\t\ttrue"
            ),
        )

    def test_recovery_deadline_fails_closed_without_new_mutation(self) -> None:
        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function newMultiEditCleanupTargetOutcome(",
        )
        self.assertIn(
            "local function recoverMultiEdit(args, requestId, deadlineMs)",
            recovery,
        )
        self.assertEqual(1, recovery.count("if os.clock() > deadline then"))
        deadline = recovery.index("if os.clock() > deadline then")
        recovery_cas = recovery.index("multiEditCasReplace(")
        self.assertLess(deadline, recovery_cas)
        for marker in (
            'outcome.status = "recovery_required"',
            "recoveryRequired = true",
            "continue",
        ):
            self.assertIn(marker, recovery[deadline:recovery_cas])

    def test_recovery_rejects_a_plan_from_an_old_generation(self) -> None:
        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function newMultiEditCleanupTargetOutcome(",
        )
        generation_fence = (
            "or plan.generation ~= peer.generation"
        )
        self.assertIn(generation_fence, recovery)
        self.assertLess(
            recovery.index(generation_fence),
            recovery.index(
                'assertMultiEditMutationReceiptBound('
                'plan, requestId, "recover")'
            ),
        )
        self.assertLess(
            recovery.index(generation_fence),
            recovery.index("multiEditCasReplace("),
        )

    def test_dispatch_passes_exact_request_deadline_to_every_phase(self) -> None:
        dispatch = _section(
            self.source,
            "local function dispatch(request)",
            'adapterError("unsupported_operation"',
        )
        self.assertEqual(4, dispatch.count("request.deadline_ms"))
        for function_name in (
            "prepareMultiEdit",
            "applyMultiEdit",
            "recoverMultiEdit",
            "cleanupMultiEdit",
        ):
            call = dispatch[
                dispatch.index(function_name + "(") :
                dispatch.index(")", dispatch.index(function_name + "(")) + 1
            ]
            self.assertIn("request.deadline_ms", call)

    def test_create_compensation_model_fails_closed_on_every_drift(self) -> None:
        exact = {
            "same_retained_instance": True,
            "exact_unique_path": True,
            "class_matches": True,
            "source_bytes_match": True,
            "source_sha256_matches": True,
        }
        self.assertTrue(create_compensation_allowed(**exact))
        for field in exact:
            drifted = dict(exact)
            drifted[field] = False
            with self.subTest(field=field):
                self.assertFalse(create_compensation_allowed(**drifted))

    def test_cleanup_model_requires_every_identity_and_content_fence(
        self,
    ) -> None:
        exact = {
            "same_generation": True,
            "same_retained_instance": True,
            "same_retained_parent": True,
            "exact_unique_path": True,
            "name_matches": True,
            "class_matches": True,
            "source_bytes_match": True,
            "source_sha256_matches": True,
            "no_property_change_observed": True,
            "property_fingerprint_matches": True,
            "change_tracker_connected": True,
            "no_children": True,
            "no_attributes": True,
            "no_tags": True,
        }
        self.assertTrue(created_cleanup_allowed(**exact))
        for field in exact:
            drifted = dict(exact)
            drifted[field] = False
            with self.subTest(field=field):
                self.assertFalse(created_cleanup_allowed(**drifted))

    def test_cleanup_contract_is_targetless_closed_and_hash_ordered(
        self,
    ) -> None:
        operation_keys = _section(
            self.source,
            "local OPERATION_ARG_KEYS = table.freeze({",
            "local function isFiniteNumber(",
        )
        cleanup_keys = _section(
            operation_keys,
            "\tstudio_cleanup_multi_edit = table.freeze({",
            "\t}),",
        )
        for field in (
            "transaction_id = true",
            "apply_receipt_sha256 = true",
            "cleanup_authorization_sha256 = true",
        ):
            self.assertIn(field, cleanup_keys)
        for forbidden in ("path", "source", "class_name", "name"):
            self.assertNotIn(forbidden, cleanup_keys)

        hashing = _section(
            self.source,
            "local function multiEditMutationSha256(",
            "local function copyPreparedMultiEditTarget(",
        )
        mutation_order = (
            "receipt.recovery_required",
            "receipt.cleanup_authorized",
            "receipt.cleanup_contract",
            "receipt.cleanup_authorization_sha256",
            "receipt.cleanup_expires_in_ms",
            "receipt.target_count",
            "receipt.edit_count",
            "receipt.create_count",
        )
        positions = [hashing.index(marker) for marker in mutation_order]
        self.assertEqual(positions, sorted(positions))

        authorization = _section(
            hashing,
            "local function multiEditCleanupAuthorizationSha256(",
            "local function multiEditCleanupSha256(",
        )
        authorization_order = (
            '"studio-multi-edit-cleanup-authorization-v1"',
            "peer.studio_id",
            "CLIENT_INSTANCE_ID",
            "DOCUMENT_EPOCH",
            "peer.generation",
            "plan.transaction_id",
            "plan.prepare_request_id",
            "plan.prepare_sha256",
            "applyRequestId",
            "PROTOCOL_METADATA.MULTI_EDIT_CLEANUP_CONTRACT",
            "DURABLE_BOUNDS.MULTI_EDIT_CLEANUP_TTL_SECONDS * 1_000",
        )
        positions = []
        cursor = 0
        for marker in authorization_order:
            cursor = authorization.index(marker, cursor)
            positions.append(cursor)
            cursor += len(marker)
        self.assertEqual(positions, sorted(positions))

        cleanup_hash = _section(
            self.source,
            "local function multiEditCleanupSha256(",
            "local function copyPreparedMultiEditTarget(",
        )
        self.assertIn('"studio-multi-edit-cleanup-v1"', cleanup_hash)
        self.assertIn(
            "appendCanonicalMultiEditValue(parts, #receipt.targets)",
            cleanup_hash,
        )
        target_order = (
            "target.index",
            "target.kind",
            "target.path",
            "target.parent_path",
            '"name"',
            '"class_name"',
            '"expected_absent"',
            '"planned_sha256"',
            '"planned_source_length"',
            '"observed_before_state"',
            '"observed_after_state"',
            '"observed_after_class_name"',
            '"observed_after_sha256"',
            '"status"',
        )
        positions = [cleanup_hash.index(marker) for marker in target_order]
        self.assertEqual(positions, sorted(positions))

    def test_applied_creation_retains_one_separate_ten_minute_grant(
        self,
    ) -> None:
        self.assertIn(
            "MULTI_EDIT_CLEANUP_TTL_SECONDS = 600",
            self.source,
        )
        installer = _section(
            self.source,
            "local function installMultiEditCleanupGrant(",
            "local function preparedMultiEditTargetMatches(",
        )
        for marker in (
            "peer.multi_edit_cleanup_grant = {",
            "transaction_id = plan.transaction_id",
            "document_epoch = DOCUMENT_EPOCH",
            "generation = peer.generation",
            "apply_request_id = applyRequestId",
            "apply_receipt_sha256 = applyReceiptSha256",
            "cleanup_authorization_sha256 =",
            "observeMultiEditCreate(planTarget)",
            'state ~= "created_exact"',
            "planned_source = planTarget.planned_source",
            "created_instance = planTarget.created_instance",
            "parent = planTarget.parent",
        ):
            self.assertIn(marker, installer)
        self.assertNotIn("peer.multi_edit_plan =", installer)

        receipt = _section(
            self.source,
            "local function newMultiEditMutationReceipt(",
            "local function assertMultiEditMutationReceiptBound(",
        )
        self.assertIn(
            'local cleanupAuthorized = phase == "apply"\n'
            '\t\tand outcome == "applied"\n'
            "\t\tand plan.create_count > 0",
            receipt,
        )
        self.assertIn("installMultiEditCleanupGrant(", receipt)

        prepare = _section(
            self.source,
            "local function prepareMultiEdit(",
            "local function multiEditCasReplace(",
        )
        self.assertIn(
            "if createCount > 0 and cleanupGrant ~= nil then",
            prepare,
        )
        self.assertIn(
            'cleanupGrant.status == "cleanup_required"',
            prepare,
        )
        self.assertNotIn(
            "peer.multi_edit_cleanup_grant = nil\n"
            "\tlocal planTargets",
            prepare,
        )

    def test_cleanup_preflights_every_target_before_exact_destroy(
        self,
    ) -> None:
        cleanup = _section(
            self.source,
            "local function cleanupMultiEdit(",
            "local function readScript(",
        )
        preflight = cleanup.index(
            "for index, cleanupTarget in ipairs(grant.targets) do"
        )
        refused = cleanup.index('grant.status = "refused"')
        bounded = cleanup.index("local worstCaseOutcomes = {}")
        mutation = cleanup.index(
            "for index = #grant.targets, 1, -1 do"
        )
        destroy = cleanup.index(
            "compensateMultiEditCreate(\n"
            "\t\t\t\t\tcleanupTarget,\n"
            "\t\t\t\t\tgrant.expires_at"
        )
        self.assertLess(preflight, refused)
        self.assertLess(refused, bounded)
        self.assertLess(bounded, mutation)
        self.assertLess(mutation, destroy)
        for marker in (
            'state == "created_exact"',
            'state == "absent"',
            'targetOutcome.status = "preserved_conflict"',
            'targetOutcome.status = "already_absent"',
            'targetOutcome.status = "cleanup_required"',
            'grant.status = "cleanup_required"',
            '"cleanup_required",\n'
            "\t\t\tfalse,",
        ):
            self.assertIn(marker, cleanup)

        exact_delete = _section(
            self.source,
            "local function compensateMultiEditCreate(",
            "local function installMultiEditCleanupGrant(",
        )
        for marker in (
            'beforeState ~= "created_exact"',
            "beforeClass ~= planTarget.class_name",
            "beforeDigest ~= planTarget.planned_sha256",
            'contentState ~= "empty"',
            "reboundParent ~= planTarget.parent",
            "matchCount ~= 1",
            "exact ~= created",
            'multiEditCreatedContentState(planTarget) ~= "empty"',
            'type(cleanupExpiresAt) == "number"',
            "os.clock() >= cleanupExpiresAt",
            "created:Destroy()",
            'afterState == "absent"',
        ):
            self.assertIn(marker, exact_delete)
        self.assertLess(
            exact_delete.index("os.clock() >= cleanupExpiresAt"),
            exact_delete.index("created:Destroy()"),
        )

    def test_cleanup_retry_reconciles_only_the_same_exact_grant(
        self,
    ) -> None:
        refresh = _section(
            self.source,
            "local function refreshMultiEditCleanupGrant(",
            "local function prepareMultiEdit(",
        )
        for marker in (
            "grant.generation ~= peer.generation",
            "grant.document_epoch ~= DOCUMENT_EPOCH",
            'grant.status == "cleanup_required"',
            "os.clock() >= grant.expires_at",
            "grant.settlement_only = true",
            "disconnectMultiEditCreatedTrackers(grant)",
            "peer.multi_edit_cleanup_grant = nil",
        ):
            self.assertIn(marker, refresh)

        cleanup = _section(
            self.source,
            "local function cleanupMultiEdit(",
            "local function readScript(",
        )
        identity_end = cleanup.index(
            "local evidence = grant.safe_terminal_evidence"
        )
        first_preflight = cleanup.index(
            "for index, cleanupTarget in ipairs(grant.targets) do"
        )
        first_destroy = cleanup.index(
            "compensateMultiEditCreate("
        )
        expiry_admission = cleanup.index(
            "if grant.settlement_only == true"
        )
        preflight_expiry = cleanup.index(
            "if os.clock() >= grant.expires_at",
            first_preflight,
        )
        mutation_expiry = cleanup.index(
            "if os.clock() >= grant.expires_at",
            preflight_expiry + 1,
        )
        for identity_field in (
            "grant.transaction_id ~= args.transaction_id",
            "grant.document_epoch ~= DOCUMENT_EPOCH",
            "grant.generation ~= peer.generation",
            "grant.apply_receipt_sha256",
            "~= args.apply_receipt_sha256",
            "grant.cleanup_authorization_sha256",
            "~= args.cleanup_authorization_sha256",
        ):
            self.assertIn(identity_field, cleanup[:identity_end])
        self.assertLess(identity_end, first_preflight)
        self.assertLess(expiry_admission, first_preflight)
        self.assertLess(preflight_expiry, first_destroy)
        self.assertLess(mutation_expiry, first_destroy)
        self.assertLess(first_preflight, first_destroy)
        for marker in (
            '"cleanup_execution"',
            '"cached_cleanup_terminal"',
            'grant.status == "cleaned" or grant.status == "refused"',
            'grant.status ~= "authorized"',
            'and grant.status ~= "cleanup_required"',
            'grant.status = "cleanup_required"',
            "authorizationExpired",
            "expiredDuringCleanup",
            '"multi_edit_cleanup_expired"',
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn(
            '"Cleanup has an unproven partial outcome and cannot be retried"',
            cleanup,
        )

        # The first pass deleted the second target but could not prove or
        # finish the first. The exact same grant then treats the retained,
        # already-absent target as terminal and deletes only the remaining
        # unchanged retained Instance.
        self.assertEqual(
            ("cleaned", ["deleted", "already_absent"]),
            cleanup_retry_model(["created_exact", "absent"]),
        )
        self.assertEqual(
            (
                "refused",
                ["not_deleted", "preserved_conflict"],
            ),
            cleanup_retry_model(
                ["created_exact", "present_unproven"]
            ),
        )

    def test_create_failure_model_quarantines_every_non_absent_state(
        self,
    ) -> None:
        self.assertEqual(
            "not_created",
            create_failure_status(
                mutated=False,
                observed_after_state="absent",
            ),
        )
        for mutated, state in (
            (True, "absent"),
            (False, "created_exact"),
            (False, "present_unproven"),
            (False, "unavailable"),
        ):
            with self.subTest(mutated=mutated, state=state):
                self.assertEqual(
                    "recovery_required",
                    create_failure_status(
                        mutated=mutated,
                        observed_after_state=state,
                    ),
                )

    def test_created_content_model_blocks_recursive_or_metadata_loss(
        self,
    ) -> None:
        exact = {
            "read_succeeded": True,
            "child_count": 0,
            "attribute_count": 0,
            "tag_count": 0,
        }
        self.assertTrue(created_content_empty(**exact))
        for field, value in (
            ("read_succeeded", False),
            ("child_count", 1),
            ("attribute_count", 1),
            ("tag_count", 1),
        ):
            drifted = dict(exact)
            drifted[field] = value
            with self.subTest(field=field):
                self.assertFalse(created_content_empty(**drifted))

    def test_created_property_drift_is_latched_and_fingerprinted_before_delete(
        self,
    ) -> None:
        fingerprint = _section(
            self.source,
            "local function multiEditCreatedPropertyFingerprint(",
            "local function multiEditCreatedContentState(",
        )
        for property_name in (
            "Archivable",
            "Sandboxed",
            "Capabilities",
            "DefinesCapabilities",
            "Disabled",
            "Enabled",
            "RunContext",
            "LinkedSource",
        ):
            self.assertIn(f'"{property_name}"', fingerprint)
        self.assertIn(
            "appendCanonicalMultiEditValue(parts, propertyName)",
            fingerprint,
        )
        self.assertIn("return sourceSha256(table.concat(parts))", fingerprint)

        content = _section(
            self.source,
            "local function multiEditCreatedContentState(",
            "local function observeMultiEditCreate(",
        )
        for marker in (
            "planTarget.created_change_tracker",
            "tracker.dirty ~= false",
            "tracker.connection == nil",
            "tracker.connection.Connected ~= true",
            "planTarget.created_property_fingerprint",
            "multiEditCreatedPropertyFingerprint(created)",
            'return "present_unproven"',
        ):
            self.assertIn(marker, content)

        apply_create = _section(
            self.source,
            "local function applyMultiEditCreate(",
            "local function compensateMultiEditCreate(",
        )
        connect = apply_create.index("created.Changed:Connect(")
        parent_write = apply_create.index("created.Parent = parent")
        fingerprint_capture = apply_create.index(
            "planTarget.created_property_fingerprint ="
        )
        readback = apply_create.index(
            "observeMultiEditCreate(planTarget)",
            fingerprint_capture,
        )
        self.assertLess(connect, parent_write)
        self.assertLess(parent_write, fingerprint_capture)
        self.assertLess(fingerprint_capture, readback)
        for marker in (
            'propertyName == "Parent"',
            "tracker.initial_parent_change_pending",
            "created.Parent == planTarget.parent",
            "tracker.dirty = true",
            "planTarget.created_change_tracker = tracker",
        ):
            self.assertIn(marker, apply_create)

        installer = _section(
            self.source,
            "local function installMultiEditCleanupGrant(",
            "local function preparedMultiEditTargetMatches(",
        )
        self.assertIn("created_change_tracker =", installer)
        self.assertIn("created_property_fingerprint =", installer)

        disconnect = _section(
            self.source,
            "local function disconnectMultiEditCreatedTrackers(",
            "local function refreshMultiEditCleanupGrant(",
        )
        self.assertIn("tracker.connection:Disconnect()", disconnect)
        self.assertIn("tracker.connection = nil", disconnect)
        cleanup = _section(
            self.source,
            "local function cleanupMultiEdit(",
            "local function readScript(",
        )
        self.assertGreaterEqual(
            cleanup.count("disconnectMultiEditCreatedTrackers(grant)"),
            2,
        )

    def test_lost_apply_receipt_plan_states_remain_recoverable(self) -> None:
        for status in (
            "prepared",
            "applying",
            "applied",
            "rolled_back",
            "aborted_preflight",
            "recovery_required",
            "recovered",
        ):
            with self.subTest(status=status):
                self.assertTrue(recovery_plan_status_allowed(status))
        for status in ("", "preparing", "unknown"):
            with self.subTest(status=status):
                self.assertFalse(recovery_plan_status_allowed(status))

    def test_aggregate_recovery_state_tracks_final_target_statuses(self) -> None:
        self.assertFalse(
            aggregate_recovery_required(
                ["rolled_back", "not_created", "rolled_back"]
            )
        )
        self.assertTrue(
            aggregate_recovery_required(
                ["rolled_back", "recovery_required", "not_created"]
            )
        )

    def test_create_schema_is_closed_bounded_and_expected_absent(self) -> None:
        validation = _section(
            self.source,
            "local MULTI_EDIT_TARGET_KEYS",
            "local function validateDurableArgs(",
        )
        self.assertIn("MAX_MULTI_EDIT_CREATES = 16", self.source)
        for marker in (
            "local MULTI_EDIT_CREATE_KEYS = table.freeze({",
            "parent_path = true",
            "name = true",
            "class_name = true",
            "source = true",
            "expected_absent = true",
            "targetCount + createCount < 1",
            "targetCount + createCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_TARGETS",
            'create.class_name,\n'
            '\t\t\t"creates[" .. tostring(createIndex) .. "].class_name"',
            "create.expected_absent ~= true",
            "not validUtf8(create.name)",
            "not validUtf8(create.source)",
            "#create.source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES",
            "Edit and create exact target paths must be unique",
        ):
            self.assertIn(marker, validation)
        self.assertIn(
            'className ~= "Script"\n'
            '\t\tand className ~= "LocalScript"\n'
            '\t\tand className ~= "ModuleScript"',
            validation,
        )

    def test_exact_paths_reject_c0_and_c1_unicode_controls(self) -> None:
        validation = _section(
            self.source,
            "local function validPathSegmentCodepoints(",
            "local function validateTreeFilter(",
        )
        for marker in (
            "for _, codepoint in utf8.codes(value) do",
            "codepoint <= 0x1f",
            "codepoint >= 0x7f and codepoint <= 0x9f",
            "or not validPathSegmentCodepoints(segment)",
        ):
            self.assertIn(marker, validation)

    def test_prepare_orders_edits_before_creates_and_preflights_absence(self) -> None:
        prepare = _section(
            self.source,
            "local function prepareMultiEditCreateTarget(",
            "local function multiEditCasReplace(",
        )
        edit_loop = prepare.index(
            "for targetIndex, requested in ipairs(args.targets) do"
        )
        create_loop = prepare.index(
            "for createIndex, requested in ipairs(args.creates or {}) do"
        )
        self.assertLess(edit_loop, create_loop)
        self.assertIn(
            "local targetIndex = #args.targets + createIndex",
            prepare,
        )
        for marker in (
            "local parent = resolveExactPath(requested.parent_path, false)",
            "local existing, matchCount = exactNamedChild(parent, requested.name)",
            "if existing ~= nil or matchCount ~= 0 then",
            '"multi_edit_create_revision_conflict"',
            "created_instance = nil",
            "create_count = createCount",
        ):
            self.assertIn(marker, prepare)

    def test_create_is_staged_unparented_then_cas_rechecked_and_read_back(self) -> None:
        apply_create = _section(
            self.source,
            "local function applyMultiEditCreate(",
            "local function compensateMultiEditCreate(",
        )
        construct = apply_create.index(
            "created = Instance.new(planTarget.class_name)"
        )
        stage_source = apply_create.index(
            "created.Source = planTarget.planned_source"
        )
        unparented = apply_create.index("or created.Parent ~= nil")
        document_fence = apply_create.index("assertExpectedDocument()")
        parent_resolve = apply_create.index(
            "resolveExactPath(planTarget.parent_path, false)",
            document_fence,
        )
        absence_recheck = apply_create.index(
            "exactNamedChild(parent, planTarget.name)",
            parent_resolve,
        )
        final_absence = apply_create.index(
            "local finalExisting, finalMatchCount",
            absence_recheck,
        )
        final_parent_rebind = apply_create.index(
            "resolveExactPath(planTarget.parent_path, false)",
            absence_recheck,
        )
        commit_edit_fence = apply_create.index(
            "requireEditMode()", final_absence
        )
        commit_document_fence = apply_create.index(
            "assertExpectedDocument()", commit_edit_fence
        )
        parent_write = apply_create.index("created.Parent = parent")
        pointer_capture = apply_create.index(
            "planTarget.created_instance = created"
        )
        readback = apply_create.index(
            "observeMultiEditCreate(planTarget)",
            pointer_capture,
        )
        self.assertTrue(
            construct
            < stage_source
            < unparented
            < document_fence
            < parent_resolve
            < absence_recheck
            < final_parent_rebind
            < final_absence
            < commit_edit_fence
            < commit_document_fence
            < parent_write
            < pointer_capture
            < readback
        )

    def test_only_private_compensation_can_destroy_and_it_is_exactly_fenced(
        self,
    ) -> None:
        self.assertEqual(2, self.source.count(":Destroy()"))
        observe = _section(
            self.source,
            "local function multiEditCreatedContentState(",
            "local function discardUnparentedMultiEditCreate(",
        )
        for marker in (
            "parent ~= planTarget.parent",
            "matchCount ~= 1",
            "planTarget.created_instance == nil",
            "child ~= planTarget.created_instance",
            "child.Parent ~= parent",
            "child.Name ~= planTarget.name",
            "child.ClassName ~= planTarget.class_name",
            "source ~= planTarget.planned_source",
            "digest ~= planTarget.planned_sha256",
        ):
            self.assertIn(marker, observe)
        for marker in (
            "created:GetChildren()",
            "created:GetAttributes()",
            "created:GetTags()",
            "MAX_TREE_CHILDREN_PER_INSTANCE",
            "MAX_INSPECT_ATTRIBUTES_RAW",
            "MAX_INSPECT_TAGS_RAW",
            'return changed and "present_unproven" or "empty"',
        ):
            self.assertIn(marker, observe)
        discard = _section(
            self.source,
            "local function discardUnparentedMultiEditCreate(",
            "local function applyMultiEditCreate(",
        )
        self.assertLess(
            discard.index("created.Parent ~= nil"),
            discard.index("requireEditMode()"),
        )
        self.assertLess(
            discard.index("requireEditMode()"),
            discard.index("created:Destroy()"),
        )
        compensate = _section(
            self.source,
            "local function compensateMultiEditCreate(",
            "local function preparedMultiEditTargetMatches(",
        )
        self.assertLess(
            compensate.index('beforeState ~= "created_exact"'),
            compensate.index("created:Destroy()"),
        )
        self.assertLess(
            compensate.index("beforeDigest ~= planTarget.planned_sha256"),
            compensate.index("created:Destroy()"),
        )
        self.assertLess(
            compensate.index("multiEditCreatedContentState(planTarget)"),
            compensate.index("created:Destroy()"),
        )
        final_observation = compensate.index(
            "local finalState, finalClass, finalDigest"
        )
        destroy_edit_fence = compensate.index(
            "requireEditMode()", final_observation
        )
        destroy_document_fence = compensate.index(
            "assertExpectedDocument()", destroy_edit_fence
        )
        final_identity = compensate.index(
            "exactNamedChild(planTarget.parent, planTarget.name)",
            destroy_document_fence,
        )
        final_parent_rebind = compensate.index(
            "resolveExactPath(planTarget.parent_path, false)",
            destroy_document_fence,
        )
        self.assertTrue(
            final_observation
            < destroy_edit_fence
            < destroy_document_fence
            < final_parent_rebind
            < final_identity
            < compensate.index("created:Destroy()")
        )
        self.assertLess(
            compensate.index('beforeState == "absent"'),
            compensate.index("created:Destroy()"),
        )
        self.assertIn(
            "planTarget.created_instance.Parent == nil",
            compensate,
        )
        self.assertIn('afterState == "absent"', compensate)
        lifecycle = _section(
            self.source,
            "local function observeMultiEditCreate(",
            "local function preparedMultiEditTargetMatches(",
        )
        self.assertEqual(2, lifecycle.count(":Destroy()"))
        self.assertNotIn(":Destroy()", self.source[
            self.source.index("local function preparedMultiEditTargetMatches(") :
        ])

    def test_every_source_cas_is_edit_and_document_fenced_at_commit(self) -> None:
        cas = _section(
            self.source,
            "local function multiEditCasReplace(",
            "local function multiEditCreatedContentState(",
        )
        dispatch_edit = cas.index("requireEditMode()")
        dispatch_document = cas.index(
            "assertExpectedDocument()", dispatch_edit
        )
        dispatch_identity = cas.index(
            "requireLuaSourceContainer(path)", dispatch_document
        )
        update = cas.index(
            "ScriptEditorService:UpdateSourceAsync",
            dispatch_identity,
        )
        expected_revision = cas.index(
            "if currentSha256 == expectedSha256 then", update
        )
        callback_edit = cas.index(
            "requireEditMode()", expected_revision
        )
        callback_document = cas.index(
            "assertExpectedDocument()", callback_edit
        )
        callback_identity = cas.index(
            "requireLuaSourceContainer(path)", callback_document
        )
        write_commit = cas.index(
            "return replacement", callback_identity
        )
        confirmed_read = cas.index("readEditorSource(target)", write_commit)
        confirmed_identity = cas.index(
            "requireLuaSourceContainer(path)", confirmed_read
        )
        self.assertTrue(
            dispatch_edit
            < dispatch_document
            < dispatch_identity
            < update
            < expected_revision
            < callback_edit
            < callback_document
            < callback_identity
            < write_commit
            < confirmed_read
            < confirmed_identity
        )
        self.assertIn("mutationFenceFailed = true", cas)
        self.assertIn(
            "mutationFenceFailed or confirmedSha256 ~= expectedSha256",
            cas,
        )
        self.assertEqual(
            3,
            len(
                re.findall(
                    r"multiEditCasReplace\(\s*resolved,\s*"
                    r"planTarget\.path,",
                    self.source,
                )
            ),
        )

    def test_source_readbacks_rebind_exact_path_identity(self) -> None:
        lifecycle = _section(
            self.source,
            "local function prepareMultiEditExistingTarget(",
            "local function readScript(",
        )
        for marker in (
            "if requireLuaSourceContainer(requested.path) ~= target then",
            "if requireLuaSourceContainer(planTarget.path) ~= resolved then",
            "if requireLuaSourceContainer(planTarget.path) ~= exact then",
            "resolveExactPath(planTarget.parent_path, false)",
            "reboundChild ~= child",
        ):
            self.assertIn(marker, lifecycle)

    def test_non_absent_create_failure_sets_recovery_plan_status(self) -> None:
        apply = _section(
            self.source,
            "local function applyMultiEdit(",
            "local function recoverMultiEdit(",
        )
        self.assertIn(
            'if mutated or state ~= "absent" then\n'
            '\t\t\t\t\toutcomes[index].status = "recovery_required"',
            apply,
        )
        self.assertIn(
            "local recoveryRequired =\n"
            '\t\toutcomes[failureIndex].status == "recovery_required"',
            apply,
        )
        self.assertIn(
            'plan.status = recoveryRequired\n'
            '\t\tand "recovery_required"\n'
            '\t\tor "rolled_back"',
            apply,
        )
        recompute = (
            "recoveryRequired = false\n"
            "\tfor _, targetOutcome in ipairs(outcomes) do\n"
            '\t\tif targetOutcome.status == "recovery_required" then'
        )
        self.assertIn(recompute, apply)
        self.assertLess(
            apply.index(recompute),
            apply.index(
                "plan.status = recoveryRequired",
                apply.index(recompute),
            ),
        )
        stage = _section(
            self.source,
            "local function applyMultiEditCreate(",
            "local function compensateMultiEditCreate(",
        )
        self.assertGreaterEqual(
            stage.count("discardUnparentedMultiEditCreate(created)"),
            4,
        )
        self.assertGreaterEqual(
            stage.count("observeMultiEditCreate(planTarget)"),
            5,
        )

    def test_receipts_bind_create_identity_and_v2_hash_contract(self) -> None:
        hashing = _section(
            self.source,
            "local function multiEditPrepareSha256(",
            "local function multiEditRangesOverlap(",
        )
        for marker in (
            '"studio-multi-edit-prepare-v2"',
            '"studio-multi-edit-mutation-v2"',
            "receipt.create_count",
            "appendCanonicalMultiEditValue(parts, target.kind)",
            "appendCanonicalMultiEditPath(parts, target.parent_path)",
            "receipt.evidence_mode",
            "receipt.prior_terminal_outcome",
            "receipt.prior_terminal_receipt_sha256",
            '"expected_absent"',
            '"prepared_absent"',
            '"observed_before_state"',
            '"observed_after_state"',
            '"observed_after_class_name"',
            '"observed_after_sha256"',
        ):
            self.assertIn(marker, hashing)
        lifecycle = _section(
            self.source,
            "local function prepareMultiEdit(",
            "local function readScript(",
        )
        self.assertGreaterEqual(lifecycle.count("v = 2"), 2)
        for marker in (
            "studio_id = peer.studio_id",
            "client_instance_id = CLIENT_INSTANCE_ID",
            "document_epoch = DOCUMENT_EPOCH",
            "generation = peer.generation",
            "transaction_id = args.transaction_id",
            "prepare_request_id = requestId",
        ):
            self.assertIn(marker, lifecycle)

    def test_safe_terminal_evidence_replays_without_new_reads_or_writes(
        self,
    ) -> None:
        receipts = _section(
            self.source,
            "\tif safeTerminal\n\t\tand (",
            "\treturn receipt\nend\n\nlocal function assertMultiEditMutationReceiptBound(",
        )
        for marker in (
            'outcome == "aborted_preflight"',
            'outcome == "rolled_back"',
            'phase == "recover" and outcome == "recovered"',
            "plan.safe_terminal_evidence = {",
            "outcome = outcome",
            "receipt_sha256 = receipt.receipt_sha256",
            "targets = copyMultiEditTargetOutcomes(targetOutcomes)",
        ):
            self.assertIn(marker, receipts)
        self.assertNotIn('outcome == "applied"', receipts)
        self.assertNotIn('outcome == "recovery_required"', receipts)

        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function newMultiEditCleanupTargetOutcome(",
        )
        replay_start = recovery.index(
            "local evidence = plan.safe_terminal_evidence"
        )
        replay_end = recovery.index("local outcomes = {}", replay_start)
        replay = recovery[replay_start:replay_end]
        for marker in (
            'planStatus == "aborted_preflight"',
            'planStatus == "rolled_back"',
            'planStatus == "recovered"',
            "copyMultiEditTargetOutcomes(evidence.targets)",
            'plan.status = "recovered"',
            '"recover",\n\t\t\t"recovered",\n\t\t\ttrue,',
            '"cached_safe_terminal"',
            "evidence.outcome",
            "evidence.receipt_sha256",
        ):
            self.assertIn(marker, replay)
        for forbidden in (
            "requireLuaSourceContainer(",
            "observeMultiEditCreate(",
            "multiEditCasReplace(",
            "compensateMultiEditCreate(",
            ":Destroy()",
        ):
            self.assertNotIn(forbidden, replay)

    def test_recovery_is_same_generation_compensation_only(self) -> None:
        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function newMultiEditCleanupTargetOutcome(",
        )
        for marker in (
            "or plan.document_epoch ~= DOCUMENT_EPOCH",
            "or plan.generation ~= peer.generation",
            'local recoverableStatus = planStatus == "prepared"',
            'or planStatus == "applying"',
            'or planStatus == "applied"',
            'or planStatus == "rolled_back"',
            'or planStatus == "aborted_preflight"',
            'or planStatus == "recovery_required"',
            'or planStatus == "recovered"',
            "or not recoverableStatus",
            "for index = #plan.targets, 1, -1 do",
            "compensateMultiEditCreate(planTarget)",
        ):
            self.assertIn(marker, recovery)
        self.assertNotIn("Instance.new(", recovery)

    def test_renderer_embeds_one_consistent_hardened_handler(self) -> None:
        indented = "\n".join(
            "\t" + line if line else ""
            for line in self.source.rstrip().splitlines()
        )
        self.assertIn(indented, self.rendered)
        self.assertEqual(
            1,
            self.rendered.count("local function prepareMultiEdit("),
        )
        self.assertEqual(
            1,
            self.rendered.count("local function applyMultiEdit("),
        )
        self.assertEqual(
            1,
            self.rendered.count("local function recoverMultiEdit("),
        )
        self.assertIn("local MAX_ARGS_BYTES = 351_000", self.rendered)
        self.assertIn(
            'request.operation == "studio_recover_multi_edit"',
            self.rendered,
        )
        self.assertIn("result.safe_terminal == true", self.rendered)
        self.assertIn("result.recovery_required == false", self.rendered)
        self.assertNotIn("active_studio", self.rendered)
        self.assertNotIn("default_studio", self.rendered)


if __name__ == "__main__":
    unittest.main()
