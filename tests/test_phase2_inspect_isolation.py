from __future__ import annotations

import asyncio
import copy
import json
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import (
    AuthenticationError,
    RemoteToolError,
    StaleGenerationError,
)
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.service import ProxyService

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)
INSPECT_REMOTE = "studio_inspect_instance"
INSPECT_PUBLIC = INSPECT_REMOTE + "_v2"
INVALID_RESPONSE = (
    "^Targeted Studio returned an invalid instance inspection response$"
)


def boolean_value(value: bool):
    return {
        "type": "boolean",
        "boolean_value": value,
        "number_value": 0,
        "text": "",
        "numbers": [],
        "labels": [],
        "byte_length": 0,
        "truncated": False,
    }


class Phase2InspectIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.assertIn(INSPECT_REMOTE, self.catalog.remote_names)
        self.service = ProxyService(self.registry, self.catalog)
        self.a = await FakeStudio.create(
            self.registry,
            "Inspect A",
            self.catalog.remote_names,
        )
        self.b = await FakeStudio.create(
            self.registry,
            "Inspect B",
            self.catalog.remote_names,
        )

    async def call(
        self,
        studio: FakeStudio,
        arguments,
        *,
        request_id=None,
    ):
        return await self.service.call_tool(
            ALLOW_ALL,
            INSPECT_PUBLIC,
            {
                "studio_id": studio.studio_id,
                **copy.deepcopy(arguments),
            },
            client_request_id=request_id,
        )

    @staticmethod
    def valid_empty_result(studio: FakeStudio, request):
        args = request["args"]
        path = copy.deepcopy(args["path"])
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": INSPECT_REMOTE,
            "studio_id": studio.studio_id,
            "client_instance_id": studio.client_instance_id,
            "document_epoch": studio.registration.document_epoch,
            "generation": studio.generation,
            "request_id": request["request_id"],
            "datamodel_type": "Edit",
            "path": path,
            "name": path[-1],
            "class_name": "Folder",
            "snapshot_contract": (
                "path-edit-generation-fenced-observational-v1"
            ),
            "property_allowlist_version": (
                "instance-property-allowlist-v1"
            ),
            "value_encoding_version": "instance-value-v1",
            "sort_version": "name-class-original-v1",
            "child_limit": args.get("child_limit", 50),
            "descendant_max_depth": args.get(
                "descendant_max_depth",
                64 - len(path),
            ),
            "descendant_scan_limit": args.get(
                "descendant_scan_limit",
                2_000,
            ),
            "time_limit_ms": args.get("time_limit_ms", 3_000),
            "properties": [
                {
                    "selector": "Instance.Archivable",
                    "value": boolean_value(True),
                }
            ],
            "property_count": 1,
            "properties_complete": True,
            "attributes": [],
            "attributes_total": 0,
            "attributes_returned": 0,
            "attributes_truncated": False,
            "tags": [],
            "tags_total": 0,
            "tags_returned": 0,
            "tags_truncated": False,
            "children": [],
            "children_total": 0,
            "children_returned": 0,
            "children_truncated": False,
            "children_truncation_reason": "complete",
            "descendant_count": 0,
            "descendant_count_complete": True,
            "descendant_truncation_reason": "complete",
            "descendant_class_counts": [],
            "output_limit_bytes": 500_000,
        }

    async def test_cross_session_overlap_same_request_id_reverse_order(
        self,
    ) -> None:
        args_a = {
            "path": ["Workspace", "InspectA"],
            "child_limit": 17,
            "descendant_max_depth": 4,
            "descendant_scan_limit": 91,
            "time_limit_ms": 1_250,
        }
        args_b = {
            "path": ["ReplicatedStorage", "InspectB"],
            "child_limit": 23,
            "descendant_max_depth": 7,
            "descendant_scan_limit": 103,
            "time_limit_ms": 1_750,
        }
        shared_request_id = "phase2-inspect-shared-request"
        call_a = asyncio.create_task(
            self.call(
                self.a,
                args_a,
                request_id=shared_request_id,
            )
        )
        call_b = asyncio.create_task(
            self.call(
                self.b,
                args_b,
                request_id=shared_request_id,
            )
        )
        request_a, request_b = await asyncio.gather(
            self.a.next_request(),
            self.b.next_request(),
        )

        self.assertEqual(shared_request_id, request_a["request_id"])
        self.assertEqual(shared_request_id, request_b["request_id"])
        self.assertEqual(INSPECT_REMOTE, request_a["operation"])
        self.assertEqual(INSPECT_REMOTE, request_b["operation"])
        self.assertEqual(self.a.studio_id, request_a["studio_id"])
        self.assertEqual(self.b.studio_id, request_b["studio_id"])
        self.assertEqual(
            self.a.registration.document_epoch,
            request_a["document_epoch"],
        )
        self.assertEqual(
            self.b.registration.document_epoch,
            request_b["document_epoch"],
        )
        self.assertEqual(self.a.generation, request_a["generation"])
        self.assertEqual(self.b.generation, request_b["generation"])
        self.assertEqual(args_a, request_a["args"])
        self.assertEqual(args_b, request_b["args"])
        for request in (request_a, request_b):
            self.assertNotIn("studio_id", request["args"])
            self.assertNotIn("active_studio", request["args"])
            self.assertNotIn("default_studio", request["args"])

        result_a = self.valid_empty_result(self.a, request_a)
        result_b = self.valid_empty_result(self.b, request_b)
        for studio, request, result, expected_args in (
            (self.a, request_a, result_a, args_a),
            (self.b, request_b, result_b, args_b),
        ):
            self.assertEqual(studio.studio_id, result["studio_id"])
            self.assertEqual(
                studio.client_instance_id,
                result["client_instance_id"],
            )
            self.assertEqual(
                studio.registration.document_epoch,
                result["document_epoch"],
            )
            self.assertEqual(
                studio.generation,
                result["generation"],
            )
            self.assertEqual(
                request["request_id"],
                result["request_id"],
            )
            self.assertEqual(expected_args["path"], result["path"])
        self.assertTrue(self.b.respond(request_b, result_b))
        self.assertEqual(result_b, await call_b)
        self.assertFalse(call_a.done())
        self.assertTrue(self.a.respond(request_a, result_a))
        self.assertEqual(result_a, await call_a)

    async def test_same_session_inspections_are_fifo_serialized(
        self,
    ) -> None:
        first = asyncio.create_task(
            self.call(
                self.a,
                {"path": ["Workspace", "First"]},
                request_id="phase2-inspect-first",
            )
        )
        first_request = await self.a.next_request()
        second = asyncio.create_task(
            self.call(
                self.a,
                {"path": ["Workspace", "Second"]},
                request_id="phase2-inspect-second",
            )
        )
        await asyncio.sleep(0)
        self.assertTrue(self.a.transport._queue.empty())
        self.assertFalse(second.done())

        first_result = self.valid_empty_result(
            self.a,
            first_request,
        )
        self.assertTrue(self.a.respond(first_request, first_result))
        self.assertEqual(first_result, await first)

        second_request = await self.a.next_request()
        self.assertEqual(
            "phase2-inspect-second",
            second_request["request_id"],
        )
        self.assertEqual(
            {"path": ["Workspace", "Second"]},
            second_request["args"],
        )
        second_result = self.valid_empty_result(
            self.a,
            second_request,
        )
        self.assertTrue(self.a.respond(second_request, second_result))
        self.assertEqual(second_result, await second)

    async def test_reconnect_fences_old_inspection_and_new_generation_works(
        self,
    ) -> None:
        old_generation = self.a.generation
        old_document_epoch = self.a.registration.document_epoch
        operation = asyncio.create_task(
            self.call(
                self.a,
                {"path": ["Workspace", "OldGeneration"]},
                request_id="phase2-inspect-old-generation",
            )
        )
        request = await self.a.next_request()
        self.assertEqual(old_generation, request["generation"])
        old_result = self.valid_empty_result(self.a, request)
        self.assertTrue(self.a.respond(request, old_result))

        self.assertTrue(self.a.disconnect())
        old_connection = await self.a.reconnect()
        self.assertEqual(old_generation + 1, self.a.generation)
        self.assertEqual(
            old_document_epoch,
            self.a.registration.document_epoch,
        )
        with self.assertRaises(StaleGenerationError):
            await operation

        with self.assertRaises(AuthenticationError):
            self.registry.receive_response(
                old_connection.studio_id,
                old_connection.generation,
                old_connection.resume_token,
                request["request_id"],
                success=True,
                result=old_result,
            )

        fresh = asyncio.create_task(
            self.call(
                self.a,
                {"path": ["Workspace", "NewGeneration"]},
                request_id="phase2-inspect-new-generation",
            )
        )
        fresh_request = await self.a.next_request()
        self.assertEqual(self.a.studio_id, fresh_request["studio_id"])
        self.assertEqual(
            old_document_epoch,
            fresh_request["document_epoch"],
        )
        self.assertEqual(
            old_generation + 1,
            fresh_request["generation"],
        )
        fresh_result = self.valid_empty_result(
            self.a,
            fresh_request,
        )
        self.assertTrue(
            self.a.respond(fresh_request, fresh_result)
        )
        self.assertEqual(fresh_result, await fresh)

    async def test_oversized_inspection_result_fails_closed(
        self,
    ) -> None:
        path = [
            f"{index:02d}" + "x" * 98
            for index in range(63)
        ]
        operation = asyncio.create_task(
            self.call(
                self.a,
                {
                    "path": path,
                    "child_limit": 200,
                    "descendant_max_depth": 1,
                    "descendant_scan_limit": 100,
                    "time_limit_ms": 1_000,
                },
                request_id="phase2-inspect-oversized-result",
            )
        )
        request = await self.a.next_request()
        result = self.valid_empty_result(self.a, request)
        child_names = [
            f"{index:02d}" + "y" * 98
            for index in range(80)
        ]
        result["children"] = [
            {
                "name": child_name,
                "class_name": "Folder",
                "addressable": index < len(child_names) - 1,
                "path": (
                    path + [child_name]
                    if index < len(child_names) - 1
                    else []
                ),
            }
            for index, child_name in enumerate(child_names)
        ]
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

        self.assertTrue(self.a.respond(request, result))
        with self.assertRaisesRegex(RemoteToolError, INVALID_RESPONSE):
            await operation


if __name__ == "__main__":
    unittest.main()
