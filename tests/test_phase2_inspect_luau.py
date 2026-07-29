from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts import render_studio_plugin


ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
TOKEN = "t" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"

SELECTORS = [
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
ATTRIBUTE_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")


def descendant_initial_model(
    *,
    root_child_count: int,
    max_depth: int,
    elapsed_ms: int,
    time_limit_ms: int,
) -> tuple[int, bool, str] | None:
    if root_child_count == 0:
        return 0, True, "complete"
    if elapsed_ms >= time_limit_ms:
        return 0, False, "time_limit"
    if max_depth == 0:
        return 0, False, "depth_limit"
    return None


def child_summary_model(
    *,
    names: list[str],
    returned_count: int,
    reason: str,
    parent_path: tuple[str, ...] = ("Workspace",),
) -> list[dict[str, object]]:
    counts = {name: names.count(name) for name in names}
    returned = [
        {
            "name": name,
            "addressable": counts[name] == 1,
            "path": list(parent_path + (name,))
            if counts[name] == 1
            else [],
        }
        for name in names[:returned_count]
    ]
    if reason != "complete" and returned:
        returned[-1]["addressable"] = False
        returned[-1]["path"] = []
    return returned


def inspection_attribute_name_model(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        1 <= len(encoded) <= 100
        and ATTRIBUTE_NAME.fullmatch(value) is not None
    )


class Phase2InspectLuauTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HANDLERS.read_text(encoding="utf-8")
        cls.inspect = cls.source[
            cls.source.index("local function newInspectionValue(") :
            cls.source.index("local function readScript(")
        ]

    def test_closed_args_defaults_and_bounds_are_exact(self) -> None:
        keys = self.source[
            self.source.index(
                "studio_inspect_instance = table.freeze({"
            ) :
            self.source.index(
                "studio_search_scripts = table.freeze({"
            )
        ]
        self.assertEqual(
            {
                "path",
                "child_limit",
                "descendant_max_depth",
                "descendant_scan_limit",
                "time_limit_ms",
            },
            set(re.findall(r"\n\t\t([a-z_]+) = true,", keys)),
        )
        for marker in (
            "local MAX_INSPECT_CHILD_LIMIT = 200",
            "local DEFAULT_INSPECT_CHILD_LIMIT = 50",
            "local MAX_INSPECT_DESCENDANT_DEPTH = 64",
            "local MAX_INSPECT_DESCENDANT_SCAN_LIMIT = 5_000",
            "local DEFAULT_INSPECT_DESCENDANT_SCAN_LIMIT = 2_000",
            "local MIN_INSPECT_TIME_LIMIT_MS = 100",
            "local MAX_INSPECT_TIME_LIMIT_MS = 10_000",
            "local DEFAULT_INSPECT_TIME_LIMIT_MS = 3_000",
            "local MAX_INSPECT_CHILD_OUTPUT_BYTES = 300_000",
            "local MAX_INSPECT_RESULT_BYTES = 500_000",
            "local MAX_INSPECT_ATTRIBUTES_RAW = 1_024",
            "local MAX_INSPECT_TAGS_RAW = 1_024",
        ):
            self.assertIn(marker, self.source)
        validation = self.source[
            self.source.index(
                'elseif operation == "studio_inspect_instance"'
            ) :
            self.source.index(
                'elseif operation == "studio_read_script"'
            )
        ]
        self.assertIn(
            'validatePath(args.path, false, "path")',
            validation,
        )
        self.assertIn("#path + descendantMaxDepth", validation)
        self.assertIn(
            '"path plus descendant_max_depth exceeds the reusable path bound"',
            validation,
        )

    def test_property_allowlist_is_fixed_frozen_and_byte_sorted(self) -> None:
        allowlist = self.source[
            self.source.index(
                "local INSPECT_PROPERTY_ALLOWLIST = table.freeze({"
            ) :
            self.source.index("local function inspectProperties(")
        ]
        selectors = re.findall(r'selector = "([^"]+)"', allowlist)
        self.assertEqual(SELECTORS, selectors)
        self.assertEqual(sorted(selectors), selectors)
        self.assertEqual(34, allowlist.count("getter = function(target)"))
        self.assertGreaterEqual(allowlist.count("table.freeze({"), 35)
        self.assertNotIn("target[property", allowlist)
        self.assertNotIn("GetPropertyChangedSignal", allowlist)

    def test_value_encoding_is_closed_finite_and_uniform(self) -> None:
        common = self.inspect[
            self.inspect.index("local function newInspectionValue(") :
            self.inspect.index("local function inspectionUtf8Prefix(")
        ]
        for field in (
            "type = valueType",
            "boolean_value = false",
            "number_value = 0",
            'text = ""',
            "numbers = {}",
            "labels = {}",
            "byte_length = 0",
            "truncated = false",
        ):
            self.assertIn(field, common)
        for value_type in (
            "nil",
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
            "unsupported",
            "unavailable",
        ):
            self.assertIn(f'"{value_type}"', self.inspect)
        self.assertIn("isFiniteNumber", self.inspect)
        self.assertIn("utf8.len(value)", self.inspect)
        self.assertIn("utf8.offset(", self.inspect)
        self.assertIn("#value > MAX_INSPECT_STRING_RAW_BYTES", self.inspect)
        self.assertIn("MAX_INSPECT_STRING_PREFIX_BYTES + 1", self.inspect)
        self.assertIn(
            "family.SourceType ~= Enum.ContentSourceType.Uri",
            self.inspect,
        )
        self.assertIn("isInspectionFontUri(familyUri)", self.inspect)
        for uri_pattern in (
            "^rbxasset://.+$",
            "^rbxassetid://.+$",
            "^http://.+$",
            "^https://.+$",
        ):
            self.assertIn(uri_pattern, self.inspect)
        self.assertIn("not isInteger(value.Number)", self.inspect)
        self.assertIn("not isInteger(value.Offset)", self.inspect)
        self.assertIn("not isInteger(value.X.Offset)", self.inspect)
        self.assertIn("not isInteger(value.Y.Offset)", self.inspect)
        self.assertIn("not isInspectionName(name)", self.inspect)
        self.assertIn("#keypoints > MAX_INSPECT_SEQUENCE_KEYPOINTS", self.inspect)

    def test_attributes_tags_and_children_are_bounded_before_sort(self) -> None:
        attributes = self.inspect[
            self.inspect.index("local function inspectAttributes(") :
            self.inspect.index("local function inspectTags(")
        ]
        self.assertLess(
            attributes.index("#names > MAX_INSPECT_ATTRIBUTES_RAW"),
            attributes.index("table.sort(names)"),
        )
        self.assertIn(
            "not isInspectionAttributeName(name)",
            attributes,
        )
        self.assertIn("MAX_INSPECT_ATTRIBUTES_RETURNED", attributes)
        self.assertIn("inspection_attribute_value_unsupported", attributes)
        tags = self.inspect[
            self.inspect.index("local function inspectTags(") :
            self.inspect.index("local function isReusableInspectionSegment(")
        ]
        self.assertLess(
            tags.index("count > MAX_INSPECT_TAGS_RAW"),
            tags.index("table.sort(copied)"),
        )
        self.assertIn("MAX_INSPECT_TAGS_RETURNED", tags)
        self.assertIn("isInspectionName(tag)", tags)
        children = self.inspect[
            self.inspect.index("local function sortedInspectionChildren(") :
            self.inspect.index("local function assertInspectionFrontierBound(")
        ]
        self.assertIn("#children > MAX_TREE_CHILDREN_PER_INSTANCE", children)
        self.assertIn("left.original_index < right.original_index", children)
        self.assertIn("duplicateNames[child.name] == 1", children)
        self.assertIn("and appendPath(path, child.name) or {}", children)
        self.assertIn(
            "encodedBytes + #encoded + 1 > MAX_INSPECT_CHILD_OUTPUT_BYTES",
            children,
        )
        self.assertIn('reason = "output_bytes"', children)
        self.assertIn('reason = "child_limit"', children)
        boundary_reason = children.index('if reason ~= "complete"')
        boundary_addressable = children.index(
            "boundary.addressable = false"
        )
        boundary_path = children.index("boundary.path = {}")
        final_return = children.index("return returned, reason")
        self.assertLess(boundary_reason, boundary_addressable)
        self.assertLess(boundary_addressable, boundary_path)
        self.assertLess(boundary_path, final_return)

    def test_attribute_name_predicate_is_dedicated_and_engine_shaped(
        self,
    ) -> None:
        predicate = self.inspect[
            self.inspect.index(
                "local function isInspectionAttributeName("
            ) :
            self.inspect.index(
                "local function isInspectionIdentifier("
            )
        ]
        self.assertIn("#value >= 1", predicate)
        self.assertIn("#value <= MAX_PATH_SEGMENT_BYTES", predicate)
        self.assertIn(
            'string.find(value, "^[A-Za-z0-9._/%-]+$")',
            predicate,
        )
        self.assertNotIn("isInspectionName(", predicate)
        self.assertNotIn("RBX", predicate)
        tags = self.inspect[
            self.inspect.index("local function inspectTags(") :
            self.inspect.index(
                "local function isReusableInspectionSegment("
            )
        ]
        self.assertIn("isInspectionName(tag)", tags)
        children = self.inspect[
            self.inspect.index(
                "local function sortedInspectionChildren("
            ) :
            self.inspect.index(
                "local function inspectImmediateChildren("
            )
        ]
        self.assertIn("isInspectionName(name)", children)
        self.assertNotIn("isInspectionAttributeName(", tags)
        self.assertNotIn("isInspectionAttributeName(", children)

    def test_attribute_name_model_accepts_rbx_read_side(self) -> None:
        accepted = (
            "Health",
            "RBX_Internal",
            "RBX.foo-bar/baz_qux",
            "0",
            "a" * 100,
        )
        rejected = (
            "",
            "a" * 101,
            "has space",
            "colon:name",
            "back\\slash",
            "line\nbreak",
            "é",
            None,
        )
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(inspection_attribute_name_model(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(inspection_attribute_name_model(value))

    def test_child_summary_boundary_model(self) -> None:
        complete_unique = child_summary_model(
            names=["Alpha", "Beta"],
            returned_count=2,
            reason="complete",
        )
        self.assertEqual([True, True], [
            entry["addressable"] for entry in complete_unique
        ])
        self.assertEqual(
            ["Workspace", "Beta"],
            complete_unique[-1]["path"],
        )

        complete_duplicate = child_summary_model(
            names=["Alpha", "Alpha", "Beta"],
            returned_count=3,
            reason="complete",
        )
        self.assertEqual([False, False, True], [
            entry["addressable"] for entry in complete_duplicate
        ])
        self.assertEqual([], complete_duplicate[0]["path"])
        self.assertEqual(
            ["Workspace", "Beta"],
            complete_duplicate[-1]["path"],
        )

        truncated_unique = child_summary_model(
            names=["Alpha", "Beta", "Gamma"],
            returned_count=2,
            reason="output_bytes",
        )
        self.assertEqual([True, False], [
            entry["addressable"] for entry in truncated_unique
        ])
        self.assertEqual([], truncated_unique[-1]["path"])

        truncated_duplicate = child_summary_model(
            names=["Alpha", "Beta", "Beta"],
            returned_count=2,
            reason="child_limit",
        )
        self.assertEqual([True, False], [
            entry["addressable"] for entry in truncated_duplicate
        ])
        self.assertEqual([], truncated_duplicate[-1]["path"])

    def test_descendant_walk_is_manual_bounded_and_fenced(self) -> None:
        walk = self.inspect[
            self.inspect.index("local function assertInspectionFrontierBound(") :
            self.inspect.index("local function inspectInstance(")
        ]
        self.assertIn("local function newInspectionTraversal(", walk)
        self.assertIn("local function nextInspectionDescendant(", walk)
        self.assertNotIn("GetDescendants", walk)
        self.assertIn("MAX_TREE_RETAINED_CHILDREN", walk)
        self.assertIn("INSPECT_COOPERATIVE_YIELD_INTERVAL", walk)
        self.assertIn("task.wait()", walk)
        self.assertIn("fence()", walk)
        self.assertIn("peer.generation ~= generation", walk)
        self.assertIn("observed ~= target", walk)
        for reason in (
            "complete",
            "scan_limit",
            "time_limit",
            "depth_limit",
        ):
            self.assertIn(f'"{reason}"', walk)
        for forbidden in (
            "GetDescendants",
            "UniqueId",
            "GetDebugId",
            "ReflectionMetadata",
            "target.Source",
            "loadstring",
        ):
            self.assertNotIn(forbidden, self.inspect)

    def test_descendant_initial_reason_precedence_is_explicit(self) -> None:
        descendant = self.inspect[
            self.inspect.index("local function inspectDescendants(") :
            self.inspect.index("local function inspectInstance(")
        ]
        empty_branch = descendant.index("if #rootChildren == 0 then")
        time_branch = descendant.index(
            "if (os.clock() - startedAt) * 1_000 >= timeLimitMs then"
        )
        depth_branch = descendant.index("if maxDepth == 0 then")
        traversal = descendant.index(
            "local frames = newInspectionTraversal("
        )
        self.assertLess(empty_branch, time_branch)
        self.assertLess(time_branch, depth_branch)
        self.assertLess(depth_branch, traversal)
        self.assertIn(
            'return 0, true, "complete", {}',
            descendant[empty_branch:time_branch],
        )
        self.assertIn(
            'return 0, false, "time_limit", {}',
            descendant[time_branch:depth_branch],
        )
        self.assertIn(
            'return 0, false, "depth_limit", {}',
            descendant[depth_branch:traversal],
        )

    def test_descendant_initial_reason_model(self) -> None:
        self.assertEqual(
            (0, True, "complete"),
            descendant_initial_model(
                root_child_count=0,
                max_depth=0,
                elapsed_ms=10_000,
                time_limit_ms=100,
            ),
        )
        self.assertEqual(
            (0, False, "time_limit"),
            descendant_initial_model(
                root_child_count=1,
                max_depth=0,
                elapsed_ms=100,
                time_limit_ms=100,
            ),
        )
        self.assertEqual(
            (0, False, "depth_limit"),
            descendant_initial_model(
                root_child_count=1,
                max_depth=0,
                elapsed_ms=99,
                time_limit_ms=100,
            ),
        )
        self.assertIsNone(
            descendant_initial_model(
                root_child_count=1,
                max_depth=1,
                elapsed_ms=99,
                time_limit_ms=100,
            )
        )

    def test_result_has_exact_versions_identity_and_final_cap(self) -> None:
        operation = self.inspect[
            self.inspect.index("local function inspectInstance(") :
        ]
        for marker in (
            'newScriptResult("studio_inspect_instance", requestId)',
            'result.datamodel_type = "Edit"',
            "result.path = path",
            "result.name = target.Name",
            "result.class_name = target.ClassName",
            'INSPECT_SNAPSHOT_CONTRACT =\n\t'
            '"path-edit-generation-fenced-observational-v1"',
            '"instance-property-allowlist-v1"',
            '"instance-value-v1"',
            '"name-class-original-v1"',
            "result.properties_complete = propertiesComplete",
            "result.attributes_total = attributesTotal",
            "result.tags_total = tagsTotal",
            "result.children_total = #sortedChildren",
            "result.descendant_count_complete = descendantCountComplete",
            "result.descendant_class_counts = descendantClassCounts",
            "result.output_limit_bytes = MAX_INSPECT_RESULT_BYTES",
            "#encoded > MAX_INSPECT_RESULT_BYTES",
        ):
            self.assertIn(marker, self.source)
        self.assertLess(operation.index("local startedAt = os.clock()"), operation.index(
            "resolveExactPath(path, false)"
        ))
        self.assertGreaterEqual(operation.count("fence()"), 1)

    def test_renderer_exposes_only_the_durable_inspection_name(self) -> None:
        rendered = render_studio_plugin.render_durable(TOKEN, RUN_ID)
        capability_start = rendered.index("local CAPABILITIES = table.freeze({")
        capability_end = rendered.index(
            "local REQUEST_KEYS = table.freeze({", capability_start
        )
        capabilities = rendered[capability_start:capability_end]
        self.assertEqual(1, capabilities.count('"studio_inspect_instance"'))
        self.assertEqual(
            1,
            capabilities.count("studio_inspect_instance = true"),
        )
        self.assertNotIn('"inspect_instance"', capabilities)
        self.assertNotIn("active_studio", rendered)
        self.assertNotIn("default_studio", rendered)


if __name__ == "__main__":
    unittest.main()
