from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from studio_mcp_v2.catalog import (
    JOB_RECEIPT_OUTPUT_SCHEMA,
    JOB_TOOLS,
    ToolCatalog,
)
from studio_mcp_v2.session import (
    _DURABLE_STATE_KEYS,
    _DURABLE_STATE_RAW_PREDICATES,
    _DURABLE_TREE_ITEM_KEYS,
    _DURABLE_TREE_RESULT_KEYS,
)


ROOT = Path(__file__).resolve().parent.parent
DURABLE_CATALOG = ROOT / "config" / "durable-tool-catalog.json"
COMPATIBILITY_MANIFEST = (
    ROOT / "config" / "upstream-compatibility-map.json"
)

JOB_OPTIONAL_FIELDS = frozenset({"result", "error"})
JOB_RESOLUTION_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "kind",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "request_id",
        "transaction_id",
        "operation",
        "phase",
        "source",
        "resolver_job_id",
        "success",
        "safe_terminal",
        "recovery_required",
        "outcome",
        "receipt_sha256",
        "result_sha256",
        "result_bytes",
        "result",
    }
)
JOB_PHASE_FIELDS = frozenset(
    {
        "request_id",
        "generation",
        "phase",
        "success",
        "safe_terminal",
        "recovery_required",
        "outcome",
        "result_sha256",
        "result_bytes",
    }
)


def canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Phase2OutputSchemaParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.durable = json.loads(
            DURABLE_CATALOG.read_text(encoding="utf-8")
        )
        cls.manifest = json.loads(
            COMPATIBILITY_MANIFEST.read_text(encoding="utf-8")
        )
        cls.tools = {
            tool["name"]: tool for tool in cls.durable["tools"]
        }

    def test_state_output_schema_is_attached_to_state_and_closed(self) -> None:
        state = self.tools["studio_get_state"]["outputSchema"]
        tree = self.tools["studio_list_tree"]["outputSchema"]

        self.assertEqual(
            set(_DURABLE_STATE_KEYS), set(state["properties"])
        )
        self.assertEqual(
            set(_DURABLE_STATE_KEYS), set(state["required"])
        )
        self.assertIs(state["additionalProperties"], False)
        self.assertEqual(
            "studio_controller",
            state["properties"]["source"]["const"],
        )
        self.assertNotIn("operation", state["properties"])
        self.assertEqual(
            "studio_list_tree",
            tree["properties"]["operation"]["const"],
        )

        raw = state["$defs"]["rawModePredicates"]
        self.assertEqual(
            set(_DURABLE_STATE_RAW_PREDICATES),
            set(raw["properties"]),
        )
        self.assertEqual(
            set(_DURABLE_STATE_RAW_PREDICATES),
            set(raw["required"]),
        )
        self.assertIs(raw["additionalProperties"], False)

        play_refs = {
            item["$ref"]
            for item in state["properties"]["play"]["oneOf"]
        }
        self.assertEqual(
            {
                "#/$defs/controllerPlay",
                "#/$defs/transitionStarting",
                "#/$defs/transitionPlay",
                "#/$defs/transitionStopping",
                "#/$defs/transitionSettling",
                "#/$defs/transitionRecoveryRequired",
            },
            play_refs,
        )
        branches = state["allOf"][0]["oneOf"]
        self.assertEqual(8, len(branches))
        branch_pairs = {
            (
                branch["properties"]["mode"]["const"],
                branch["properties"]["mode_source"]["const"],
            )
            for branch in branches
        }
        self.assertEqual(
            {
                ("edit", "controller_predicates"),
                ("play", "controller_predicates"),
                ("unknown", "controller_predicates"),
                ("starting", "play_transition"),
                ("play", "play_transition"),
                ("stopping", "play_transition"),
                ("settling", "play_transition"),
                ("unknown", "play_transition"),
            },
            branch_pairs,
        )

    def test_tree_output_schema_matches_host_key_and_bound_contract(self) -> None:
        tree = self.tools["studio_list_tree"]["outputSchema"]
        self.assertEqual(
            set(_DURABLE_TREE_RESULT_KEYS), set(tree["properties"])
        )
        self.assertEqual(
            set(_DURABLE_TREE_RESULT_KEYS), set(tree["required"])
        )
        self.assertIs(tree["additionalProperties"], False)
        item = tree["$defs"]["item"]
        self.assertEqual(
            set(_DURABLE_TREE_ITEM_KEYS), set(item["properties"])
        )
        self.assertEqual(
            set(_DURABLE_TREE_ITEM_KEYS), set(item["required"])
        )
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(
            500, tree["properties"]["items"]["maxItems"]
        )
        self.assertEqual(
            5000, tree["properties"]["scanned"]["maximum"]
        )
        self.assertEqual(
            600000,
            tree["properties"]["output_limit_bytes"]["const"],
        )
        truncated = tree["allOf"][0]
        self.assertEqual(
            False,
            truncated["if"]["properties"]["truncated"]["const"],
        )
        self.assertEqual(
            "",
            truncated["then"]["properties"][
                "continuation_cursor"
            ]["const"],
        )
        self.assertEqual(
            "complete",
            truncated["then"]["properties"][
                "truncation_reason"
            ]["const"],
        )

    def test_state_and_tree_output_hashes_are_exactly_pinned(self) -> None:
        expected = self.manifest[
            "durable_handler_output_schema_sha256"
        ]
        self.assertEqual(
            "88c3df8639116e63bdb0f6e01b6fbe218fbf98ae77ef90464a332cc549d64f6b",
            canonical_sha256(
                self.tools["studio_get_state"]["outputSchema"]
            ),
        )
        self.assertEqual(
            "043043ebf551bd9a07822f60212acbb3664735fd3eb530d9c17c7f3701044688",
            canonical_sha256(
                self.tools["studio_list_tree"]["outputSchema"]
            ),
        )
        for name in ("studio_get_state", "studio_list_tree"):
            self.assertEqual(
                canonical_sha256(self.tools[name]["outputSchema"]),
                expected[name],
            )

    def test_public_target_injection_does_not_mutate_output_schemas(self) -> None:
        catalog = ToolCatalog(self.durable["tools"])
        exposed = {
            tool["name"]: tool
            for tool in catalog.tools_for_mcp()
        }
        for remote in ("studio_get_state", "studio_list_tree"):
            with self.subTest(remote=remote):
                definition = catalog.get(remote + "_v2")
                self.assertEqual(
                    self.tools[remote]["outputSchema"],
                    exposed[remote + "_v2"]["outputSchema"],
                )
                self.assertEqual(
                    self.tools[remote]["inputSchema"],
                    definition.input_schema,
                )
                self.assertEqual(
                    self.tools[remote]["outputSchema"],
                    definition.output_schema,
                )
                self.assertEqual(
                    canonical_sha256(definition.input_schema),
                    definition.input_schema_sha256,
                )
                self.assertEqual(
                    canonical_sha256(definition.output_schema),
                    definition.output_schema_sha256,
                )
                self.assertNotIn(
                    "studio_id",
                    self.tools[remote]["inputSchema"]["properties"],
                )
                self.assertIn(
                    "studio_id",
                    exposed[remote + "_v2"]["inputSchema"][
                        "properties"
                    ],
                )

        state_definition = catalog.get("studio_get_state_v2")
        input_copy = state_definition.input_schema
        input_copy["properties"]["forged"] = {"type": "string"}
        self.assertNotIn(
            "forged", state_definition.input_schema["properties"]
        )
        output_copy = state_definition.output_schema
        output_copy["properties"]["forged"] = {"type": "string"}
        self.assertNotIn(
            "forged", state_definition.output_schema["properties"]
        )

    def test_all_job_tools_share_one_closed_receipt_schema(self) -> None:
        self.assertEqual(
            {
                "start_studio_job_v2",
                "get_studio_job_v2",
                "cancel_studio_job_v2",
            },
            {tool["name"] for tool in JOB_TOOLS},
        )
        for tool in JOB_TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(
                    JOB_RECEIPT_OUTPUT_SCHEMA,
                    tool["outputSchema"],
                )
                self.assertIsNot(
                    JOB_RECEIPT_OUTPUT_SCHEMA,
                    tool["outputSchema"],
                )

        schema = JOB_RECEIPT_OUTPUT_SCHEMA
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            set(schema["required"]) | set(JOB_OPTIONAL_FIELDS),
            set(schema["properties"]),
        )
        self.assertIn("resolution_receipts", schema["required"])
        self.assertEqual(
            4, schema["properties"]["resolution_receipts"]["maxItems"]
        )
        self.assertIs(
            schema["properties"]["resolution_receipts"]["uniqueItems"],
            True,
        )
        self.assertEqual(
            "a865132ec5083bee64b82a0aa9cfc026199eab600bcaafcc94805ddf55e356fd",
            canonical_sha256(schema),
        )

    def test_job_phase_and_resolution_receipts_are_exactly_closed(self) -> None:
        phase = JOB_RECEIPT_OUTPUT_SCHEMA["$defs"]["phaseReceipt"]
        self.assertEqual(set(JOB_PHASE_FIELDS), set(phase["properties"]))
        self.assertEqual(set(JOB_PHASE_FIELDS), set(phase["required"]))
        self.assertIs(phase["additionalProperties"], False)

        resolution = JOB_RECEIPT_OUTPUT_SCHEMA["$defs"][
            "resolutionReceipt"
        ]
        self.assertEqual(
            set(JOB_RESOLUTION_FIELDS), set(resolution["properties"])
        )
        self.assertEqual(
            set(JOB_RESOLUTION_FIELDS), set(resolution["required"])
        )
        self.assertIs(resolution["additionalProperties"], False)
        self.assertEqual(
            "studio-mcp-v2-job-resolution",
            resolution["properties"]["format"]["const"],
        )
        self.assertEqual(
            "exact_multi_edit_recovery",
            resolution["properties"]["kind"]["const"],
        )
        self.assertEqual(
            "recovered",
            resolution["properties"]["outcome"]["const"],
        )
        self.assertEqual(
            100000,
            resolution["properties"]["result_bytes"]["maximum"],
        )
        self.assertEqual(
            "#/$defs/recoveryResult",
            resolution["properties"]["result"]["$ref"],
        )
        recovery_result = JOB_RECEIPT_OUTPUT_SCHEMA["$defs"][
            "recoveryResult"
        ]
        self.assertIs(recovery_result["additionalProperties"], False)
        self.assertEqual(
            set(recovery_result["properties"]),
            set(recovery_result["required"]),
        )
        self.assertEqual(
            "recovered",
            recovery_result["properties"]["outcome"]["const"],
        )
        self.assertIs(
            recovery_result["properties"]["safe_terminal"]["const"],
            True,
        )
        self.assertIs(
            recovery_result["properties"]["recovery_required"]["const"],
            False,
        )
        recovery_target = JOB_RECEIPT_OUTPUT_SCHEMA["$defs"][
            "recoveryTargetReceipt"
        ]
        self.assertEqual(
            {
                "#/$defs/recoveryEditTargetReceipt",
                "#/$defs/recoveryCreateTargetReceipt",
            },
            {branch["$ref"] for branch in recovery_target["oneOf"]},
        )
        for name, kind, statuses in (
            (
                "recoveryEditTargetReceipt",
                "edit",
                {"rolled_back", "not_applied"},
            ),
            (
                "recoveryCreateTargetReceipt",
                "create",
                {"rolled_back", "not_created"},
            ),
        ):
            with self.subTest(recovery_target=name):
                target = JOB_RECEIPT_OUTPUT_SCHEMA["$defs"][name]
                self.assertIs(target["additionalProperties"], False)
                self.assertEqual(
                    set(target["properties"]),
                    set(target["required"]),
                )
                self.assertEqual(
                    kind, target["properties"]["kind"]["const"]
                )
                self.assertEqual(
                    statuses,
                    set(target["properties"]["status"]["enum"]),
                )
        self.assertEqual(
            {"live_recovery", "cached_safe_terminal"},
            set(
                recovery_result["properties"]["evidence_mode"]["enum"]
            ),
        )
        self.assertIn("evidence_mode", recovery_result["required"])
        self.assertIn(
            "prior_terminal_outcome", recovery_result["required"]
        )
        self.assertIn(
            "prior_terminal_receipt_sha256", recovery_result["required"]
        )
        self.assertEqual(
            2, recovery_result["properties"]["v"]["const"]
        )
        self.assertEqual(
            0, recovery_result["properties"]["edit_count"]["minimum"]
        )
        self.assertIn(
            "create_count", recovery_result["properties"]
        )
        source_branch = resolution["allOf"][0]
        self.assertEqual(
            "",
            source_branch["then"]["properties"][
                "resolver_job_id"
            ]["const"],
        )
        self.assertEqual(
            1,
            source_branch["else"]["properties"][
                "resolver_job_id"
            ]["minLength"],
        )

    def test_job_admission_and_result_error_conditionals_are_explicit(self) -> None:
        schema = JOB_RECEIPT_OUTPUT_SCHEMA
        admission = schema["$defs"]["admittedContract"]["oneOf"]
        self.assertEqual(
            {
                "#/$defs/multiEditAdmission",
                "#/$defs/treeAdmission",
                "#/$defs/scriptQueryAdmission",
                "#/$defs/inspectionAdmission",
                "#/$defs/hashedAdmission",
            },
            {item["$ref"] for item in admission},
        )
        for name in (
            "multiEditAdmission",
            "treeAdmission",
            "scriptQueryAdmission",
            "inspectionAdmission",
            "hashedAdmission",
        ):
            with self.subTest(admission=name):
                self.assertIs(
                    schema["$defs"][name]["additionalProperties"],
                    False,
                )
        multi_edit = schema["$defs"]["multiEditAdmission"]
        self.assertEqual(
            "studio-job-admission-v2",
            multi_edit["properties"]["contract_version"]["const"],
        )
        self.assertEqual(
            "edit-target-input-then-create-input-v2",
            multi_edit["properties"]["ordering_version"]["const"],
        )
        self.assertEqual(
            0, multi_edit["properties"]["edit_count"]["minimum"]
        )
        self.assertIn("create_count", multi_edit["required"])
        self.assertEqual(
            {
                "#/$defs/multiEditEditTarget",
                "#/$defs/multiEditCreateTarget",
            },
            {
                branch["$ref"]
                for branch in schema["$defs"]["multiEditTarget"]["oneOf"]
            },
        )

        encoded = json.dumps(schema["allOf"], sort_keys=True)
        for field in (
            "result_present",
            "error_present",
            "terminal",
            "status",
            "dispatched",
            "resolution_receipts",
            "resolved_by_exact_recovery:recovered",
        ):
            with self.subTest(conditional=field):
                self.assertIn(field, encoded)


if __name__ == "__main__":
    unittest.main()
