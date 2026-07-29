from __future__ import annotations

import dataclasses
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
            "local MAX_MULTI_EDIT_REPLACEMENT_SPANS = 1_024",
            "local MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES = 8_192",
            "local MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES = 1_048_576",
            "local MAX_MULTI_EDIT_RECEIPT_BYTES = 100_000",
        ):
            self.assertIn(marker, self.source)

        validation = _section(
            self.source,
            "local function validateMultiEditTargets(",
            "local function validateDurableArgs(",
        )
        self.assertEqual(
            2,
            validation.count(
                "aggregatePathBytes > MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES"
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
            "aggregateReplacementCount > MAX_MULTI_EDIT_REPLACEMENT_SPANS",
            prepare,
        )
        self.assertIn(
            "aggregateSourceBytes > MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES",
            prepare,
        )
        self.assertIn(
            "aggregatePlannedSourceBytes\n"
            "\t\t\t\t> MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES",
            prepare,
        )

    def test_receipt_bounds_are_proved_before_mutation(self) -> None:
        self.assertEqual(
            2,
            self.source.count(
                "#encodedReceipt > MAX_MULTI_EDIT_RECEIPT_BYTES"
            ),
        )
        prepare = _section(
            self.source,
            "local function prepareMultiEdit(",
            "local function multiEditCasReplace(",
        )
        self.assertLess(
            prepare.index("#encodedReceipt > MAX_MULTI_EDIT_RECEIPT_BYTES"),
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
            "local function readScript(",
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
            "\t\t\tsource,\n"
            "\t\t\trequested.edits,\n"
            "\t\t\tdeadline",
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
        strict_apply = (
            "multiEditCasReplace(\n"
            "\t\t\tresolved,\n"
            "\t\t\tplanTarget.prepared_sha256,\n"
            "\t\t\tplanTarget.planned_source,\n"
            "\t\t\tfalse\n"
            "\t\t)"
        )
        idempotent_rollback = (
            "multiEditCasReplace(\n"
            "\t\t\t\tresolved,\n"
            "\t\t\t\tplanTarget.planned_sha256,\n"
            "\t\t\t\tplanTarget.original_source,\n"
            "\t\t\t\ttrue\n"
            "\t\t\t)"
        )
        self.assertEqual(1, apply.count(strict_apply))
        self.assertEqual(1, apply.count(idempotent_rollback))

        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function readScript(",
        )
        self.assertEqual(
            1,
            recovery.count(
                "multiEditCasReplace(\n"
                "\t\t\t\t\tresolved,\n"
                "\t\t\t\t\tplanTarget.planned_sha256,\n"
                "\t\t\t\t\tplanTarget.original_source,\n"
                "\t\t\t\t\ttrue\n"
                "\t\t\t\t)"
            ),
        )

    def test_recovery_deadline_fails_closed_without_new_mutation(self) -> None:
        recovery = _section(
            self.source,
            "local function recoverMultiEdit(",
            "local function readScript(",
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
            "local function readScript(",
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
        self.assertEqual(3, dispatch.count("request.deadline_ms"))
        for function_name in (
            "prepareMultiEdit",
            "applyMultiEdit",
            "recoverMultiEdit",
        ):
            call = dispatch[
                dispatch.index(function_name + "(") :
                dispatch.index(")", dispatch.index(function_name + "(")) + 1
            ]
            self.assertIn("request.deadline_ms", call)

    def test_renderer_embeds_one_consistent_hardened_handler(self) -> None:
        self.assertIn(self.source.rstrip(), self.rendered)
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
