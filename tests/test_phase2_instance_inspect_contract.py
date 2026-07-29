from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from studio_mcp_v2.catalog import DISCOVERY_TOOL, ToolCatalog
from studio_mcp_v2.catalog_review import (
    FAMILY_ALLOWED_ARGUMENTS,
    FAMILY_TO_DURABLE_HANDLER,
    load_catalog,
    load_compatibility_manifest,
    review_catalogs,
    validate_durable_contract,
)
from studio_mcp_v2.errors import ValidationError


ROOT = Path(__file__).resolve().parent.parent
DURABLE_CATALOG = ROOT / "config" / "durable-tool-catalog.json"
UPSTREAM_CATALOG = ROOT / "config" / "tool-catalog.json"
COMPATIBILITY_MANIFEST = (
    ROOT / "config" / "upstream-compatibility-map.json"
)

INSPECT_ARGUMENTS = frozenset(
    {
        "path",
        "child_limit",
        "descendant_max_depth",
        "descendant_scan_limit",
        "time_limit_ms",
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
INSPECT_OUTPUT_FIELDS = IDENTITY_FIELDS | {
    "datamodel_type",
    "path",
    "name",
    "class_name",
    "snapshot_contract",
    "property_allowlist_version",
    "value_encoding_version",
    "sort_version",
    "child_limit",
    "descendant_max_depth",
    "descendant_scan_limit",
    "time_limit_ms",
    "properties",
    "property_count",
    "properties_complete",
    "attributes",
    "attributes_total",
    "attributes_returned",
    "attributes_truncated",
    "tags",
    "tags_total",
    "tags_returned",
    "tags_truncated",
    "children",
    "children_total",
    "children_returned",
    "children_truncated",
    "children_truncation_reason",
    "descendant_count",
    "descendant_count_complete",
    "descendant_truncation_reason",
    "descendant_class_counts",
    "output_limit_bytes",
}
SAFE_VALUE_FIELDS = {
    "type",
    "boolean_value",
    "number_value",
    "text",
    "numbers",
    "labels",
    "byte_length",
    "truncated",
}
PROPERTY_SELECTORS = [
    "BasePart.Anchored",
    "BasePart.CFrame",
    "BasePart.CanCollide",
    "BasePart.CanQuery",
    "BasePart.CanTouch",
    "BasePart.CastShadow",
    "BasePart.CollisionGroup",
    "BasePart.Color",
    "BasePart.Locked",
    "BasePart.Massless",
    "BasePart.Material",
    "BasePart.MaterialVariant",
    "BasePart.Reflectance",
    "BasePart.Size",
    "BasePart.Transparency",
    "BaseScript.Enabled",
    "BaseScript.RunContext",
    "GuiObject.Active",
    "GuiObject.AnchorPoint",
    "GuiObject.BackgroundColor3",
    "GuiObject.BackgroundTransparency",
    "GuiObject.BorderColor3",
    "GuiObject.BorderSizePixel",
    "GuiObject.ClipsDescendants",
    "GuiObject.LayoutOrder",
    "GuiObject.Position",
    "GuiObject.Rotation",
    "GuiObject.Size",
    "GuiObject.Visible",
    "GuiObject.ZIndex",
    "Instance.Archivable",
    "LayerCollector.Enabled",
    "LayerCollector.ResetOnSpawn",
    "LayerCollector.ZIndexBehavior",
]


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Phase2InstanceInspectContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.durable, _durable_bytes = load_catalog(DURABLE_CATALOG)
        self.upstream, _upstream_bytes = load_catalog(UPSTREAM_CATALOG)
        self.manifest = load_compatibility_manifest(
            COMPATIBILITY_MANIFEST
        )
        self.tools = {
            tool["name"]: tool for tool in self.durable["tools"]
        }
        self.inspect = self.tools["studio_inspect_instance"]

    @staticmethod
    def _catalog(*tools):
        return {
            "format": "phase2-instance-inspect-contract-test",
            "tools": list(tools),
        }

    def _validate_contract(self, payload):
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
                compatibility_manifest=self.manifest,
                handler_source_path=handler_source,
            )

    def test_catalog_order_exact_input_contract_and_bounds(self) -> None:
        names = [tool["name"] for tool in self.durable["tools"]]
        self.assertEqual(
            names.index("studio_inspect_instance"),
            names.index("studio_list_tree") + 1,
        )
        self.assertEqual("0.4.0-dev.3", self.durable["catalog_version"])
        schema = self.inspect["inputSchema"]
        properties = schema["properties"]
        self.assertEqual(set(properties), INSPECT_ARGUMENTS)
        self.assertEqual(schema["required"], ["path"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            FAMILY_ALLOWED_ARGUMENTS["instance_inspection"],
            INSPECT_ARGUMENTS,
        )
        path = properties["path"]
        self.assertEqual(path["minItems"], 1)
        self.assertEqual(path["maxItems"], 64)
        self.assertEqual(path["items"]["minLength"], 1)
        self.assertEqual(path["items"]["maxLength"], 100)
        self.assertEqual(properties["child_limit"]["minimum"], 0)
        self.assertEqual(properties["child_limit"]["maximum"], 200)
        self.assertEqual(
            properties["descendant_max_depth"]["minimum"], 0
        )
        self.assertEqual(
            properties["descendant_max_depth"]["maximum"], 64
        )
        self.assertIn(
            "64 minus the target path segment count",
            properties["descendant_max_depth"]["description"],
        )
        self.assertEqual(
            properties["descendant_scan_limit"]["minimum"], 1
        )
        self.assertEqual(
            properties["descendant_scan_limit"]["maximum"], 5000
        )
        self.assertEqual(properties["time_limit_ms"]["minimum"], 100)
        self.assertEqual(properties["time_limit_ms"]["maximum"], 10000)

    def test_exact_closed_output_contract_and_safe_value_bounds(self) -> None:
        schema = self.inspect["outputSchema"]
        properties = schema["properties"]
        self.assertEqual(
            set(schema),
            {
                "type",
                "$defs",
                "properties",
                "required",
                "additionalProperties",
            },
        )
        self.assertEqual(set(properties), INSPECT_OUTPUT_FIELDS)
        self.assertEqual(set(schema["required"]), INSPECT_OUTPUT_FIELDS)
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(
            properties["adapter"]["const"],
            "studio-mcp-v2-durable-plugin",
        )
        self.assertEqual(properties["v"]["const"], 1)
        self.assertEqual(
            properties["operation"]["const"],
            "studio_inspect_instance",
        )
        self.assertEqual(properties["datamodel_type"]["const"], "Edit")
        self.assertEqual(
            properties["snapshot_contract"]["const"],
            "path-edit-generation-fenced-observational-v1",
        )
        self.assertEqual(
            properties["property_allowlist_version"]["const"],
            "instance-property-allowlist-v1",
        )
        self.assertEqual(
            properties["value_encoding_version"]["const"],
            "instance-value-v1",
        )
        self.assertEqual(
            properties["sort_version"]["const"],
            "name-class-original-v1",
        )
        self.assertEqual(properties["output_limit_bytes"]["const"], 500000)

        safe_value = schema["$defs"]["safeValue"]
        self.assertEqual(
            set(safe_value["properties"]), SAFE_VALUE_FIELDS
        )
        self.assertEqual(set(safe_value["required"]), SAFE_VALUE_FIELDS)
        self.assertIs(safe_value["additionalProperties"], False)
        self.assertEqual(
            safe_value["properties"]["type"]["enum"],
            [
                "nil",
                "unavailable",
                "unsupported",
                "boolean",
                "number",
                "string",
                "enum",
                "vector2",
                "vector3",
                "color3",
                "cframe",
                "udim",
                "udim2",
                "rect",
                "brick_color",
                "number_range",
                "number_sequence",
                "color_sequence",
                "font",
            ],
        )
        self.assertEqual(
            safe_value["properties"]["text"]["maxLength"], 1024
        )
        self.assertEqual(
            safe_value["properties"]["numbers"]["maxItems"], 256
        )
        self.assertEqual(
            safe_value["properties"]["labels"]["maxItems"], 3
        )
        self.assertEqual(
            safe_value["properties"]["byte_length"]["maximum"], 262144
        )

        property_items = properties["properties"]["items"]
        self.assertEqual(
            set(property_items["properties"]), {"selector", "value"}
        )
        self.assertEqual(
            set(property_items["required"]), {"selector", "value"}
        )
        self.assertIs(property_items["additionalProperties"], False)
        self.assertEqual(
            property_items["properties"]["selector"]["enum"],
            PROPERTY_SELECTORS,
        )
        self.assertEqual(PROPERTY_SELECTORS, sorted(PROPERTY_SELECTORS))
        self.assertEqual(properties["properties"]["maxItems"], 34)
        self.assertEqual(properties["property_count"]["maximum"], 34)
        self.assertEqual(properties["attributes"]["maxItems"], 64)
        self.assertEqual(properties["attributes_total"]["maximum"], 1024)
        attribute = properties["attributes"]["items"]
        self.assertEqual(
            set(attribute["properties"]), {"name", "value"}
        )
        self.assertEqual(
            set(attribute["required"]), {"name", "value"}
        )
        self.assertIs(attribute["additionalProperties"], False)
        self.assertEqual(properties["tags"]["maxItems"], 128)
        self.assertEqual(properties["tags_total"]["maximum"], 1024)
        self.assertEqual(properties["children"]["maxItems"], 200)
        self.assertEqual(properties["children_total"]["maximum"], 10000)
        self.assertEqual(
            properties["children_truncation_reason"]["enum"],
            ["complete", "child_limit", "output_bytes"],
        )
        self.assertEqual(
            properties["descendant_truncation_reason"]["enum"],
            ["complete", "scan_limit", "time_limit", "depth_limit"],
        )
        self.assertEqual(
            properties["descendant_class_counts"]["maxItems"], 256
        )
        class_count = properties["descendant_class_counts"]["items"]
        self.assertEqual(
            set(class_count["properties"]), {"class_name", "count"}
        )
        self.assertEqual(
            set(class_count["required"]), {"class_name", "count"}
        )
        self.assertIs(class_count["additionalProperties"], False)
        child = properties["children"]["items"]
        self.assertEqual(
            set(child["properties"]),
            {"name", "class_name", "addressable", "path"},
        )
        self.assertEqual(set(child["required"]), set(child["properties"]))
        self.assertIs(child["additionalProperties"], False)
        self.assertEqual(
            child["properties"]["path"]["minItems"],
            0,
        )

    def test_publication_injects_only_explicit_studio_target(self) -> None:
        raw_properties = self.inspect["inputSchema"]["properties"]
        self.assertNotIn("studio_id", raw_properties)
        exposed = {
            tool["name"]: tool
            for tool in ToolCatalog(self.durable["tools"]).tools_for_mcp()
        }
        self.assertIn("studio_inspect_instance_v2", exposed)
        self.assertNotIn("inspect_instance_v2", exposed)
        public = exposed["studio_inspect_instance_v2"]
        self.assertIn("studio_id", public["inputSchema"]["required"])
        self.assertEqual(
            public["inputSchema"]["properties"]["studio_id"]["format"],
            "uuid",
        )
        self.assertNotIn(
            "studio_id",
            DISCOVERY_TOOL["inputSchema"]["properties"],
        )

    def test_manifest_pins_exact_input_and_output_contracts(self) -> None:
        self.assertEqual(
            self.manifest.durable_handler_schema_sha256[
                "studio_inspect_instance"
            ],
            _canonical_sha256(self.inspect["inputSchema"]),
        )
        self.assertEqual(
            self.manifest.durable_handler_output_schema_sha256[
                "studio_inspect_instance"
            ],
            _canonical_sha256(self.inspect["outputSchema"]),
        )
        validated = self._validate_contract(self.durable)
        self.assertEqual(validated["reviewed_handler_schema_count"], 12)
        self.assertEqual(
            validated["reviewed_handler_output_schema_count"], 12
        )

    def test_official_shape_is_mapped_but_incompatible_and_review_only(
        self,
    ) -> None:
        official = next(
            tool
            for tool in self.upstream["tools"]
            if tool["name"] == "inspect_instance"
        )
        candidate = self._catalog(copy.deepcopy(official))
        candidate_bytes = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        review = review_catalogs(
            self._catalog(),
            candidate,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )
        self.assertTrue(review.fail_closed)
        self.assertEqual(len(review.changes), 1)
        change = review.changes[0]
        self.assertEqual(change.name, "inspect_instance")
        self.assertEqual(change.family, "instance_inspection")
        self.assertEqual(
            change.durable_handler, "studio_inspect_instance"
        )
        self.assertEqual(change.compatibility, "incompatible_schema")

    def test_mapped_upstream_output_drift_fails_closed_separately(
        self,
    ) -> None:
        drifted = copy.deepcopy(self.inspect)
        drifted["name"] = "inspect_instance"
        del drifted["outputSchema"]
        candidate = self._catalog(drifted)
        candidate_bytes = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        review = review_catalogs(
            self._catalog(),
            candidate,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=self.manifest,
            durable_payload=self.durable,
        )
        self.assertTrue(review.fail_closed)
        self.assertEqual(len(review.changes), 1)
        change = review.changes[0]
        self.assertEqual(change.name, "inspect_instance")
        self.assertEqual(change.family, "instance_inspection")
        self.assertEqual(
            change.durable_handler, "studio_inspect_instance"
        )
        self.assertEqual(
            change.compatibility, "incompatible_output_schema"
        )

    def test_input_or_output_contract_drift_fails_exact_pin(self) -> None:
        cases = (
            ("input", "input schema"),
            ("output", "output schema"),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind):
                payload = copy.deepcopy(self.durable)
                inspect = next(
                    tool
                    for tool in payload["tools"]
                    if tool["name"] == "studio_inspect_instance"
                )
                if kind == "input":
                    inspect["inputSchema"]["properties"]["child_limit"][
                        "maximum"
                    ] = 201
                else:
                    inspect["outputSchema"]["$defs"]["safeValue"][
                        "properties"
                    ]["text"]["maxLength"] = 1025
                with self.assertRaisesRegex(ValidationError, expected):
                    self._validate_contract(payload)


if __name__ == "__main__":
    unittest.main()
