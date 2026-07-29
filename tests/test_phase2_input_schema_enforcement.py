from __future__ import annotations

import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import ValidationError
from studio_mcp_v2.multi_edit import canonical_json_sha256
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.schema_validation import (
    validate_input_schema_definition,
    validate_schema_instance,
)
from studio_mcp_v2.service import ProxyService

from .helpers import ALLOW_ALL, PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)


class InputSchemaSubsetTests(unittest.TestCase):
    def test_boolean_does_not_satisfy_integer_or_number(self) -> None:
        for schema_type in ("integer", "number"):
            with self.subTest(schema_type=schema_type):
                with self.assertRaises(ValidationError):
                    validate_schema_instance(
                        True, {"type": schema_type}
                    )

    def test_huge_integer_is_bounded_without_float_overflow(self) -> None:
        with self.assertRaises(ValidationError):
            validate_schema_instance(
                10**5000,
                {"type": "number", "maximum": 100},
            )

    def test_nested_const_equality_keeps_boolean_distinct_from_integer(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            validate_schema_instance(
                {"value": 1},
                {"const": {"value": True}},
            )

    def test_dependent_required_and_closed_objects(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": [],
            "dependentRequired": {
                "start": ["end"],
                "end": ["start"],
            },
            "additionalProperties": False,
        }
        validate_input_schema_definition(schema)
        validate_schema_instance({}, schema)
        validate_schema_instance(
            {"start": 0, "end": 1}, schema
        )
        with self.assertRaises(ValidationError):
            validate_schema_instance({"start": 0}, schema)
        with self.assertRaises(ValidationError):
            validate_schema_instance({"rogue": 1}, schema)

    def test_uuid_format_is_canonical_and_schema_drift_fails_closed(
        self,
    ) -> None:
        schema = {"type": "string", "format": "uuid"}
        validate_input_schema_definition(schema)
        validate_schema_instance(
            "00000000-0000-4000-8000-000000000001", schema
        )
        with self.assertRaises(ValidationError):
            validate_schema_instance(
                "00000000-0000-4000-8000-00000000000A",
                schema,
            )
        with self.assertRaises(ValidationError):
            validate_input_schema_definition(
                {"type": "string", "futureKeyword": True}
            )

    def test_catalog_rejects_an_unknown_input_schema_shape(self) -> None:
        with self.assertRaises(ValidationError):
            ToolCatalog(
                [
                    {
                        "name": "future_tool",
                        "inputSchema": {
                            "type": "object",
                            "unevaluatedProperties": False,
                        },
                    }
                ]
            )


class ServiceInputSchemaEnforcementTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.service = ProxyService(self.registry, self.catalog)
        self.studio = await FakeStudio.create(
            self.registry,
            "Schema enforcement",
            self.catalog.remote_names,
        )

    async def test_direct_invalid_tree_is_rejected_before_dispatch(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            await self.service.call_tool(
                ALLOW_ALL,
                "studio_list_tree_v2",
                {
                    "studio_id": self.studio.studio_id,
                    "root_path": "not-an-array",
                    "rogue": True,
                },
            )
        self.assertTrue(self.studio.transport._queue.empty())
        self.assertFalse(self.studio.session.pending)

    async def test_nested_job_invalid_tree_creates_no_job_and_dispatches_nothing(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            self.service.start_job(
                ALLOW_ALL,
                self.studio.studio_id,
                "studio_list_tree_v2",
                {
                    "root_path": "not-an-array",
                    "rogue": True,
                },
                1_000,
            )
        self.assertEqual({}, self.studio.session.jobs)
        self.assertTrue(self.studio.transport._queue.empty())

    async def test_nested_job_rejects_schema_drift_before_admission_contract(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            self.service.start_job(
                ALLOW_ALL,
                self.studio.studio_id,
                "studio_search_scripts_v2",
                {
                    "keywords": "Player",
                    "root_path": ["Workspace"],
                    "page_size": 0,
                },
                1_000,
            )
        self.assertEqual({}, self.studio.session.jobs)
        self.assertTrue(self.studio.transport._queue.empty())

    async def test_job_wrapper_rejects_mutators_without_terminal_receipts(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            self.service.start_job(
                ALLOW_ALL,
                self.studio.studio_id,
                "studio_update_script_v2",
                {
                    "path": ["ServerScriptService", "Main"],
                    "expected_sha256": "1" * 64,
                    "new_source": "return true",
                },
                1_000,
            )
        self.assertEqual({}, self.studio.session.jobs)
        self.assertTrue(self.studio.transport._queue.empty())

    async def test_valid_nested_tree_is_admitted_with_schema_pins(
        self,
    ) -> None:
        definition = self.catalog.get("studio_list_tree_v2")
        receipt = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            "studio_list_tree_v2",
            {
                "root_path": ["Workspace"],
                "page_size": 1,
            },
            1_000,
        )
        self.assertEqual(
            definition.input_schema_sha256,
            receipt["input_schema_sha256"],
        )
        request = await self.studio.next_request()
        self.assertEqual(
            {"root_path": ["Workspace"], "page_size": 1},
            request["args"],
        )
        job = self.studio.session.jobs[receipt["job_id"]]
        job.task.cancel()
        await job.task

    async def test_job_dispatch_uses_the_exact_admitted_argument_snapshot(
        self,
    ) -> None:
        arguments = {
            "root_path": ["Workspace"],
            "page_size": 1,
        }
        admitted = {
            "root_path": ["Workspace"],
            "page_size": 1,
        }
        receipt = self.service.start_job(
            ALLOW_ALL,
            self.studio.studio_id,
            "studio_list_tree_v2",
            arguments,
            1_000,
        )
        arguments["root_path"][0] = "ServerStorage"
        arguments["page_size"] = 9
        arguments["rogue"] = True

        request = await self.studio.next_request()
        self.assertEqual(admitted, request["args"])
        self.assertEqual(
            canonical_json_sha256(admitted),
            receipt["arguments_sha256"],
        )
        self.assertEqual(
            ["Workspace"],
            receipt["admitted_contract"]["root_path"],
        )
        job = self.studio.session.jobs[receipt["job_id"]]
        job.task.cancel()
        await job.task


if __name__ == "__main__":
    unittest.main()
