from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.catalog_review import (
    CatalogChange,
    CatalogReview,
    FAMILY_ALLOWED_ARGUMENTS,
    FAMILY_TO_DURABLE_HANDLER,
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

SEARCH_ARGUMENTS = frozenset(
    {
        "keywords",
        "root_path",
        "max_depth",
        "scan_limit",
        "page_size",
        "time_limit_ms",
        "continuation_cursor",
    }
)
GREP_ARGUMENTS = frozenset(
    {
        "query",
        "root_path",
        "max_depth",
        "case_sensitive",
        "scan_limit",
        "source_byte_limit",
        "page_size",
        "time_limit_ms",
        "continuation_cursor",
    }
)
IDENTITY_FIELDS = {
    "adapter",
    "v",
    "operation",
    "studio_id",
    "client_instance_id",
    "document_epoch",
    "generation",
    "request_id",
}
SEARCH_OUTPUT_FIELDS = IDENTITY_FIELDS | {
    "root_path",
    "keywords",
    "match_semantics",
    "query_version",
    "sort_version",
    "max_depth",
    "scan_limit",
    "page_size",
    "time_limit_ms",
    "scanned_instances",
    "scanned_scripts",
    "returned",
    "items",
    "truncated",
    "has_more",
    "continuation_cursor",
    "truncation_reason",
    "output_limit_bytes",
}
GREP_OUTPUT_FIELDS = IDENTITY_FIELDS | {
    "root_path",
    "query",
    "match_mode",
    "case_sensitive",
    "query_version",
    "sort_version",
    "max_depth",
    "scan_limit",
    "source_byte_limit",
    "page_size",
    "time_limit_ms",
    "scanned_instances",
    "scanned_scripts",
    "source_bytes_scanned",
    "returned",
    "items",
    "truncated",
    "has_more",
    "continuation_cursor",
    "truncation_reason",
    "output_limit_bytes",
}


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Phase2ScriptCatalogContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.durable, _durable_bytes = load_catalog(DURABLE_CATALOG)
        self.manifest = load_compatibility_manifest(
            COMPATIBILITY_MANIFEST
        )
        self.tools = {
            tool["name"]: tool for tool in self.durable["tools"]
        }
        self.search = self.tools["studio_search_scripts"]
        self.grep = self.tools["studio_grep_scripts"]

    @staticmethod
    def _catalog(*tools):
        return {
            "format": "phase2-script-contract-test",
            "tools": list(tools),
        }

    def _exact_upstream_alias(self, handler: str, alias: str):
        result = copy.deepcopy(self.tools[handler])
        result["name"] = alias
        return result

    def _review(self, baseline, candidate):
        candidate_bytes = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return review_catalogs(
            baseline,
            candidate,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )

    def _validate_contract(self, payload, manifest=None):
        with tempfile.TemporaryDirectory() as temporary:
            handler_source = Path(temporary) / "handlers.luau"
            handler_source.write_text(
                "\n".join(
                    'if request.operation == "' + handler + '" then'
                    for handler in sorted(
                        set(FAMILY_TO_DURABLE_HANDLER.values())
                    )
                ),
                encoding="utf-8",
            )
            return validate_durable_contract(
                payload,
                compatibility_manifest=manifest or self.manifest,
                handler_source_path=handler_source,
            )

    def test_exact_closed_input_contracts_and_bounds(self) -> None:
        cases = (
            (
                self.search,
                SEARCH_ARGUMENTS,
                "keywords",
                10,
            ),
            (
                self.grep,
                GREP_ARGUMENTS,
                "query",
                50,
            ),
        )
        for tool, arguments, required_query, page_maximum in cases:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                properties = schema["properties"]
                self.assertEqual(set(properties), arguments)
                self.assertEqual(schema["required"], [required_query])
                self.assertIs(schema["additionalProperties"], False)
                self.assertNotIn("studio_id", properties)
                query = properties[required_query]
                self.assertEqual(query["minLength"], 1)
                self.assertEqual(query["maxLength"], 256)
                self.assertEqual(query["pattern"], r"^[ -~]+$")
                self.assertEqual(
                    properties["root_path"]["minItems"], 0
                )
                self.assertEqual(
                    properties["root_path"]["maxItems"], 64
                )
                self.assertEqual(
                    properties["root_path"]["items"]["minLength"], 1
                )
                self.assertEqual(
                    properties["root_path"]["items"]["maxLength"], 100
                )
                self.assertEqual(properties["max_depth"]["minimum"], 0)
                self.assertEqual(properties["max_depth"]["maximum"], 64)
                self.assertEqual(properties["scan_limit"]["minimum"], 1)
                self.assertEqual(
                    properties["scan_limit"]["maximum"], 5000
                )
                self.assertEqual(properties["page_size"]["minimum"], 1)
                self.assertEqual(
                    properties["page_size"]["maximum"], page_maximum
                )
                self.assertEqual(
                    properties["time_limit_ms"]["minimum"], 100
                )
                self.assertEqual(
                    properties["time_limit_ms"]["maximum"], 10000
                )
                cursor = properties["continuation_cursor"]
                self.assertEqual(cursor["minLength"], 1)
                self.assertEqual(cursor["maxLength"], 2048)
                self.assertEqual(
                    cursor["pattern"],
                    r"^[A-Za-z0-9+/]+={0,2}\.[0-9a-f]{64}$",
                )
        self.assertEqual(
            FAMILY_ALLOWED_ARGUMENTS["script_name_search"],
            SEARCH_ARGUMENTS,
        )
        self.assertEqual(
            FAMILY_ALLOWED_ARGUMENTS["script_content_search"],
            GREP_ARGUMENTS,
        )
        source_limit = self.grep["inputSchema"]["properties"][
            "source_byte_limit"
        ]
        self.assertEqual(source_limit["minimum"], 262144)
        self.assertEqual(source_limit["maximum"], 4194304)
        self.assertEqual(
            self.grep["inputSchema"]["properties"]["case_sensitive"][
                "type"
            ],
            "boolean",
        )

    def test_nonfinite_catalog_numbers_fail_closed(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                for location in ("schema", "top_level"):
                    with self.subTest(location=location):
                        payload = copy.deepcopy(self.durable)
                        if location == "schema":
                            payload["tools"][0]["inputSchema"][
                                "properties"
                            ]["poison"] = {
                                "type": "number",
                                "maximum": value,
                            }
                        else:
                            payload["poison"] = value
                        with self.assertRaisesRegex(
                            ValidationError, "bounded JSON|non-JSON"
                        ):
                            validate_catalog_payload(payload)

        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "catalog.json"
            raw_cases = (
                (
                    '{"format":"test","tools":[{"name":"poison",'
                    '"inputSchema":{"type":"object","properties":'
                    '{"value":{"type":"number","maximum":NaN}},'
                    '"required":[]}}]}'
                ),
                '{"format":"test","poison":1e999,"tools":[]}',
            )
            for raw in raw_cases:
                with self.subTest(raw=raw):
                    candidate.write_text(raw, encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValidationError,
                        "valid UTF-8 JSON|non-JSON",
                    ):
                        load_catalog(candidate)

    def test_review_command_is_directly_executable_python(self) -> None:
        script = ROOT / "scripts" / "review_upstream_catalog.py"
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", str(script), "--help"],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--installed-v1-cache", completed.stdout)

    def test_exact_closed_search_output_contract(self) -> None:
        schema = self.search["outputSchema"]
        properties = schema["properties"]
        self.assertEqual(set(properties), SEARCH_OUTPUT_FIELDS)
        self.assertEqual(set(schema["required"]), SEARCH_OUTPUT_FIELDS)
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            properties["operation"]["const"],
            "studio_search_scripts",
        )
        self.assertEqual(
            properties["match_semantics"]["const"],
            "all_keywords_ascii_case_insensitive_literal_subsequence",
        )
        self.assertEqual(
            properties["query_version"]["const"],
            "script-name-query-v1",
        )
        self.assertEqual(
            properties["sort_version"]["const"], "name-class-v1"
        )
        self.assertEqual(properties["keywords"]["minItems"], 1)
        self.assertEqual(properties["keywords"]["maxItems"], 8)
        self.assertEqual(
            properties["keywords"]["items"]["maxLength"], 64
        )
        self.assertEqual(properties["returned"]["maximum"], 10)
        self.assertEqual(properties["items"]["maxItems"], 10)
        item = properties["items"]["items"]
        self.assertEqual(
            set(item["properties"]),
            {"path", "name", "class_name"},
        )
        self.assertEqual(
            set(item["required"]),
            {"path", "name", "class_name"},
        )
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(
            item["properties"]["class_name"]["enum"],
            ["Script", "LocalScript", "ModuleScript"],
        )
        self.assertEqual(
            properties["truncation_reason"]["enum"],
            [
                "complete",
                "page_size",
                "scan_limit",
                "time_budget",
                "output_bytes",
            ],
        )
        self.assertEqual(properties["output_limit_bytes"]["const"], 200000)

    def test_exact_closed_grep_output_contract(self) -> None:
        schema = self.grep["outputSchema"]
        properties = schema["properties"]
        self.assertEqual(set(properties), GREP_OUTPUT_FIELDS)
        self.assertEqual(set(schema["required"]), GREP_OUTPUT_FIELDS)
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            properties["operation"]["const"], "studio_grep_scripts"
        )
        self.assertEqual(properties["match_mode"]["const"], "literal")
        self.assertEqual(
            properties["query_version"]["const"],
            "script-grep-query-v1",
        )
        self.assertEqual(
            properties["sort_version"]["const"], "name-class-v1"
        )
        self.assertEqual(
            properties["source_bytes_scanned"]["maximum"], 4194304
        )
        self.assertEqual(properties["returned"]["maximum"], 50)
        self.assertEqual(properties["items"]["maxItems"], 50)
        item = properties["items"]["items"]
        expected_item_fields = {
            "path",
            "name",
            "class_name",
            "source_sha256",
            "source_length",
            "match_start_byte",
            "match_end_byte",
            "line_number",
            "column_byte",
            "preview_start_byte",
            "preview",
            "preview_prefix_truncated",
            "preview_suffix_truncated",
        }
        self.assertEqual(set(item["properties"]), expected_item_fields)
        self.assertEqual(set(item["required"]), expected_item_fields)
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(
            item["properties"]["source_sha256"]["pattern"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            item["properties"]["source_length"]["maximum"], 262144
        )
        self.assertEqual(
            item["properties"]["line_number"]["maximum"], 20000
        )
        self.assertEqual(
            item["properties"]["preview"]["maxLength"], 512
        )
        for field in (
            "match_start_byte",
            "match_end_byte",
            "line_number",
            "column_byte",
            "preview_start_byte",
        ):
            self.assertEqual(
                item["properties"][field]["minimum"], 1
            )
        self.assertEqual(
            properties["truncation_reason"]["enum"],
            [
                "complete",
                "page_size",
                "scan_limit",
                "source_bytes",
                "time_budget",
                "output_bytes",
            ],
        )
        self.assertEqual(properties["output_limit_bytes"]["const"], 500000)

    def test_identity_output_and_explicit_target_publication(self) -> None:
        catalog = ToolCatalog(self.durable["tools"])
        exposed = {
            tool["name"]: tool for tool in catalog.tools_for_mcp()
        }
        for handler in ("studio_search_scripts", "studio_grep_scripts"):
            public_name = handler + "_v2"
            with self.subTest(handler=handler):
                public = exposed[public_name]
                self.assertIn(
                    "studio_id", public["inputSchema"]["required"]
                )
                self.assertEqual(
                    public["inputSchema"]["properties"]["studio_id"][
                        "format"
                    ],
                    "uuid",
                )
                self.assertEqual(
                    public["outputSchema"],
                    self.tools[handler]["outputSchema"],
                )
                output = self.tools[handler]["outputSchema"][
                    "properties"
                ]
                self.assertEqual(
                    output["adapter"]["const"],
                    "studio-mcp-v2-durable-plugin",
                )
                self.assertEqual(output["v"]["const"], 1)
                self.assertEqual(output["studio_id"]["format"], "uuid")
                self.assertEqual(
                    output["client_instance_id"]["format"], "uuid"
                )
                self.assertEqual(
                    output["document_epoch"]["maxLength"], 128
                )
                self.assertEqual(output["generation"]["minimum"], 1)
                self.assertEqual(output["request_id"]["maxLength"], 128)
        self.assertNotIn("script_search", exposed)
        self.assertNotIn("script_grep", exposed)
        self.assertNotIn("studio_search_scripts", exposed)
        self.assertNotIn("studio_grep_scripts", exposed)

    def test_official_shapes_remain_incompatible_and_review_only(self) -> None:
        upstream, _upstream_bytes = load_catalog(UPSTREAM_CATALOG)
        upstream_tools = {
            tool["name"]: tool for tool in upstream["tools"]
        }
        expected = {
            "script_search": (
                "script_name_search",
                "studio_search_scripts",
            ),
            "script_grep": (
                "script_content_search",
                "studio_grep_scripts",
            ),
        }
        for name, (family, handler) in expected.items():
            with self.subTest(upstream=name):
                candidate = self._catalog(upstream_tools[name])
                review = self._review(self._catalog(), candidate)
                change = next(
                    item
                    for item in review.changes
                    if item.kind == "new"
                )
                self.assertEqual(change.family, family)
                self.assertEqual(change.durable_handler, handler)
                self.assertEqual(
                    change.compatibility, "incompatible_schema"
                )
                self.assertTrue(review.fail_closed)
                self.assertNotIn(name, self.tools)

    def test_exact_review_mapping_never_exposes_or_copies_aliases(self) -> None:
        exact = self._exact_upstream_alias(
            "studio_search_scripts", "script_search"
        )
        candidate = self._catalog(exact)
        candidate_bytes = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        review = self._review(self._catalog(), candidate)
        change = next(
            item for item in review.changes if item.kind == "new"
        )
        self.assertEqual(change.compatibility, "compatible_candidate")
        self.assertFalse(review.fail_closed)
        generated = regenerate_durable_catalog(
            self.durable,
            candidate,
            candidate_bytes,
            review,
            self.manifest,
        )
        generated_names = {
            tool["name"] for tool in generated["tools"]
        }
        self.assertIn("studio_search_scripts", generated_names)
        self.assertNotIn("script_search", generated_names)

        malicious = copy.deepcopy(exact)
        malicious["outputSchema"]["properties"]["output_limit_bytes"][
            "const"
        ] = 999999
        malicious_candidate = self._catalog(malicious)
        forged_review = CatalogReview(
            baseline_sha256="0" * 64,
            candidate_sha256="1" * 64,
            changes=(
                CatalogChange(
                    "new",
                    "script_search",
                    family="script_name_search",
                    durable_handler="studio_search_scripts",
                    compatibility="compatible_candidate",
                ),
            ),
            fail_closed=False,
        )
        generated = regenerate_durable_catalog(
            self.durable,
            malicious_candidate,
            json.dumps(malicious_candidate).encode("utf-8"),
            forged_review,
            self.manifest,
        )
        generated_search = next(
            tool
            for tool in generated["tools"]
            if tool["name"] == "studio_search_scripts"
        )
        self.assertEqual(
            generated_search["outputSchema"],
            self.search["outputSchema"],
        )

    def test_input_add_remove_and_tamper_fail_closed(self) -> None:
        exact = self._exact_upstream_alias(
            "studio_grep_scripts", "script_grep"
        )
        mutations = {}

        added = copy.deepcopy(exact)
        added["inputSchema"]["properties"]["raw_pattern"] = {
            "type": "string"
        }
        mutations["added"] = added

        removed = copy.deepcopy(exact)
        del removed["inputSchema"]["properties"]["case_sensitive"]
        mutations["removed"] = removed

        tampered = copy.deepcopy(exact)
        tampered["inputSchema"]["properties"]["scan_limit"][
            "maximum"
        ] = 5001
        mutations["tampered"] = tampered

        for label, tool in mutations.items():
            with self.subTest(mutation=label):
                review = self._review(
                    self._catalog(), self._catalog(tool)
                )
                change = next(
                    item
                    for item in review.changes
                    if item.kind == "new"
                )
                self.assertEqual(
                    change.compatibility, "incompatible_schema"
                )
                self.assertTrue(review.fail_closed)

    def test_output_changes_are_distinguished_and_fail_closed(self) -> None:
        exact = self._exact_upstream_alias(
            "studio_search_scripts", "script_search"
        )
        mutations = {}

        added = copy.deepcopy(exact)
        added["outputSchema"]["properties"]["unreviewed"] = {
            "type": "boolean"
        }
        added["outputSchema"]["required"].append("unreviewed")
        mutations["added"] = added

        removed = copy.deepcopy(exact)
        del removed["outputSchema"]["properties"]["returned"]
        removed["outputSchema"]["required"].remove("returned")
        mutations["removed"] = removed

        tampered = copy.deepcopy(exact)
        tampered["outputSchema"]["properties"]["returned"][
            "maximum"
        ] = 11
        mutations["tampered"] = tampered

        for label, tool in mutations.items():
            with self.subTest(mutation=label):
                review = self._review(
                    self._catalog(exact), self._catalog(tool)
                )
                changes = [
                    item
                    for item in review.changes
                    if item.kind == "output_schema_changed"
                ]
                self.assertEqual(len(changes), 1)
                self.assertEqual(
                    changes[0].compatibility,
                    "incompatible_output_schema",
                )
                self.assertTrue(review.fail_closed)
                self.assertFalse(
                    any(
                        item.kind == "metadata_changed"
                        for item in review.changes
                    )
                )

    def test_exact_input_and_output_digests_reject_contract_tamper(
        self,
    ) -> None:
        self.assertTrue(self._validate_contract(self.durable)[
            "closed_handler_contracts"
        ])
        cases = []

        input_tamper = copy.deepcopy(self.durable)
        input_tamper["tools"][2]["inputSchema"]["properties"][
            "page_size"
        ]["maximum"] = 11
        cases.append(("input", input_tamper, "input schema"))

        output_add = copy.deepcopy(self.durable)
        output_add["tools"][2]["outputSchema"]["properties"]["extra"] = {
            "type": "boolean"
        }
        output_add["tools"][2]["outputSchema"]["required"].append("extra")
        cases.append(("output_add", output_add, "output schema"))

        output_remove = copy.deepcopy(self.durable)
        del output_remove["tools"][3]["outputSchema"]["properties"][
            "returned"
        ]
        output_remove["tools"][3]["outputSchema"]["required"].remove(
            "returned"
        )
        cases.append(("output_remove", output_remove, "output schema"))

        output_tamper = copy.deepcopy(self.durable)
        output_tamper["tools"][3]["outputSchema"]["properties"][
            "output_limit_bytes"
        ]["const"] = 500001
        cases.append(("output_tamper", output_tamper, "output schema"))

        for label, payload, expected in cases:
            with self.subTest(mutation=label):
                with self.assertRaisesRegex(
                    ValidationError,
                    expected + " does not match its reviewed SHA-256",
                ):
                    self._validate_contract(payload)

    def test_manifest_pins_all_input_output_and_absent_contracts(
        self,
    ) -> None:
        handlers = set(FAMILY_TO_DURABLE_HANDLER.values())
        self.assertEqual(
            set(self.manifest.durable_handler_schema_sha256),
            handlers,
        )
        self.assertEqual(
            set(self.manifest.durable_handler_output_schema_sha256),
            handlers,
        )
        self.assertEqual(self.manifest.manifest_version, "3")
        self.assertEqual(
            self.manifest.schema_policy, "exact_handler_contract"
        )
        absent_digest = _canonical_sha256(None)
        for handler in handlers:
            tool = self.tools[handler]
            self.assertEqual(
                self.manifest.durable_handler_schema_sha256[handler],
                _canonical_sha256(tool["inputSchema"]),
            )
            self.assertEqual(
                self.manifest.durable_handler_output_schema_sha256[
                    handler
                ],
                _canonical_sha256(tool.get("outputSchema")),
            )
            if "outputSchema" not in tool:
                self.assertEqual(
                    self.manifest
                    .durable_handler_output_schema_sha256[handler],
                    absent_digest,
                )

        mismatched = dict(
            self.manifest.durable_handler_output_schema_sha256
        )
        mismatched["studio_search_scripts"] = "0" * 64
        bad_manifest = replace(
            self.manifest,
            durable_handler_output_schema_sha256=mismatched,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "output schema does not match its reviewed SHA-256",
        ):
            self._validate_contract(self.durable, bad_manifest)

        manifest_payload = json.loads(
            COMPATIBILITY_MANIFEST.read_text(encoding="utf-8")
        )
        omissions = (
            "durable_handler_schema_sha256",
            "durable_handler_output_schema_sha256",
        )
        for field in omissions:
            with self.subTest(omitted_map=field):
                payload = copy.deepcopy(manifest_payload)
                del payload[field]["studio_search_scripts"]
                with tempfile.TemporaryDirectory() as temporary:
                    candidate = Path(temporary) / "manifest.json"
                    candidate.write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValidationError):
                        load_compatibility_manifest(candidate)

    def test_optional_output_schema_must_be_an_object_schema(self) -> None:
        malformed = copy.deepcopy(self.durable)
        malformed["tools"][0]["outputSchema"] = None
        with self.assertRaisesRegex(
            ValidationError,
            "outputSchema must be an object schema",
        ):
            validate_catalog_payload(malformed)


if __name__ == "__main__":
    unittest.main()
