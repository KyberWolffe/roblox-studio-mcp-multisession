from __future__ import annotations

import asyncio
import copy
import json
import math
import unittest

from studio_mcp_v2.errors import (
    AuthenticationError,
    RemoteToolError,
    StaleGenerationError,
)

from .helpers import FakeStudio, make_service


INSPECT = "studio_inspect_instance"
INVALID_RESPONSE = (
    "^Targeted Studio returned an invalid instance inspection response$"
)


def safe_value(value_type: str, **updates):
    value = {
        "type": value_type,
        "boolean_value": False,
        "number_value": 0,
        "text": "",
        "numbers": [],
        "labels": [],
        "byte_length": 0,
        "truncated": False,
    }
    value.update(updates)
    return value


BASE_PART_SELECTORS = {
    "BasePart.Anchored",
    "BasePart.CanCollide",
    "BasePart.CanQuery",
    "BasePart.CanTouch",
    "BasePart.CastShadow",
    "BasePart.CFrame",
    "BasePart.CollisionGroup",
    "BasePart.Color",
    "BasePart.Locked",
    "BasePart.Massless",
    "BasePart.Material",
    "BasePart.MaterialVariant",
    "BasePart.Reflectance",
    "BasePart.Size",
    "BasePart.Transparency",
}
BASE_SCRIPT_SELECTORS = {
    "BaseScript.Enabled",
    "BaseScript.RunContext",
}
GUI_OBJECT_SELECTORS = {
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
}
LAYER_COLLECTOR_SELECTORS = {
    "LayerCollector.Enabled",
    "LayerCollector.ResetOnSpawn",
    "LayerCollector.ZIndexBehavior",
}
BOOLEAN_PROPERTY_SELECTORS = {
    "Instance.Archivable",
    "BasePart.Anchored",
    "BasePart.CanCollide",
    "BasePart.CanQuery",
    "BasePart.CanTouch",
    "BasePart.CastShadow",
    "BasePart.Locked",
    "BasePart.Massless",
    "BaseScript.Enabled",
    "GuiObject.Active",
    "GuiObject.ClipsDescendants",
    "GuiObject.Visible",
    "LayerCollector.Enabled",
    "LayerCollector.ResetOnSpawn",
}
NUMBER_PROPERTY_SELECTORS = {
    "BasePart.Reflectance",
    "BasePart.Transparency",
    "GuiObject.BackgroundTransparency",
    "GuiObject.BorderSizePixel",
    "GuiObject.LayoutOrder",
    "GuiObject.Rotation",
    "GuiObject.ZIndex",
}


def property_value(selector: str):
    if selector in BOOLEAN_PROPERTY_SELECTORS:
        return safe_value("boolean", boolean_value=True)
    if selector in NUMBER_PROPERTY_SELECTORS:
        return safe_value("number", number_value=0)
    if selector == "BasePart.CFrame":
        return safe_value(
            "cframe",
            numbers=[
                0,
                0,
                0,
                1,
                0,
                0,
                0,
                1,
                0,
                0,
                0,
                1,
            ],
        )
    if selector == "BasePart.CollisionGroup":
        return safe_value(
            "string", text="Default", byte_length=7
        )
    if selector == "BasePart.MaterialVariant":
        return safe_value("string")
    if selector in {
        "BasePart.Color",
        "GuiObject.BackgroundColor3",
        "GuiObject.BorderColor3",
    }:
        return safe_value("color3", numbers=[0.25, 0.5, 0.75])
    if selector == "BasePart.Material":
        return safe_value(
            "enum",
            number_value=256,
            labels=["Material", "Plastic"],
        )
    if selector == "BasePart.Size":
        return safe_value("vector3", numbers=[4, 1, 2])
    if selector == "BaseScript.RunContext":
        return safe_value(
            "enum",
            number_value=0,
            labels=["RunContext", "Legacy"],
        )
    if selector == "GuiObject.AnchorPoint":
        return safe_value("vector2", numbers=[0.5, 0.5])
    if selector in {"GuiObject.Position", "GuiObject.Size"}:
        return safe_value("udim2", numbers=[0, 0, 0, 0])
    if selector == "LayerCollector.ZIndexBehavior":
        return safe_value(
            "enum",
            number_value=0,
            labels=["ZIndexBehavior", "Global"],
        )
    raise AssertionError(f"missing property fixture: {selector}")


def property_entries(selectors):
    return [
        {"selector": selector, "value": property_value(selector)}
        for selector in sorted(selectors)
    ]


class DurableInspectResponseValidationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.registry, _catalog, _service = make_service()
        self.studio = await FakeStudio.create(
            self.registry,
            "Inspect response validation",
            {INSPECT},
        )

    @staticmethod
    def inspect_args():
        return {
            "path": ["Workspace", "ObservedPart"],
            "child_limit": 3,
            "descendant_max_depth": 2,
            "descendant_scan_limit": 20,
            "time_limit_ms": 1_000,
        }

    def valid_result(self, request):
        path = copy.deepcopy(request["args"]["path"])
        properties = property_entries(
            BASE_PART_SELECTORS | {"Instance.Archivable"}
        )
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": INSPECT,
            "studio_id": self.studio.studio_id,
            "client_instance_id": self.studio.client_instance_id,
            "document_epoch": self.studio.registration.document_epoch,
            "generation": self.studio.generation,
            "request_id": request["request_id"],
            "datamodel_type": "Edit",
            "path": path,
            "name": path[-1],
            "class_name": "Part",
            "snapshot_contract": (
                "path-edit-generation-fenced-observational-v1"
            ),
            "property_allowlist_version": (
                "instance-property-allowlist-v1"
            ),
            "value_encoding_version": "instance-value-v1",
            "sort_version": "name-class-original-v1",
            "child_limit": request["args"].get("child_limit", 50),
            "descendant_max_depth": request["args"].get(
                "descendant_max_depth", 64 - len(path)
            ),
            "descendant_scan_limit": request["args"].get(
                "descendant_scan_limit", 2_000
            ),
            "time_limit_ms": request["args"].get(
                "time_limit_ms", 3_000
            ),
            "properties": properties,
            "property_count": len(properties),
            "properties_complete": True,
            "attributes": [
                {
                    "name": "Health",
                    "value": safe_value(
                        "number", number_value=100
                    ),
                },
                {
                    "name": "Label",
                    "value": safe_value(
                        "string",
                        text="sample",
                        byte_length=6,
                    ),
                },
            ],
            "attributes_total": 2,
            "attributes_returned": 2,
            "attributes_truncated": False,
            "tags": ["Inspectable", "Runtime"],
            "tags_total": 2,
            "tags_returned": 2,
            "tags_truncated": False,
            "children": [
                {
                    "name": "Attachment",
                    "class_name": "Attachment",
                    "addressable": True,
                    "path": path + ["Attachment"],
                },
                {
                    "name": "Twin",
                    "class_name": "Folder",
                    "addressable": False,
                    "path": [],
                },
                {
                    "name": "Twin",
                    "class_name": "Part",
                    "addressable": False,
                    "path": [],
                },
            ],
            "children_total": 3,
            "children_returned": 3,
            "children_truncated": False,
            "children_truncation_reason": "complete",
            "descendant_count": 4,
            "descendant_count_complete": True,
            "descendant_truncation_reason": "complete",
            "descendant_class_counts": [
                {"class_name": "Attachment", "count": 1},
                {"class_name": "Folder", "count": 1},
                {"class_name": "Part", "count": 2},
            ],
            "output_limit_bytes": 500_000,
        }

    async def invoke(self, arguments=None, *, request_id=None):
        task = asyncio.create_task(
            self.studio.session.invoke(
                INSPECT,
                copy.deepcopy(arguments or self.inspect_args()),
                1_000,
                request_id=request_id,
            )
        )
        request = await self.studio.next_request()
        self.assertEqual(INSPECT, request["operation"])
        return task, request

    async def assert_rejected(
        self, mutate, *, arguments=None
    ) -> None:
        before = (
            self.studio.session.mode,
            self.studio.session.last_confirmed_mode,
            self.studio.session.uncertainty_state,
            self.studio.session.play_bridge_uncertain,
            copy.deepcopy(self.studio.session.uncertain_requests),
        )
        task, request = await self.invoke(arguments)
        candidate = self.valid_result(request)
        mutate(candidate)
        self.assertTrue(self.studio.respond(request, candidate))
        with self.assertRaisesRegex(RemoteToolError, INVALID_RESPONSE):
            await task
        self.assertEqual(
            before,
            (
                self.studio.session.mode,
                self.studio.session.last_confirmed_mode,
                self.studio.session.uncertainty_state,
                self.studio.session.play_bridge_uncertain,
                self.studio.session.uncertain_requests,
            ),
        )
        self.assertNotIn(
            request["request_id"], self.studio.session.pending
        )

    async def test_valid_direct_result_is_exact_and_state_neutral(self):
        task, request = await self.invoke(request_id="inspect-direct")
        result = self.valid_result(request)

        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)
        self.assertEqual("edit", self.studio.session.mode)
        self.assertEqual("edit", self.studio.session.last_confirmed_mode)
        self.assertIsNone(self.studio.session.uncertainty_state)

    async def test_request_defaults_are_authenticated_in_response(self):
        task, request = await self.invoke(
            {"path": ["Workspace", "ObservedPart"]}
        )
        result = self.valid_result(request)

        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)
        self.assertEqual(50, result["child_limit"])
        self.assertEqual(62, result["descendant_max_depth"])
        self.assertEqual(2_000, result["descendant_scan_limit"])
        self.assertEqual(3_000, result["time_limit_ms"])

    async def test_malformed_pending_arguments_cannot_bless_success(
        self,
    ):
        cases = []
        extra = self.inspect_args()
        extra["property"] = "Source"
        cases.append(extra)
        boolean_limit = self.inspect_args()
        boolean_limit["child_limit"] = True
        cases.append(boolean_limit)
        depth_overflow = self.inspect_args()
        depth_overflow["descendant_max_depth"] = 63
        cases.append(depth_overflow)
        zero_scan = self.inspect_args()
        zero_scan["descendant_scan_limit"] = 0
        cases.append(zero_scan)
        short_time = self.inspect_args()
        short_time["time_limit_ms"] = 99
        cases.append(short_time)
        controlled_path = self.inspect_args()
        controlled_path["path"] = ["Workspace", "Bad\nName"]
        cases.append(controlled_path)

        for index, arguments in enumerate(cases):
            with self.subTest(case=index):
                task, request = await self.invoke(arguments)
                result = self.valid_result(request)
                self.assertTrue(self.studio.respond(request, result))
                with self.assertRaisesRegex(
                    RemoteToolError, INVALID_RESPONSE
                ):
                    await task

    async def test_valid_result_completes_identically_as_job(self):
        job = self.studio.session.start_job(
            "studio_inspect_instance_v2",
            INSPECT,
            self.inspect_args(),
            1_000,
        )
        request = await self.studio.next_request()
        result = self.valid_result(request)

        self.assertTrue(self.studio.respond(request, result))
        await job.task
        self.assertEqual("completed", job.status)
        self.assertEqual(result, job.result)
        self.assertIsNone(job.error)
        self.assertEqual("edit", self.studio.session.mode)

    async def test_invalid_job_response_fails_without_result(self):
        job = self.studio.session.start_job(
            "studio_inspect_instance_v2",
            INSPECT,
            self.inspect_args(),
            1_000,
        )
        request = await self.studio.next_request()
        result = self.valid_result(request)
        result["properties"] = []
        result["property_count"] = 0

        self.assertTrue(self.studio.respond(request, result))
        await job.task
        self.assertEqual("failed", job.status)
        self.assertIsNone(job.result)
        self.assertEqual("studio_tool_error", job.error["code"])
        self.assertRegex(job.error["message"], INVALID_RESPONSE)
        self.assertEqual("edit", self.studio.session.mode)

        descendant_job = self.studio.session.start_job(
            "studio_inspect_instance_v2",
            INSPECT,
            self.inspect_args(),
            1_000,
        )
        request = await self.studio.next_request()
        result = self.valid_result(request)
        result["children"] = []
        result["children_total"] = 0
        result["children_returned"] = 0
        result["children_truncated"] = False
        result["children_truncation_reason"] = "complete"

        self.assertTrue(self.studio.respond(request, result))
        await descendant_job.task
        self.assertEqual("failed", descendant_job.status)
        self.assertIsNone(descendant_job.result)
        self.assertEqual(
            "studio_tool_error", descendant_job.error["code"]
        )
        self.assertRegex(
            descendant_job.error["message"], INVALID_RESPONSE
        )

        enum_job = self.studio.session.start_job(
            "studio_inspect_instance_v2",
            INSPECT,
            self.inspect_args(),
            1_000,
        )
        request = await self.studio.next_request()
        result = self.valid_result(request)
        material = next(
            item
            for item in result["properties"]
            if item["selector"] == "BasePart.Material"
        )
        material["value"]["labels"][0] = "RunContext"

        self.assertTrue(self.studio.respond(request, result))
        await enum_job.task
        self.assertEqual("failed", enum_job.status)
        self.assertIsNone(enum_job.result)
        self.assertEqual(
            "studio_tool_error", enum_job.error["code"]
        )
        self.assertRegex(enum_job.error["message"], INVALID_RESPONSE)

    async def test_identity_generation_request_and_constants_fail_closed(
        self,
    ):
        other = await FakeStudio.create(
            self.registry, "Other inspect session", {INSPECT}
        )
        mutations = (
            ("adapter", "other-adapter"),
            ("v", True),
            ("operation", "inspect_instance"),
            ("studio_id", other.studio_id),
            ("client_instance_id", other.client_instance_id),
            (
                "document_epoch",
                other.registration.document_epoch,
            ),
            ("generation", self.studio.generation + 1),
            ("request_id", "other-request"),
            ("datamodel_type", "Server"),
            ("snapshot_contract", "unfenced"),
            ("property_allowlist_version", "future"),
            ("value_encoding_version", "future"),
            ("sort_version", "name-only"),
            ("output_limit_bytes", 500_001),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                await self.assert_rejected(
                    lambda candidate,
                    field=field,
                    replacement=replacement: candidate.__setitem__(
                        field, replacement
                    )
                )
        self.assertTrue(other.transport._queue.empty())

    async def test_missing_extra_and_request_echo_fail_closed(self):
        await self.assert_rejected(
            lambda candidate: candidate.pop("properties")
        )
        await self.assert_rejected(
            lambda candidate: candidate.__setitem__(
                "source", "Workspace.ObservedPart"
            )
        )
        for field in (
            "path",
            "child_limit",
            "descendant_max_depth",
            "descendant_scan_limit",
            "time_limit_ms",
        ):
            with self.subTest(field=field):
                def mutate(candidate, field=field):
                    if field == "path":
                        candidate[field] = ["Workspace", "Other"]
                        candidate["name"] = "Other"
                    else:
                        candidate[field] += 1

                await self.assert_rejected(mutate)

    async def test_property_allowlist_order_counts_and_completeness(self):
        await self.assert_rejected(
            lambda candidate: candidate["properties"][0].__setitem__(
                "selector", "BasePart.Unknown"
            )
        )
        await self.assert_rejected(
            lambda candidate: candidate["properties"].reverse()
        )
        await self.assert_rejected(
            lambda candidate: candidate.__setitem__(
                "property_count", candidate["property_count"] + 1
            )
        )
        await self.assert_rejected(
            lambda candidate: candidate.__setitem__(
                "properties_complete", False
            )
        )

        def omit_archivable(candidate):
            candidate["properties"] = candidate["properties"][:-1]
            candidate["property_count"] -= 1

        await self.assert_rejected(omit_archivable)

        def empty_properties(candidate):
            candidate["properties"] = []
            candidate["property_count"] = 0

        await self.assert_rejected(empty_properties)

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["properties"][0]["value"] = safe_value("unavailable")
        result["properties_complete"] = False
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["properties"][-1]["value"] = safe_value(
            "unsupported"
        )
        result["properties_complete"] = False
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        await self.assert_rejected(
            lambda candidate: candidate["attributes"][0].__setitem__(
                "value", safe_value("unsupported")
            )
        )

    async def test_property_selector_value_type_mapping_fails_closed(
        self,
    ):
        for replacement in (
            safe_value(
                "string", text="true", byte_length=4
            ),
            safe_value("nil"),
            safe_value("enum", number_value=1, labels=["X", "Y"]),
        ):
            with self.subTest(value_type=replacement["type"]):
                await self.assert_rejected(
                    lambda candidate,
                    replacement=replacement: candidate[
                        "properties"
                    ][0].__setitem__(
                        "value", copy.deepcopy(replacement)
                    )
                )

    async def test_property_groups_are_complete_or_absent(self):
        groups = (
            BASE_PART_SELECTORS,
            BASE_SCRIPT_SELECTORS,
            GUI_OBJECT_SELECTORS,
            LAYER_COLLECTOR_SELECTORS,
        )

        for group in groups:
            with self.subTest(
                partial_selector=sorted(group)[0]
            ):
                def partial_group(candidate, group=group):
                    selectors = {
                        "Instance.Archivable",
                        sorted(group)[0],
                    }
                    candidate["properties"] = property_entries(
                        selectors
                    )
                    candidate["property_count"] = len(
                        candidate["properties"]
                    )

                await self.assert_rejected(partial_group)

        def two_complete_groups(candidate):
            selectors = (
                BASE_PART_SELECTORS
                | BASE_SCRIPT_SELECTORS
                | {"Instance.Archivable"}
            )
            candidate["properties"] = property_entries(selectors)
            candidate["property_count"] = len(
                candidate["properties"]
            )

        await self.assert_rejected(two_complete_groups)

        for selectors, class_name in (
            ({"Instance.Archivable"}, "Folder"),
            (
                BASE_PART_SELECTORS | {"Instance.Archivable"},
                "Part",
            ),
            (
                BASE_SCRIPT_SELECTORS | {"Instance.Archivable"},
                "Script",
            ),
            (
                GUI_OBJECT_SELECTORS | {"Instance.Archivable"},
                "Frame",
            ),
            (
                LAYER_COLLECTOR_SELECTORS
                | {"Instance.Archivable"},
                "ScreenGui",
            ),
        ):
            with self.subTest(property_count=len(selectors)):
                task, request = await self.invoke()
                result = self.valid_result(request)
                result["properties"] = property_entries(selectors)
                result["property_count"] = len(result["properties"])
                result["class_name"] = class_name
                self.assertTrue(self.studio.respond(request, result))
                self.assertEqual(result, await task)

    async def test_enum_property_families_are_selector_bound(self):
        cases = (
            (
                BASE_PART_SELECTORS,
                "BasePart.Material",
                "Material",
                "RunContext",
            ),
            (
                BASE_SCRIPT_SELECTORS,
                "BaseScript.RunContext",
                "RunContext",
                "ZIndexBehavior",
            ),
            (
                LAYER_COLLECTOR_SELECTORS,
                "LayerCollector.ZIndexBehavior",
                "ZIndexBehavior",
                "Material",
            ),
        )
        for group, selector, expected, wrong in cases:
            with self.subTest(selector=selector):
                def wrong_family(
                    candidate,
                    group=group,
                    selector=selector,
                    wrong=wrong,
                ):
                    candidate["properties"] = property_entries(
                        group | {"Instance.Archivable"}
                    )
                    candidate["property_count"] = len(
                        candidate["properties"]
                    )
                    entry = next(
                        item
                        for item in candidate["properties"]
                        if item["selector"] == selector
                    )
                    entry["value"]["labels"][0] = wrong

                await self.assert_rejected(wrong_family)

                task, request = await self.invoke()
                result = self.valid_result(request)
                result["properties"] = property_entries(
                    group | {"Instance.Archivable"}
                )
                result["property_count"] = len(result["properties"])
                enum_entry = next(
                    item
                    for item in result["properties"]
                    if item["selector"] == selector
                )
                self.assertEqual(
                    expected, enum_entry["value"]["labels"][0]
                )
                self.assertTrue(self.studio.respond(request, result))
                self.assertEqual(result, await task)

    async def test_string_prefix_boundaries_match_luau(self):
        valid = (
            safe_value(
                "string",
                text="é" * 512,
                byte_length=1_024,
            ),
            safe_value(
                "string",
                text="x" * 1_021,
                byte_length=1_025,
                truncated=True,
            ),
            safe_value(
                "string",
                text="x" * 1_024,
                byte_length=262_144,
                truncated=True,
            ),
        )
        invalid = (
            safe_value(
                "string",
                text="short",
                byte_length=100,
                truncated=True,
            ),
            safe_value(
                "string",
                text="x" * 1_020,
                byte_length=1_025,
                truncated=True,
            ),
            safe_value(
                "string",
                text="x" * 1_024,
                byte_length=1_024,
                truncated=True,
            ),
        )
        for value in valid:
            self.assertTrue(
                self.studio.session._valid_inspection_value(
                    value, property_value=True
                )
            )
        for value in invalid:
            self.assertFalse(
                self.studio.session._valid_inspection_value(
                    value, property_value=True
                )
            )

            def malformed_prefix(candidate, value=value):
                entry = next(
                    item
                    for item in candidate["properties"]
                    if item["selector"]
                    == "BasePart.CollisionGroup"
                )
                entry["value"] = copy.deepcopy(value)

            await self.assert_rejected(malformed_prefix)

        task, request = await self.invoke()
        result = self.valid_result(request)
        collision_group = next(
            item
            for item in result["properties"]
            if item["selector"] == "BasePart.CollisionGroup"
        )
        collision_group["value"] = copy.deepcopy(valid[-1])
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

    async def test_attribute_and_tag_bounds_counts_and_order_fail_closed(
        self,
    ):
        mutations = (
            lambda c: c["attributes"].reverse(),
            lambda c: c["attributes"][0].__setitem__(
                "name", "Bad\nName"
            ),
            lambda c: c["attributes"][0].__setitem__(
                "name", "Bad Name"
            ),
            lambda c: c["attributes"][0].__setitem__(
                "name", "Café"
            ),
            lambda c: c["attributes"][0].__setitem__(
                "name", "Bad:Name"
            ),
            lambda c: c["attributes"][0].__setitem__(
                "value",
                safe_value(
                    "enum",
                    number_value=256,
                    labels=["Material", "Plastic"],
                ),
            ),
            lambda c: c.__setitem__("attributes_total", 3),
            lambda c: c.__setitem__("attributes_returned", True),
            lambda c: c.__setitem__("attributes_truncated", True),
            lambda c: c["tags"].reverse(),
            lambda c: c["tags"].__setitem__(1, c["tags"][0]),
            lambda c: c["tags"].__setitem__(0, "Bad\u0000Tag"),
            lambda c: c.__setitem__("tags_total", 129),
            lambda c: c.__setitem__("tags_truncated", True),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                await self.assert_rejected(mutation)

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["attributes"][0]["name"] = "Core.Attr-1/value_2"
        result["attributes"][1]["name"] = "RBX.Core-Attr/value_1"
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

    async def test_child_order_addressability_and_truncation_coherence(
        self,
    ):
        await self.assert_rejected(
            lambda candidate: candidate["children"].reverse()
        )

        def duplicate_marked_addressable(candidate):
            candidate["children"][1]["addressable"] = True
            candidate["children"][1]["path"] = (
                candidate["path"] + ["Twin"]
            )

        await self.assert_rejected(duplicate_marked_addressable)

        def unique_marked_unaddressable(candidate):
            candidate["children"][0]["addressable"] = False
            candidate["children"][0]["path"] = []

        await self.assert_rejected(unique_marked_unaddressable)

        def truncated_final_claims_addressable(candidate):
            candidate["children"] = candidate["children"][:1]
            candidate["children_total"] = 2
            candidate["children_returned"] = 1
            candidate["children_truncated"] = True
            candidate["children_truncation_reason"] = "output_bytes"

        await self.assert_rejected(
            truncated_final_claims_addressable
        )

        def output_bytes_returned_nothing(candidate):
            candidate["children"] = []
            candidate["children_total"] = 1
            candidate["children_returned"] = 0
            candidate["children_truncated"] = True
            candidate["children_truncation_reason"] = "output_bytes"

        await self.assert_rejected(output_bytes_returned_nothing)
        await self.assert_rejected(
            lambda candidate: candidate["children"][0].__setitem__(
                "path", ["Workspace", "Elsewhere", "Attachment"]
            )
        )
        await self.assert_rejected(
            lambda candidate: candidate["children"][1].__setitem__(
                "path", ["Workspace", "ObservedPart", "Twin"]
            )
        )
        await self.assert_rejected(
            lambda candidate: candidate.__setitem__(
                "children_returned", 2
            )
        )
        await self.assert_rejected(
            lambda candidate: candidate.__setitem__(
                "children_truncation_reason", []
            )
        )

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["children"] = result["children"][:2]
        result["children_returned"] = 2
        result["children_total"] = 4
        result["children_truncated"] = True
        result["children_truncation_reason"] = "output_bytes"
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["children"] = result["children"][:1]
        result["children"][0]["addressable"] = False
        result["children"][0]["path"] = []
        result["children_total"] = 2
        result["children_returned"] = 1
        result["children_truncated"] = True
        result["children_truncation_reason"] = "output_bytes"
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["children_total"] = 4
        result["children_truncated"] = True
        result["children_truncation_reason"] = "child_limit"
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        depth_limit_args = self.inspect_args()
        depth_limit_args["path"] = ["Node"] * 64
        depth_limit_args["descendant_max_depth"] = 0
        task, request = await self.invoke(depth_limit_args)
        result = self.valid_result(request)
        for child in result["children"]:
            child["addressable"] = False
            child["path"] = []
        result["descendant_count"] = 0
        result["descendant_count_complete"] = False
        result["descendant_truncation_reason"] = "depth_limit"
        result["descendant_class_counts"] = []
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

    async def test_descendant_reason_counts_and_class_order_fail_closed(
        self,
    ):
        mutations = (
            lambda c: c.__setitem__("descendant_count", 21),
            lambda c: c.__setitem__(
                "descendant_count_complete", False
            ),
            lambda c: c.__setitem__(
                "descendant_truncation_reason", "time_budget"
            ),
            lambda c: c.__setitem__(
                "descendant_truncation_reason", []
            ),
            lambda c: c["descendant_class_counts"].reverse(),
            lambda c: c["descendant_class_counts"][0].__setitem__(
                "count", 2
            ),
            lambda c: c["descendant_class_counts"][0].__setitem__(
                "class_name", "Bad.Class"
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                await self.assert_rejected(mutation)

        task, request = await self.invoke()
        result = self.valid_result(request)
        result["descendant_count"] = 20
        result["descendant_count_complete"] = False
        result["descendant_truncation_reason"] = "scan_limit"
        result["descendant_class_counts"] = [
            {"class_name": "Part", "count": 20}
        ]
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

    async def test_descendants_cohere_with_immediate_children(self):
        def clear_children(candidate):
            candidate["children"] = []
            candidate["children_total"] = 0
            candidate["children_returned"] = 0
            candidate["children_truncated"] = False
            candidate["children_truncation_reason"] = "complete"

        await self.assert_rejected(clear_children)

        def empty_but_timed_out(candidate):
            clear_children(candidate)
            candidate["descendant_count"] = 0
            candidate["descendant_count_complete"] = False
            candidate["descendant_truncation_reason"] = "time_limit"
            candidate["descendant_class_counts"] = []

        await self.assert_rejected(empty_but_timed_out)

        task, request = await self.invoke()
        result = self.valid_result(request)
        clear_children(result)
        result["descendant_count"] = 0
        result["descendant_count_complete"] = True
        result["descendant_truncation_reason"] = "complete"
        result["descendant_class_counts"] = []
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        zero_depth_args = self.inspect_args()
        zero_depth_args["descendant_max_depth"] = 0
        await self.assert_rejected(
            lambda _candidate: None,
            arguments=zero_depth_args,
        )

        def zero_depth_claims_complete(candidate):
            candidate["descendant_count"] = 0
            candidate["descendant_count_complete"] = True
            candidate["descendant_truncation_reason"] = "complete"
            candidate["descendant_class_counts"] = []

        await self.assert_rejected(
            zero_depth_claims_complete,
            arguments=zero_depth_args,
        )

        for reason in ("depth_limit", "time_limit"):
            with self.subTest(zero_depth_reason=reason):
                task, request = await self.invoke(zero_depth_args)
                result = self.valid_result(request)
                result["descendant_count"] = 0
                result["descendant_count_complete"] = False
                result["descendant_truncation_reason"] = reason
                result["descendant_class_counts"] = []
                self.assertTrue(self.studio.respond(request, result))
                self.assertEqual(result, await task)

        one_depth_args = self.inspect_args()
        one_depth_args["descendant_max_depth"] = 1

        def fewer_than_immediate(candidate, reason):
            candidate["descendant_count"] = 2
            candidate["descendant_count_complete"] = (
                reason == "complete"
            )
            candidate["descendant_truncation_reason"] = reason
            candidate["descendant_class_counts"] = [
                {"class_name": "Part", "count": 2}
            ]

        for reason in ("complete", "depth_limit"):
            with self.subTest(undersized_reason=reason):
                await self.assert_rejected(
                    lambda candidate,
                    reason=reason: fewer_than_immediate(
                        candidate, reason
                    ),
                    arguments=one_depth_args,
                )

        for reason in ("complete", "depth_limit"):
            with self.subTest(exact_reason=reason):
                task, request = await self.invoke(one_depth_args)
                result = self.valid_result(request)
                result["descendant_count"] = 3
                result["descendant_count_complete"] = (
                    reason == "complete"
                )
                result["descendant_truncation_reason"] = reason
                result["descendant_class_counts"] = [
                    {"class_name": "Part", "count": 3}
                ]
                self.assertTrue(self.studio.respond(request, result))
                self.assertEqual(result, await task)

        def time_exceeds_immediate(candidate):
            candidate["descendant_count"] = 4
            candidate["descendant_count_complete"] = False
            candidate["descendant_truncation_reason"] = "time_limit"
            candidate["descendant_class_counts"] = [
                {"class_name": "Part", "count": 4}
            ]

        await self.assert_rejected(
            time_exceeds_immediate,
            arguments=one_depth_args,
        )

        task, request = await self.invoke(one_depth_args)
        result = self.valid_result(request)
        result["descendant_count"] = 1
        result["descendant_count_complete"] = False
        result["descendant_truncation_reason"] = "time_limit"
        result["descendant_class_counts"] = [
            {"class_name": "Part", "count": 1}
        ]
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        scan_args = copy.deepcopy(one_depth_args)
        scan_args["descendant_scan_limit"] = 2
        task, request = await self.invoke(scan_args)
        result = self.valid_result(request)
        result["descendant_count"] = 2
        result["descendant_count_complete"] = False
        result["descendant_truncation_reason"] = "scan_limit"
        result["descendant_class_counts"] = [
            {"class_name": "Part", "count": 2}
        ]
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

        scan_at_total_args = copy.deepcopy(one_depth_args)
        scan_at_total_args["descendant_scan_limit"] = 3

        def scan_claims_full_immediate_set(candidate):
            candidate["descendant_count"] = 3
            candidate["descendant_count_complete"] = False
            candidate["descendant_truncation_reason"] = "scan_limit"
            candidate["descendant_class_counts"] = [
                {"class_name": "Part", "count": 3}
            ]

        await self.assert_rejected(
            scan_claims_full_immediate_set,
            arguments=scan_at_total_args,
        )

    async def test_all_safe_value_kinds_accept_locked_encodings(self):
        valid_values = (
            safe_value("nil"),
            safe_value("unavailable"),
            safe_value("unsupported"),
            safe_value("boolean", boolean_value=True),
            safe_value("number", number_value=-1.25),
            safe_value(
                "string",
                text="é" * 512,
                byte_length=1_024,
            ),
            safe_value(
                "string",
                text="x" * 1_024,
                byte_length=1_025,
                truncated=True,
            ),
            safe_value(
                "enum",
                number_value=1,
                labels=["Material", "Plastic"],
            ),
            safe_value("vector2", numbers=[1, 2]),
            safe_value("vector3", numbers=[1, 2, 3]),
            safe_value("color3", numbers=[0, 0.5, 1]),
            safe_value("cframe", numbers=list(range(12))),
            safe_value("udim", numbers=[0.5, 2]),
            safe_value("udim2", numbers=[0.5, 2, 1, -3]),
            safe_value("rect", numbers=[0, 1, 2, 3]),
            safe_value(
                "brick_color",
                number_value=21,
                numbers=[1, 0, 0],
                labels=["Bright red"],
            ),
            safe_value("number_range", numbers=[-1, 2]),
            safe_value(
                "number_sequence",
                numbers=[0, 1, 0, 0.5, 2, 0.25, 1, 3, 0],
            ),
            safe_value(
                "color_sequence",
                numbers=[0, 1, 0, 0, 1, 0, 0.5, 1],
            ),
            safe_value(
                "font",
                boolean_value=True,
                number_value=700,
                labels=[
                    (
                        "rbxasset://fonts/families/"
                        "SourceSansPro.json"
                    ),
                    "Bold",
                    "Normal",
                ],
            ),
        )
        for value in valid_values:
            with self.subTest(value_type=value["type"]):
                self.assertTrue(
                    self.studio.session._valid_inspection_value(
                        value, property_value=True
                    )
                )

    async def test_malformed_value_shapes_nan_and_ranges_fail_closed(self):
        invalid_values = []
        invalid_values.append(
            safe_value("number", number_value=math.nan)
        )
        invalid_values.append(
            safe_value("number", number_value=True)
        )
        invalid_values.append(
            safe_value("number", number_value=10**10_000)
        )
        invalid_values.append(
            safe_value("string", text="x", byte_length=2)
        )
        invalid_values.append(
            safe_value(
                "string",
                text="x" * 1_025,
                byte_length=1_025,
            )
        )
        invalid_values.append(
            safe_value(
                "enum",
                number_value=1.5,
                labels=["Enum.Material", "Plastic"],
            )
        )
        invalid_values.append(
            safe_value("color3", numbers=[0, -0.1, 1])
        )
        invalid_values.append(
            safe_value("udim", numbers=[0.5, 2.5])
        )
        invalid_values.append(
            safe_value("rect", numbers=[2, 0, 1, 1])
        )
        invalid_values.append(
            safe_value("number_range", numbers=[2, 1])
        )
        invalid_values.append(
            safe_value(
                "number_sequence",
                numbers=[0, 1, 0, 0.5, 2, -1, 1, 3, 0],
            )
        )
        invalid_values.append(
            safe_value(
                "color_sequence",
                numbers=[0, 1, 0, 0, 0, 0, 0.5, 1],
            )
        )
        invalid_values.append(
            safe_value(
                "font",
                boolean_value=False,
                number_value=400,
                labels=["object-backed", "Regular", "Normal"],
            )
        )
        inactive_contaminated = safe_value(
            "vector3", numbers=[1, 2, 3]
        )
        inactive_contaminated["text"] = "smuggled"
        invalid_values.append(inactive_contaminated)

        for value in invalid_values:
            with self.subTest(value_type=value["type"]):
                self.assertFalse(
                    self.studio.session._valid_inspection_value(
                        value, property_value=True
                    )
                )

        await self.assert_rejected(
            lambda candidate: candidate["properties"][0].__setitem__(
                "value",
                safe_value("number", number_value=10**10_000),
            )
        )

    async def test_ieee754_integer_exactness_is_enforced(self):
        unrepresentable = 2**53 + 1
        representable = 2**60
        self.assertFalse(
            self.studio.session._valid_inspection_value(
                safe_value(
                    "number", number_value=unrepresentable
                ),
                property_value=True,
            )
        )
        self.assertFalse(
            self.studio.session._valid_inspection_value(
                safe_value(
                    "vector3",
                    numbers=[1, unrepresentable, 3],
                ),
                property_value=True,
            )
        )
        self.assertTrue(
            self.studio.session._valid_inspection_value(
                safe_value(
                    "number", number_value=representable
                ),
                property_value=True,
            )
        )
        self.assertTrue(
            self.studio.session._valid_inspection_value(
                safe_value(
                    "vector3",
                    numbers=[representable, 2**53, 0],
                ),
                property_value=True,
            )
        )

        def corrupt_scalar(candidate):
            entry = next(
                item
                for item in candidate["properties"]
                if item["selector"] == "BasePart.Reflectance"
            )
            entry["value"]["number_value"] = unrepresentable

        await self.assert_rejected(corrupt_scalar)

        def corrupt_list(candidate):
            entry = next(
                item
                for item in candidate["properties"]
                if item["selector"] == "BasePart.Size"
            )
            entry["value"]["numbers"][1] = unrepresentable

        await self.assert_rejected(corrupt_list)

        task, request = await self.invoke()
        result = self.valid_result(request)
        reflectance = next(
            item
            for item in result["properties"]
            if item["selector"] == "BasePart.Reflectance"
        )
        size = next(
            item
            for item in result["properties"]
            if item["selector"] == "BasePart.Size"
        )
        reflectance["value"]["number_value"] = representable
        size["value"]["numbers"] = [representable, 2**53, 0]
        self.assertTrue(self.studio.respond(request, result))
        self.assertEqual(result, await task)

    async def test_oversized_canonical_result_fails_closed(self):
        path = [
            f"{index:02d}" + "x" * 98
            for index in range(63)
        ]
        arguments = {
            "path": path,
            "child_limit": 200,
            "descendant_max_depth": 1,
            "descendant_scan_limit": 100,
            "time_limit_ms": 1_000,
        }
        task, request = await self.invoke(arguments)
        result = self.valid_result(request)
        result["children"] = [
            {
                "name": f"{index:02d}" + "y" * 98,
                "class_name": "Folder",
                "addressable": True,
                "path": path + [f"{index:02d}" + "y" * 98],
            }
            for index in range(80)
        ]
        result["children"][-1]["addressable"] = False
        result["children"][-1]["path"] = []
        result["children_total"] = 81
        result["children_returned"] = 80
        result["children_truncated"] = True
        result["children_truncation_reason"] = "output_bytes"
        result["descendant_count"] = 81
        result["descendant_class_counts"] = [
            {"class_name": "Folder", "count": 81}
        ]
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertGreater(len(encoded), 500_000)

        self.assertTrue(self.studio.respond(request, result))
        with self.assertRaisesRegex(RemoteToolError, INVALID_RESPONSE):
            await task

    async def test_reconnect_fences_completed_old_generation_result(self):
        old_generation = self.studio.generation
        task, request = await self.invoke(request_id="inspect-old-gen")
        result = self.valid_result(request)
        self.assertTrue(self.studio.respond(request, result))

        self.assertTrue(self.studio.disconnect())
        old_connection = await self.studio.reconnect()
        self.assertEqual(old_generation + 1, self.studio.generation)
        with self.assertRaises(StaleGenerationError):
            await task

        with self.assertRaises(AuthenticationError):
            self.registry.receive_response(
                old_connection.studio_id,
                old_connection.generation,
                old_connection.resume_token,
                request["request_id"],
                success=True,
                result=result,
            )
