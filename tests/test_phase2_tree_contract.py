from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from studio_mcp_v2.catalog import DISCOVERY_TOOL, ToolCatalog
from studio_mcp_v2.catalog_review import (
    FAMILY_ALLOWED_ARGUMENTS,
    load_catalog,
    load_compatibility_manifest,
    regenerate_durable_catalog,
    review_catalogs,
    validate_catalog_payload,
    validate_durable_contract,
)
from studio_mcp_v2.errors import ValidationError


ROOT = Path(__file__).resolve().parent.parent
DURABLE_CATALOG = ROOT / "config" / "durable-tool-catalog.json"
UPSTREAM_CATALOG = ROOT / "config" / "tool-catalog.json"
COMPATIBILITY_MANIFEST = (
    ROOT / "config" / "upstream-compatibility-map.json"
)

TREE_ARGUMENTS = frozenset(
    {
        "root_path",
        "max_depth",
        "max_results",
        "name_filter",
        "class_filter",
        "class_is_a",
        "scan_limit",
        "page_size",
        "continuation_cursor",
    }
)


class Phase2TreeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.durable, _durable_bytes = load_catalog(DURABLE_CATALOG)
        self.manifest = load_compatibility_manifest(
            COMPATIBILITY_MANIFEST
        )
        self.tools = {
            tool["name"]: tool for tool in self.durable["tools"]
        }
        self.tree = self.tools["studio_list_tree"]

    @staticmethod
    def _catalog(*tools):
        return {
            "format": "phase2-tree-contract-test",
            "tools": list(tools),
        }

    @staticmethod
    def _tree_tool(payload):
        return next(
            tool
            for tool in payload["tools"]
            if tool["name"] == "studio_list_tree"
        )

    def _review_added_search_tool(self, tool):
        candidate = self._catalog(tool)
        candidate_bytes = (
            json.dumps(candidate, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        )
        review = review_catalogs(
            self._catalog(),
            candidate,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )
        return candidate, candidate_bytes, review

    def test_tree_query_schema_is_bounded_backward_compatible_and_targeted(
        self,
    ) -> None:
        schema = self.tree["inputSchema"]
        properties = schema["properties"]

        self.assertEqual(set(properties), TREE_ARGUMENTS)
        self.assertEqual(schema["required"], [])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            FAMILY_ALLOWED_ARGUMENTS["tree_read"],
            TREE_ARGUMENTS,
        )

        self.assertEqual(properties["max_depth"]["minimum"], 0)
        self.assertEqual(properties["max_depth"]["maximum"], 6)
        self.assertIn(
            "root_path segment count plus max_depth",
            properties["max_depth"]["description"],
        )
        self.assertEqual(properties["max_results"]["minimum"], 1)
        self.assertEqual(properties["max_results"]["maximum"], 500)
        self.assertIn(
            "Legacy page-size alias",
            properties["max_results"]["description"],
        )
        self.assertEqual(properties["root_path"]["minItems"], 0)
        self.assertEqual(properties["root_path"]["maxItems"], 64)
        self.assertEqual(
            properties["root_path"]["items"]["maxLength"],
            100,
        )

        name_filter = properties["name_filter"]
        self.assertEqual(name_filter["type"], "string")
        self.assertEqual(name_filter["minLength"], 1)
        self.assertEqual(name_filter["maxLength"], 100)
        self.assertEqual(
            name_filter["pattern"],
            r"^[^\u0000-\u001f\u007f]+$",
        )
        self.assertIn("literal", name_filter["description"])
        self.assertIn(
            "case-insensitive for ASCII letters",
            name_filter["description"],
        )
        class_filter = properties["class_filter"]
        self.assertEqual(class_filter["type"], "string")
        self.assertEqual(class_filter["minLength"], 1)
        self.assertEqual(class_filter["maxLength"], 100)
        self.assertEqual(
            class_filter["pattern"],
            r"^[A-Za-z_][A-Za-z0-9_]*$",
        )
        self.assertIn("literal", class_filter["description"])
        self.assertEqual(properties["class_is_a"]["type"], "boolean")
        self.assertIn(
            "Invalid unless class_filter is supplied",
            properties["class_is_a"]["description"],
        )
        self.assertEqual(properties["scan_limit"]["minimum"], 1)
        self.assertEqual(properties["scan_limit"]["maximum"], 5000)
        self.assertIn(
            "Default: 2000",
            properties["scan_limit"]["description"],
        )
        self.assertEqual(properties["page_size"]["minimum"], 1)
        self.assertEqual(properties["page_size"]["maximum"], 500)
        self.assertIn(
            "Do not combine with max_results",
            properties["page_size"]["description"],
        )
        self.assertEqual(
            properties["continuation_cursor"]["minLength"],
            1,
        )
        self.assertEqual(
            properties["continuation_cursor"]["maxLength"],
            512,
        )
        self.assertEqual(
            properties["continuation_cursor"]["pattern"],
            r"^[A-Za-z0-9+/]+={0,2}\.[0-9a-f]{64}$",
        )
        cursor_description = properties["continuation_cursor"][
            "description"
        ]
        for fence in (
            "explicit Studio session",
            "document epoch",
            "generation",
            "query",
            "continuation lineage",
        ):
            self.assertIn(fence, cursor_description)

        response_description = self.tree["description"]
        for field in (
            "root_path",
            "items",
            "truncated",
            "max_depth",
            "max_results",
            "returned",
            "scanned",
            "scan_limit",
            "page_size",
            "has_more",
            "continuation_cursor",
        ):
            self.assertIn(field, response_description)

        exposed = {
            tool["name"]: tool
            for tool in ToolCatalog(self.durable["tools"]).tools_for_mcp()
        }
        public_schema = exposed["studio_list_tree_v2"]["inputSchema"]
        self.assertIn("studio_id", public_schema["required"])
        self.assertEqual(
            public_schema["properties"]["studio_id"]["format"],
            "uuid",
        )
        self.assertNotIn("studio_id", properties)
        self.assertNotIn(
            "studio_id",
            DISCOVERY_TOOL["inputSchema"]["properties"],
        )

    def test_durable_tree_contract_rejects_unknown_fields_and_shapes(
        self,
    ) -> None:
        validated = validate_durable_contract(
            self.durable,
            compatibility_manifest=self.manifest,
        )
        self.assertTrue(validated["closed_handler_schemas"])

        unknown_argument = copy.deepcopy(self.durable)
        self._tree_tool(unknown_argument)["inputSchema"]["properties"][
            "raw_query"
        ] = {"type": "string"}
        with self.assertRaisesRegex(
            ValidationError,
            "exact closed argument contract",
        ):
            validate_durable_contract(
                unknown_argument,
                compatibility_manifest=self.manifest,
            )

        open_schema = copy.deepcopy(self.durable)
        self._tree_tool(open_schema)["inputSchema"][
            "additionalProperties"
        ] = True
        with self.assertRaisesRegex(
            ValidationError,
            "exact closed argument contract",
        ):
            validate_durable_contract(
                open_schema,
                compatibility_manifest=self.manifest,
            )

        unknown_shape = copy.deepcopy(self.durable)
        self._tree_tool(unknown_shape)["inputSchema"][
            "unevaluatedProperties"
        ] = False
        with self.assertRaisesRegex(
            ValidationError,
            "exact closed argument contract",
        ):
            validate_durable_contract(
                unknown_shape,
                compatibility_manifest=self.manifest,
            )

        malformed_property = copy.deepcopy(self.durable)
        self._tree_tool(malformed_property)["inputSchema"]["properties"][
            "scan_limit"
        ] = "integer"
        with self.assertRaisesRegex(
            ValidationError,
            "schema properties are invalid",
        ):
            validate_catalog_payload(malformed_property)

    def test_reviewed_digest_rejects_every_known_field_schema_tamper(
        self,
    ) -> None:
        canonical_tree_schema = json.dumps(
            self.tree["inputSchema"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical_tree_schema).hexdigest(),
            self.manifest.durable_handler_schema_sha256[
                "studio_list_tree"
            ],
        )

        def change_type(schema):
            schema["properties"]["page_size"]["type"] = "string"

        def raise_bound(schema):
            schema["properties"]["page_size"]["maximum"] = 999999

        def loosen_pattern(schema):
            schema["properties"]["continuation_cursor"][
                "pattern"
            ] = ".*"

        def add_required_argument(schema):
            schema["required"].append("continuation_cursor")

        def rewrite_description(schema):
            schema["properties"]["page_size"][
                "description"
            ] = "Unreviewed replacement description."

        mutations = {
            "type": change_type,
            "bound": raise_bound,
            "pattern": loosen_pattern,
            "required": add_required_argument,
            "description": rewrite_description,
        }
        for label, mutate in mutations.items():
            with self.subTest(tamper=label):
                candidate = copy.deepcopy(self.durable)
                mutate(self._tree_tool(candidate)["inputSchema"])
                with self.assertRaisesRegex(
                    ValidationError,
                    "does not match its reviewed SHA-256",
                ):
                    validate_durable_contract(
                        candidate,
                        compatibility_manifest=self.manifest,
                    )

    def test_manifest_schema_digest_mismatch_and_omission_fail_closed(
        self,
    ) -> None:
        mismatched_digests = dict(
            self.manifest.durable_handler_schema_sha256
        )
        mismatched_digests["studio_list_tree"] = "0" * 64
        mismatched_manifest = replace(
            self.manifest,
            durable_handler_schema_sha256=mismatched_digests,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "does not match its reviewed SHA-256",
        ):
            validate_durable_contract(
                self.durable,
                compatibility_manifest=mismatched_manifest,
            )

        payload = json.loads(
            COMPATIBILITY_MANIFEST.read_text(encoding="utf-8")
        )
        del payload["durable_handler_schema_sha256"][
            "studio_list_tree"
        ]
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "compatibility.json"
            candidate.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError,
                "pin every durable handler schema",
            ):
                load_compatibility_manifest(candidate)

    def test_review_gate_requires_exact_tree_shape_and_never_adds_alias(
        self,
    ) -> None:
        exact = copy.deepcopy(self.tree)
        exact["name"] = "search_game_tree"
        candidate, candidate_bytes, exact_review = (
            self._review_added_search_tool(exact)
        )
        added = [
            change
            for change in exact_review.changes
            if change.kind == "new"
        ]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].family, "tree_read")
        self.assertEqual(
            added[0].durable_handler,
            "studio_list_tree",
        )
        self.assertEqual(
            added[0].compatibility,
            "compatible_candidate",
        )
        self.assertFalse(exact_review.fail_closed)

        generated = regenerate_durable_catalog(
            self.durable,
            candidate,
            candidate_bytes,
            exact_review,
            self.manifest,
        )
        generated_names = {tool["name"] for tool in generated["tools"]}
        self.assertIn("studio_list_tree", generated_names)
        self.assertNotIn("search_game_tree", generated_names)

        extra_argument = copy.deepcopy(exact)
        extra_argument["inputSchema"]["properties"]["pattern"] = {
            "type": "string"
        }
        _candidate, _candidate_bytes, extra_review = (
            self._review_added_search_tool(extra_argument)
        )
        extra_change = next(
            change
            for change in extra_review.changes
            if change.kind == "new"
        )
        self.assertEqual(
            extra_change.compatibility,
            "incompatible_schema",
        )
        self.assertTrue(extra_review.fail_closed)

        changed_bound = copy.deepcopy(exact)
        changed_bound["inputSchema"]["properties"]["scan_limit"][
            "maximum"
        ] = 5001
        _candidate, _candidate_bytes, bound_review = (
            self._review_added_search_tool(changed_bound)
        )
        bound_change = next(
            change
            for change in bound_review.changes
            if change.kind == "new"
        )
        self.assertEqual(
            bound_change.compatibility,
            "incompatible_schema",
        )
        self.assertTrue(bound_review.fail_closed)

    def test_current_upstream_search_shape_remains_review_only(self) -> None:
        upstream, _upstream_bytes = load_catalog(UPSTREAM_CATALOG)
        search = next(
            tool
            for tool in upstream["tools"]
            if tool["name"] == "search_game_tree"
        )
        _candidate, _candidate_bytes, review = (
            self._review_added_search_tool(search)
        )
        change = next(
            item for item in review.changes if item.kind == "new"
        )
        self.assertEqual(change.family, "tree_read")
        self.assertEqual(
            change.durable_handler,
            "studio_list_tree",
        )
        self.assertEqual(
            change.compatibility,
            "incompatible_schema",
        )
        self.assertTrue(review.fail_closed)
        self.assertNotIn("search_game_tree", self.tools)


if __name__ == "__main__":
    unittest.main()
