from __future__ import annotations

import copy
import hashlib
import math
import unittest

from studio_mcp_v2.errors import ValidationError
from studio_mcp_v2.multi_edit import (
    MAX_MULTI_EDIT_ARGUMENT_BYTES,
    MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES,
    MAX_MULTI_EDIT_EDITS,
    MAX_MULTI_EDIT_EDITS_PER_TARGET,
    MAX_MULTI_EDIT_LITERAL_BYTES,
    MAX_MULTI_EDIT_SOURCE_BYTES,
    MAX_MULTI_EDIT_TARGETS,
    MAX_PATH_SEGMENT_BYTES,
    MAX_PATH_SEGMENTS,
    MULTI_EDIT_ATOMICITY,
    MULTI_EDIT_ORDERING_VERSION,
    MULTI_EDIT_RECEIPT_CONTRACT,
    canonical_json_bytes,
    canonical_json_sha256,
    mutation_receipt_sha256,
    normalize_multi_edit_arguments,
    prepare_receipt_sha256,
    public_arguments_sha256,
    total_edit_count,
)


SHA_A = "1" * 64
SHA_B = "2" * 64
SHA_C = "3" * 64
SHA_D = "4" * 64


def _edit(
    old_string: str = "old",
    new_string: str = "new",
    **updates,
):
    result = {
        "old_string": old_string,
        "new_string": new_string,
    }
    result.update(updates)
    return result


def _target(
    name: str = "Script",
    *,
    edits=None,
    expected_sha256: str = SHA_A,
    path=None,
):
    return {
        "path": list(path if path is not None else ["Workspace", name]),
        "expected_sha256": expected_sha256,
        "edits": list(edits if edits is not None else [_edit()]),
    }


def _arguments(targets=None):
    return {
        "datamodel_type": "Edit",
        "targets": list(targets if targets is not None else [_target()]),
    }


def _prepare_receipt():
    return {
        "studio_id": "studio-alpha",
        "client_instance_id": "client-01",
        "document_epoch": "epoch-0001",
        "generation": 7,
        "request_id": "request-prepare-01",
        "transaction_id": "txn-01",
        "ordering_version": MULTI_EDIT_ORDERING_VERSION,
        "atomicity": MULTI_EDIT_ATOMICITY,
        "target_count": 2,
        "edit_count": 3,
        "create_count": 0,
        "aggregate_source_bytes": 30,
        "aggregate_planned_source_bytes": 35,
        "targets": [
            {
                "index": 0,
                "kind": "edit",
                "path": ["ServerScriptService", "Alpha"],
                "expected_sha256": SHA_A,
                "prepared_sha256": SHA_A,
                "planned_sha256": SHA_B,
                "source_length": 10,
                "planned_source_length": 12,
                "edit_count": 2,
                "replacement_count": 2,
                "status": "prepared",
            },
            {
                "index": 1,
                "kind": "edit",
                "path": ["ReplicatedStorage", "Beta"],
                "expected_sha256": SHA_C,
                "prepared_sha256": SHA_C,
                "planned_sha256": SHA_D,
                "source_length": 20,
                "planned_source_length": 23,
                "edit_count": 1,
                "replacement_count": 1,
                "status": "prepared",
            },
        ],
    }


def _mutation_receipt():
    prepare_hash = prepare_receipt_sha256(_prepare_receipt())
    return {
        "phase": "apply",
        "studio_id": "studio-alpha",
        "client_instance_id": "client-01",
        "document_epoch": "epoch-0001",
        "generation": 7,
        "request_id": "request-apply-01",
        "transaction_id": "txn-01",
        "prepare_request_id": "request-prepare-01",
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
        "cleanup_authorized": False,
        "cleanup_contract": "",
        "cleanup_authorization_sha256": "",
        "cleanup_expires_in_ms": 0,
        "target_count": 2,
        "edit_count": 3,
        "create_count": 0,
        "targets": [
            {
                "index": 0,
                "kind": "edit",
                "path": ["ServerScriptService", "Alpha"],
                "expected_sha256": SHA_A,
                "prepared_sha256": SHA_A,
                "planned_sha256": SHA_B,
                "observed_before_sha256": SHA_A,
                "observed_after_sha256": SHA_B,
                "source_length": 10,
                "planned_source_length": 12,
                "edit_count": 2,
                "replacement_count": 2,
                "status": "applied",
            },
            {
                "index": 1,
                "kind": "edit",
                "path": ["ReplicatedStorage", "Beta"],
                "expected_sha256": SHA_C,
                "prepared_sha256": SHA_C,
                "planned_sha256": SHA_D,
                "observed_before_sha256": SHA_C,
                "observed_after_sha256": SHA_D,
                "source_length": 20,
                "planned_source_length": 23,
                "edit_count": 1,
                "replacement_count": 1,
                "status": "applied",
            },
        ],
    }


def _drift(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        if len(value) == 64 and set(value) <= set("0123456789abcdef"):
            replacement = "0" if value[0] != "0" else "1"
            return replacement + value[1:]
        return value + "-drift"
    if isinstance(value, list):
        return value + ["Drift"]
    raise AssertionError(f"no deterministic drift for {type(value)!r}")


class Phase2MultiEditModelTests(unittest.TestCase):
    def assert_invalid(self, payload) -> None:
        with self.assertRaises(ValidationError):
            normalize_multi_edit_arguments(payload)

    def test_exact_closed_schema_defaults_and_input_ordering(self) -> None:
        arguments = _arguments(
            [
                _target(
                    "First",
                    edits=[
                        _edit("one", "ONE"),
                        _edit("two", "TWO", replace_all=True),
                    ],
                    expected_sha256=SHA_A,
                ),
                _target(
                    "Second",
                    edits=[_edit("é", "E", start_byte=5, end_byte=7)],
                    expected_sha256=SHA_B,
                ),
            ]
        )
        original = copy.deepcopy(arguments)

        normalized = normalize_multi_edit_arguments(arguments)

        self.assertEqual(arguments, original)
        self.assertEqual(
            normalized,
            {
                "datamodel_type": "Edit",
                "targets": [
                    {
                        "path": ["Workspace", "First"],
                        "expected_sha256": SHA_A,
                        "edits": [
                            {
                                "old_string": "one",
                                "new_string": "ONE",
                                "replace_all": False,
                            },
                            {
                                "old_string": "two",
                                "new_string": "TWO",
                                "replace_all": True,
                            },
                        ],
                    },
                    {
                        "path": ["Workspace", "Second"],
                        "expected_sha256": SHA_B,
                        "edits": [
                            {
                                "old_string": "é",
                                "new_string": "E",
                                "replace_all": False,
                                "start_byte": 5,
                                "end_byte": 7,
                            }
                        ],
                    },
                ],
            },
        )
        self.assertEqual(
            MULTI_EDIT_ORDERING_VERSION,
            "edit-target-input-then-create-input-v2",
        )
        self.assertEqual(total_edit_count(normalized["targets"]), 3)

        top_extra = _arguments()
        top_extra["extra"] = True
        missing_top = {"targets": [_target()]}
        target_extra = _arguments()
        target_extra["targets"][0]["extra"] = True
        missing_target = _arguments()
        del missing_target["targets"][0]["expected_sha256"]
        edit_extra = _arguments()
        edit_extra["targets"][0]["edits"][0]["extra"] = True
        missing_edit = _arguments()
        del missing_edit["targets"][0]["edits"][0]["new_string"]
        invalid_cases = [
            None,
            [],
            top_extra,
            missing_top,
            {"datamodel_type": "Play", "targets": [_target()]},
            {"datamodel_type": "Edit", "targets": tuple([_target()])},
            target_extra,
            missing_target,
            edit_extra,
            missing_edit,
        ]
        for index, payload in enumerate(invalid_cases):
            with self.subTest(index=index):
                self.assert_invalid(payload)

    def test_utf8_is_measured_in_bytes_and_lone_surrogates_fail_closed(self) -> None:
        valid = _arguments(
            [
                _target(
                    path=["😀" * 25],
                    edits=[_edit("😀", "é")],
                )
            ]
        )
        self.assertEqual(
            normalize_multi_edit_arguments(valid)["targets"][0]["path"],
            ["😀" * 25],
        )

        too_wide_path = _arguments(
            [_target(path=["😀" * 26])]
        )
        self.assert_invalid(too_wide_path)

        for location in ("path", "old_string", "new_string"):
            payload = _arguments()
            if location == "path":
                payload["targets"][0]["path"] = ["\ud800"]
            else:
                payload["targets"][0]["edits"][0][location] = "\ud800"
            with self.subTest(location=location):
                self.assert_invalid(payload)

        control_path = _arguments([_target(path=["Line\nBreak"])])
        self.assert_invalid(control_path)
        self.assert_invalid(
            _arguments([_target(edits=[_edit("", "not-empty")])])
        )
        self.assertEqual(
            normalize_multi_edit_arguments(
                _arguments([_target(edits=[_edit("delete", "")])])
            )["targets"][0]["edits"][0]["new_string"],
            "",
        )

    def test_duplicate_exact_target_paths_are_rejected(self) -> None:
        duplicate = _arguments(
            [
                _target("Same", expected_sha256=SHA_A),
                _target("Same", expected_sha256=SHA_B),
            ]
        )
        self.assert_invalid(duplicate)

        case_distinct = _arguments(
            [
                _target("Same", expected_sha256=SHA_A),
                _target("same", expected_sha256=SHA_B),
            ]
        )
        self.assertEqual(
            len(normalize_multi_edit_arguments(case_distinct)["targets"]),
            2,
        )

    def test_revision_requires_exact_lowercase_sha256(self) -> None:
        valid = "0123456789abcdef" * 4
        normalized = normalize_multi_edit_arguments(
            _arguments([_target(expected_sha256=valid)])
        )
        self.assertEqual(
            normalized["targets"][0]["expected_sha256"],
            valid,
        )

        invalid_revisions = [
            "0" * 63,
            "0" * 65,
            "A" * 64,
            "g" * 64,
            " " + ("0" * 63),
            ("0" * 63) + "\n",
            b"0" * 64,
            None,
        ]
        for revision in invalid_revisions:
            payload = _arguments(
                [_target(expected_sha256=revision)]
            )
            with self.subTest(revision=repr(revision)):
                self.assert_invalid(payload)

    def test_target_edit_and_path_bounds(self) -> None:
        maximum_targets = _arguments(
            [
                _target(f"Script-{index}", expected_sha256=f"{index:x}" * 64)
                for index in range(MAX_MULTI_EDIT_TARGETS)
            ]
        )
        self.assertEqual(
            len(normalize_multi_edit_arguments(maximum_targets)["targets"]),
            MAX_MULTI_EDIT_TARGETS,
        )
        self.assert_invalid({"datamodel_type": "Edit", "targets": []})
        self.assert_invalid(
            _arguments(
                [
                    _target(
                        f"Script-{index}",
                        expected_sha256=f"{index % 16:x}" * 64,
                    )
                    for index in range(MAX_MULTI_EDIT_TARGETS + 1)
                ]
            )
        )

        maximum_per_target = _arguments(
            [
                _target(
                    edits=[
                        _edit(f"old-{index}", f"new-{index}")
                        for index in range(MAX_MULTI_EDIT_EDITS_PER_TARGET)
                    ]
                )
            ]
        )
        self.assertEqual(
            len(
                normalize_multi_edit_arguments(maximum_per_target)["targets"][
                    0
                ]["edits"]
            ),
            MAX_MULTI_EDIT_EDITS_PER_TARGET,
        )
        self.assert_invalid(_arguments([_target(edits=[])]))
        self.assert_invalid(
            _arguments(
                [
                    _target(
                        edits=[
                            _edit(f"old-{index}", f"new-{index}")
                            for index in range(
                                MAX_MULTI_EDIT_EDITS_PER_TARGET + 1
                            )
                        ]
                    )
                ]
            )
        )

        exactly_total = _arguments(
            [
                _target(
                    f"Total-{target_index}",
                    edits=[
                        _edit("x", "y")
                        for _ in range(MAX_MULTI_EDIT_EDITS_PER_TARGET)
                    ],
                )
                for target_index in range(
                    MAX_MULTI_EDIT_EDITS // MAX_MULTI_EDIT_EDITS_PER_TARGET
                )
            ]
        )
        normalized = normalize_multi_edit_arguments(exactly_total)
        self.assertEqual(total_edit_count(normalized["targets"]), MAX_MULTI_EDIT_EDITS)
        over_total = copy.deepcopy(exactly_total)
        over_total["targets"].append(
            _target("One-Too-Many", edits=[_edit("x", "y")])
        )
        self.assert_invalid(over_total)

        maximum_path = ["p" * MAX_PATH_SEGMENT_BYTES] * MAX_PATH_SEGMENTS
        self.assertEqual(
            len(
                normalize_multi_edit_arguments(
                    _arguments([_target(path=maximum_path)])
                )["targets"][0]["path"]
            ),
            MAX_PATH_SEGMENTS,
        )
        for path in (
            [],
            ["p"] * (MAX_PATH_SEGMENTS + 1),
            ["p" * (MAX_PATH_SEGMENT_BYTES + 1)],
            [""],
        ):
            with self.subTest(path_length=len(path)):
                self.assert_invalid(_arguments([_target(path=path)]))

    def test_literal_bounds_accept_exact_limit_and_reject_overflow(self) -> None:
        exact_old = "😀" * (MAX_MULTI_EDIT_LITERAL_BYTES // 4)
        exact = _arguments([_target(edits=[_edit(exact_old, "")])])
        normalized = normalize_multi_edit_arguments(exact)
        self.assertEqual(
            len(
                normalized["targets"][0]["edits"][0]["old_string"].encode(
                    "utf-8"
                )
            ),
            MAX_MULTI_EDIT_LITERAL_BYTES,
        )

        aggregate_over = _arguments(
            [
                _target(
                    edits=[
                        _edit("a" * MAX_MULTI_EDIT_LITERAL_BYTES, "b")
                    ]
                )
            ]
        )
        self.assert_invalid(aggregate_over)
        self.assert_invalid(
            _arguments(
                [
                    _target(
                        edits=[
                            _edit(
                                "a" * (MAX_MULTI_EDIT_LITERAL_BYTES + 1),
                                "",
                            )
                        ]
                    )
                ]
            )
        )
        self.assert_invalid(
            _arguments(
                [
                    _target(
                        edits=[
                            _edit(
                                "x",
                                "b" * (MAX_MULTI_EDIT_LITERAL_BYTES + 1),
                            )
                        ]
                    )
                ]
            )
        )

    @staticmethod
    def _encoded_boundary_arguments(
        escaped_filler_length: int,
        plain_filler_length: int,
    ):
        targets = []
        for index in range(MAX_MULTI_EDIT_TARGETS):
            old_string = (
                ('"' * escaped_filler_length)
                + ("a" * plain_filler_length)
                if index == 0
                else "a"
            )
            targets.append(
                _target(
                    edits=[_edit(old_string, "")],
                    expected_sha256=f"{index:x}" * 64,
                    path=["Workspace", f"Script-{index:02d}"],
                )
            )
        return _arguments(targets)

    def test_encoded_argument_bound_accepts_exact_limit(self) -> None:
        one_byte = self._encoded_boundary_arguments(0, 1)
        normalized_one_byte = normalize_multi_edit_arguments(one_byte)
        base_size = len(canonical_json_bytes(normalized_one_byte))
        encoded_filler_size = (
            1 + MAX_MULTI_EDIT_ARGUMENT_BYTES - base_size
        )
        escaped_filler_length = encoded_filler_size // 2
        plain_filler_length = encoded_filler_size % 2
        self.assertGreater(escaped_filler_length, 1)
        self.assertLessEqual(
            escaped_filler_length
            + plain_filler_length
            + MAX_MULTI_EDIT_TARGETS
            - 1,
            MAX_MULTI_EDIT_LITERAL_BYTES,
        )

        exact = self._encoded_boundary_arguments(
            escaped_filler_length,
            plain_filler_length,
        )
        normalized_exact = normalize_multi_edit_arguments(exact)
        self.assertEqual(
            len(canonical_json_bytes(normalized_exact)),
            MAX_MULTI_EDIT_ARGUMENT_BYTES,
        )
        self.assert_invalid(
            self._encoded_boundary_arguments(
                escaped_filler_length,
                plain_filler_length + 1,
            )
        )

    def test_aggregate_path_byte_bound_is_exact(self) -> None:
        first_path = ["a" * MAX_PATH_SEGMENT_BYTES] * MAX_PATH_SEGMENTS
        remaining = (
            MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES
            - MAX_PATH_SEGMENT_BYTES * MAX_PATH_SEGMENTS
        )
        second_path = (
            ["b" * MAX_PATH_SEGMENT_BYTES]
            * (remaining // MAX_PATH_SEGMENT_BYTES)
        )
        if remaining % MAX_PATH_SEGMENT_BYTES:
            second_path.append(
                "b" * (remaining % MAX_PATH_SEGMENT_BYTES)
            )
        exact = _arguments(
            [
                _target(
                    path=first_path,
                    expected_sha256=SHA_A,
                ),
                _target(
                    path=second_path,
                    expected_sha256=SHA_B,
                ),
            ]
        )
        normalized = normalize_multi_edit_arguments(exact)
        self.assertEqual(
            sum(
                len(segment.encode("utf-8"))
                for target in normalized["targets"]
                for segment in target["path"]
            ),
            MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES,
        )
        over = copy.deepcopy(exact)
        over["targets"][1]["path"][-1] += "b"
        self.assert_invalid(over)

    def test_optional_byte_offsets_are_paired_and_utf8_exact(self) -> None:
        valid = _arguments(
            [
                _target(
                    edits=[
                        _edit(
                            "é",
                            "E",
                            start_byte=5,
                            end_byte=7,
                        )
                    ]
                )
            ]
        )
        normalized = normalize_multi_edit_arguments(valid)
        self.assertEqual(
            (
                normalized["targets"][0]["edits"][0]["start_byte"],
                normalized["targets"][0]["edits"][0]["end_byte"],
            ),
            (5, 7),
        )

        invalid_edits = [
            _edit("a", "b", start_byte=0),
            _edit("a", "b", end_byte=1),
            _edit("a", "b", start_byte=0, end_byte=1, replace_all=True),
            _edit("a", "b", start_byte=True, end_byte=1),
            _edit("a", "b", start_byte=0, end_byte=False),
            _edit("a", "b", start_byte=-1, end_byte=0),
            _edit("a", "b", start_byte=1, end_byte=1),
            _edit("a", "b", start_byte=2, end_byte=1),
            _edit("é", "E", start_byte=5, end_byte=6),
            _edit(
                "a",
                "b",
                start_byte=MAX_MULTI_EDIT_SOURCE_BYTES,
                end_byte=MAX_MULTI_EDIT_SOURCE_BYTES + 1,
            ),
        ]
        for index, edit in enumerate(invalid_edits):
            with self.subTest(index=index):
                self.assert_invalid(_arguments([_target(edits=[edit])]))

    def test_canonical_json_is_stable_utf8_and_fail_closed(self) -> None:
        first = {"z": "é", "a": [3, True, None]}
        second = {"a": [3, True, None], "z": "é"}
        expected = b'{"a":[3,true,null],"z":"\xc3\xa9"}'
        self.assertEqual(canonical_json_bytes(first), expected)
        self.assertEqual(canonical_json_bytes(second), expected)
        expected_hash = hashlib.sha256(expected).hexdigest()
        self.assertEqual(canonical_json_sha256(first), expected_hash)
        self.assertEqual(canonical_json_sha256(second), expected_hash)
        self.assertEqual(public_arguments_sha256(first), expected_hash)

        for value in (
            {"value": math.nan},
            {"value": math.inf},
            {"value": object()},
            {"value": {"not", "json"}},
            {"value": "\ud800"},
        ):
            with self.subTest(value_type=type(value["value"]).__name__):
                with self.assertRaises(ValidationError):
                    canonical_json_bytes(value)

    def test_prepare_receipt_golden_hash_and_every_field_drifts(self) -> None:
        receipt = _prepare_receipt()
        expected = (
            "2f4362b3fa5e2d4b27e623d965cc64e3755a529852e3c7b64d1ce731318e28c2"
        )
        self.assertEqual(prepare_receipt_sha256(receipt), expected)
        self.assertEqual(
            prepare_receipt_sha256(copy.deepcopy(receipt)),
            expected,
        )

        top_fields = (
            "studio_id",
            "client_instance_id",
            "document_epoch",
            "generation",
            "request_id",
            "transaction_id",
            "ordering_version",
            "atomicity",
            "target_count",
            "edit_count",
            "create_count",
            "aggregate_source_bytes",
            "aggregate_planned_source_bytes",
        )
        target_fields = (
            "index",
            "kind",
            "path",
            "expected_sha256",
            "prepared_sha256",
            "planned_sha256",
            "source_length",
            "planned_source_length",
            "edit_count",
            "replacement_count",
            "status",
        )
        for field in top_fields:
            drifted = copy.deepcopy(receipt)
            drifted[field] = _drift(drifted[field])
            with self.subTest(scope="top", field=field):
                self.assertNotEqual(
                    prepare_receipt_sha256(drifted),
                    expected,
                )
        for target_index in range(len(receipt["targets"])):
            for field in target_fields:
                drifted = copy.deepcopy(receipt)
                drifted["targets"][target_index][field] = _drift(
                    drifted["targets"][target_index][field]
                )
                with self.subTest(
                    scope=f"target-{target_index}",
                    field=field,
                ):
                    self.assertNotEqual(
                        prepare_receipt_sha256(drifted),
                        expected,
                    )

    def test_mutation_receipt_golden_hash_and_every_field_drifts(self) -> None:
        receipt = _mutation_receipt()
        expected = (
            "76c128f8c42294616d7c87d1f0123543341e8012ae1fac3fa5678fa480a55091"
        )
        self.assertEqual(mutation_receipt_sha256(receipt), expected)
        self.assertEqual(
            mutation_receipt_sha256(copy.deepcopy(receipt)),
            expected,
        )

        top_fields = (
            "phase",
            "studio_id",
            "client_instance_id",
            "document_epoch",
            "generation",
            "request_id",
            "transaction_id",
            "prepare_request_id",
            "prepare_sha256",
            "ordering_version",
            "atomicity",
            "receipt_contract",
            "evidence_mode",
            "prior_terminal_outcome",
            "prior_terminal_receipt_sha256",
            "outcome",
            "safe_terminal",
            "recovery_required",
            "cleanup_authorized",
            "cleanup_contract",
            "cleanup_authorization_sha256",
            "cleanup_expires_in_ms",
            "target_count",
            "edit_count",
            "create_count",
        )
        target_fields = (
            "index",
            "kind",
            "path",
            "expected_sha256",
            "prepared_sha256",
            "planned_sha256",
            "observed_before_sha256",
            "observed_after_sha256",
            "source_length",
            "planned_source_length",
            "edit_count",
            "replacement_count",
            "status",
        )
        for field in top_fields:
            drifted = copy.deepcopy(receipt)
            drifted[field] = _drift(drifted[field])
            with self.subTest(scope="top", field=field):
                self.assertNotEqual(
                    mutation_receipt_sha256(drifted),
                    expected,
                )
        for target_index in range(len(receipt["targets"])):
            for field in target_fields:
                drifted = copy.deepcopy(receipt)
                drifted["targets"][target_index][field] = _drift(
                    drifted["targets"][target_index][field]
                )
                with self.subTest(
                    scope=f"target-{target_index}",
                    field=field,
                ):
                    self.assertNotEqual(
                        mutation_receipt_sha256(drifted),
                        expected,
                    )

    def test_receipt_hashes_reject_noncanonical_values(self) -> None:
        prepare = _prepare_receipt()
        prepare["generation"] = 7.0
        with self.assertRaises(ValidationError):
            prepare_receipt_sha256(prepare)

        mutation = _mutation_receipt()
        mutation["targets"][0]["path"][0] = None
        with self.assertRaises(ValidationError):
            mutation_receipt_sha256(mutation)


if __name__ == "__main__":
    unittest.main()
