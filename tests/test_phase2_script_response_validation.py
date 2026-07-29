from __future__ import annotations

import asyncio
import base64
import copy
import unittest

from studio_mcp_v2.errors import RemoteToolError

from .helpers import FakeStudio, make_service


SEARCH = "studio_search_scripts"
GREP = "studio_grep_scripts"
INVALID_RESPONSE = (
    "^Targeted Studio returned an invalid script query response$"
)


class DurableScriptResponseValidationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.registry, _catalog, _service = make_service()
        self.studio = await FakeStudio.create(
            self.registry,
            "Script response validation",
            {SEARCH, GREP},
        )
        self.studio.session.mode = "play"
        self.studio.session.last_confirmed_mode = "play"

    @staticmethod
    def cursor(payload: bytes = b'{"v":1}') -> str:
        return (
            base64.b64encode(payload).decode("ascii")
            + "."
            + "a" * 64
        )

    @staticmethod
    def search_args():
        return {
            "keywords": " Player,Controller ",
            "root_path": ["Workspace"],
            "max_depth": 3,
            "scan_limit": 10,
            "page_size": 2,
            "time_limit_ms": 1_000,
        }

    @staticmethod
    def grep_args():
        return {
            "query": "needle",
            "root_path": ["ServerScriptService"],
            "max_depth": 3,
            "case_sensitive": True,
            "scan_limit": 10,
            "source_byte_limit": 262_144,
            "page_size": 2,
            "time_limit_ms": 1_000,
        }

    def common_result(self, request, *, output_limit):
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "v": 1,
            "operation": request["operation"],
            "studio_id": self.studio.studio_id,
            "client_instance_id": self.studio.client_instance_id,
            "document_epoch": self.studio.registration.document_epoch,
            "generation": self.studio.generation,
            "request_id": request["request_id"],
            "root_path": copy.deepcopy(request["args"]["root_path"]),
            "sort_version": "name-class-v1",
            "max_depth": request["args"]["max_depth"],
            "scan_limit": request["args"]["scan_limit"],
            "page_size": request["args"]["page_size"],
            "time_limit_ms": request["args"]["time_limit_ms"],
            "scanned_instances": 3,
            "scanned_scripts": 1,
            "returned": 1,
            "items": [],
            "truncated": False,
            "has_more": False,
            "continuation_cursor": "",
            "truncation_reason": "complete",
            "output_limit_bytes": output_limit,
        }

    def valid_search(self, request):
        result = self.common_result(request, output_limit=200_000)
        result.update(
            {
                "keywords": ["player", "controller"],
                "match_semantics": (
                    "all_keywords_ascii_case_insensitive_"
                    "literal_subsequence"
                ),
                "query_version": "script-name-query-v1",
            }
        )
        result["items"] = [
            {
                "path": ["Workspace", "PlayerController"],
                "name": "PlayerController",
                "class_name": "LocalScript",
            }
        ]
        return result

    def valid_grep(self, request):
        result = self.common_result(request, output_limit=500_000)
        result.update(
            {
                "query": "needle",
                "match_mode": "literal",
                "case_sensitive": True,
                "query_version": "script-grep-query-v1",
                "source_byte_limit": 262_144,
                "source_bytes_scanned": 18,
            }
        )
        result["items"] = [
            {
                "path": ["ServerScriptService", "Search"],
                "name": "Search",
                "class_name": "Script",
                "source_sha256": "1" * 64,
                "source_length": 18,
                "match_start_byte": 7,
                "match_end_byte": 12,
                "line_number": 1,
                "column_byte": 7,
                "preview_start_byte": 1,
                "preview": "hello needle world",
                "preview_prefix_truncated": False,
                "preview_suffix_truncated": False,
            }
        ]
        return result

    async def invoke(self, operation, arguments, *, request_id=None):
        task = asyncio.create_task(
            self.studio.session.invoke(
                operation,
                arguments,
                1_000,
                request_id=request_id,
            )
        )
        request = await self.studio.next_request()
        self.assertEqual(operation, request["operation"])
        return task, request

    async def assert_rejected(
        self,
        operation,
        arguments,
        candidate_builder,
        *,
        request_id=None,
    ) -> None:
        before = (
            self.studio.session.mode,
            self.studio.session.last_confirmed_mode,
            self.studio.session.uncertainty_state,
            self.studio.session.play_bridge_uncertain,
            copy.deepcopy(self.studio.session.uncertain_requests),
        )
        task, request = await self.invoke(
            operation, arguments, request_id=request_id
        )
        candidate = candidate_builder(request)

        self.assertTrue(self.studio.respond(request, candidate))
        with self.assertRaisesRegex(RemoteToolError, INVALID_RESPONSE):
            await task

        self.assertNotIn(
            request["request_id"], self.studio.session.pending
        )
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

    async def test_valid_direct_results_are_exact_and_state_neutral(self):
        for operation, arguments, builder in (
            (SEARCH, self.search_args(), self.valid_search),
            (GREP, self.grep_args(), self.valid_grep),
        ):
            with self.subTest(operation=operation):
                task, request = await self.invoke(operation, arguments)
                result = builder(request)

                self.assertTrue(self.studio.respond(request, result))
                self.assertEqual(result, await task)
                self.assertEqual("play", self.studio.session.mode)
                self.assertEqual(
                    "play", self.studio.session.last_confirmed_mode
                )
                self.assertIsNone(
                    self.studio.session.uncertainty_state
                )

    async def test_valid_results_complete_identically_as_jobs(self):
        for operation, arguments, builder in (
            (SEARCH, self.search_args(), self.valid_search),
            (GREP, self.grep_args(), self.valid_grep),
        ):
            with self.subTest(operation=operation):
                job = self.studio.session.start_job(
                    operation + "_v2",
                    operation,
                    arguments,
                    1_000,
                )
                request = await self.studio.next_request()
                result = builder(request)

                self.assertTrue(self.studio.respond(request, result))
                await job.task

                self.assertEqual("completed", job.status)
                self.assertEqual(result, job.result)
                self.assertIsNone(job.error)
                self.assertEqual("play", self.studio.session.mode)

    async def test_valid_pagination_and_case_insensitive_grep(self):
        search_args = self.search_args()
        task, request = await self.invoke(SEARCH, search_args)
        result = self.valid_search(request)
        result["items"].append(
            {
                "path": ["Workspace", "PlayerControllerTwo"],
                "name": "PlayerControllerTwo",
                "class_name": "ModuleScript",
            }
        )
        result["returned"] = 2
        result["scanned_scripts"] = 2
        result["continuation_cursor"] = self.cursor()
        result["truncated"] = True
        result["has_more"] = True
        result["truncation_reason"] = "page_size"
        self.studio.respond(request, result)
        self.assertEqual(result, await task)

    async def test_search_uses_ordered_byte_subsequence_semantics(self):
        arguments = self.search_args()
        arguments["keywords"] = "pyr"
        task, request = await self.invoke(SEARCH, arguments)
        result = self.valid_search(request)
        result["keywords"] = ["pyr"]

        self.studio.respond(request, result)
        self.assertEqual(result, await task)

        arguments["keywords"] = "ryp"

        def wrong_order(request):
            candidate = self.valid_search(request)
            candidate["keywords"] = ["ryp"]
            return candidate

        await self.assert_rejected(SEARCH, arguments, wrong_order)

        grep_args = self.grep_args()
        grep_args["query"] = "NeEdLe"
        grep_args["case_sensitive"] = False
        task, request = await self.invoke(GREP, grep_args)
        result = self.valid_grep(request)
        result["query"] = "NeEdLe"
        result["case_sensitive"] = False
        self.studio.respond(request, result)
        self.assertEqual(result, await task)

    async def test_nonobject_missing_and_extra_results_fail_closed(self):
        await self.assert_rejected(
            SEARCH, self.search_args(), lambda _request: "not-an-object"
        )
        await self.assert_rejected(
            SEARCH,
            self.search_args(),
            lambda request: {
                key: value
                for key, value in self.valid_search(request).items()
                if key != "items"
            },
        )

        def extra(request):
            candidate = self.valid_search(request)
            candidate["image_base64"] = "not-an-image"
            return candidate

        await self.assert_rejected(
            SEARCH, self.search_args(), extra
        )

    async def test_cross_session_request_and_generation_fail_closed(self):
        other = await FakeStudio.create(
            self.registry, "Other script response", {SEARCH, GREP}
        )

        def cross_session(request):
            candidate = self.valid_search(request)
            candidate["studio_id"] = other.studio_id
            candidate["client_instance_id"] = other.client_instance_id
            candidate["document_epoch"] = (
                other.registration.document_epoch
            )
            return candidate

        await self.assert_rejected(
            SEARCH, self.search_args(), cross_session
        )

        for field, replacement in (
            ("request_id", "other-request"),
            ("generation", self.studio.generation + 1),
        ):
            with self.subTest(field=field):
                def mutate(request, field=field, replacement=replacement):
                    candidate = self.valid_search(request)
                    candidate[field] = replacement
                    return candidate

                await self.assert_rejected(
                    SEARCH, self.search_args(), mutate
                )

    async def test_boolean_in_integer_fields_fails_closed(self):
        for operation, arguments, builder, field in (
            (SEARCH, self.search_args(), self.valid_search, "v"),
            (
                SEARCH,
                self.search_args(),
                self.valid_search,
                "scanned_instances",
            ),
            (
                GREP,
                self.grep_args(),
                self.valid_grep,
                "source_bytes_scanned",
            ),
        ):
            with self.subTest(operation=operation, field=field):
                def mutate(request, builder=builder, field=field):
                    candidate = builder(request)
                    candidate[field] = True
                    return candidate

                await self.assert_rejected(
                    operation, arguments, mutate
                )

    async def test_bad_counts_cursor_and_reason_fail_closed(self):
        mutations = []

        def returned_mismatch(request):
            candidate = self.valid_search(request)
            candidate["returned"] = 2
            return candidate

        mutations.append(returned_mismatch)

        def scanned_over_limit(request):
            candidate = self.valid_search(request)
            candidate["scanned_instances"] = 11
            return candidate

        mutations.append(scanned_over_limit)

        def invalid_cursor(request):
            candidate = self.valid_search(request)
            candidate["continuation_cursor"] = "not-base64." + "a" * 64
            candidate["truncated"] = True
            candidate["has_more"] = True
            candidate["truncation_reason"] = "time_budget"
            return candidate

        mutations.append(invalid_cursor)

        def incoherent_complete(request):
            candidate = self.valid_search(request)
            candidate["continuation_cursor"] = self.cursor()
            return candidate

        mutations.append(incoherent_complete)

        def short_page(request):
            candidate = self.valid_search(request)
            candidate["continuation_cursor"] = self.cursor()
            candidate["truncated"] = True
            candidate["has_more"] = True
            candidate["truncation_reason"] = "page_size"
            return candidate

        mutations.append(short_page)

        def early_scan_limit(request):
            candidate = self.valid_search(request)
            candidate["continuation_cursor"] = self.cursor()
            candidate["truncated"] = True
            candidate["has_more"] = True
            candidate["truncation_reason"] = "scan_limit"
            return candidate

        mutations.append(early_scan_limit)

        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                await self.assert_rejected(
                    SEARCH, self.search_args(), mutation
                )

    async def test_bad_search_path_name_class_and_match_fail_closed(self):
        replacements = (
            ("path", ["OtherRoot", "PlayerController"]),
            ("path", ["Workspace", "Bad\nName"]),
            ("path", ["Workspace", "x" * 101]),
            ("name", "Different"),
            ("class_name", "Folder"),
            ("name", "PlayerOnly"),
        )
        for field, replacement in replacements:
            with self.subTest(field=field, replacement=replacement):
                def mutate(
                    request,
                    field=field,
                    replacement=replacement,
                ):
                    candidate = self.valid_search(request)
                    candidate["items"][0][field] = replacement
                    if field == "name" and replacement == "PlayerOnly":
                        candidate["items"][0]["path"][-1] = replacement
                    return candidate

                await self.assert_rejected(
                    SEARCH, self.search_args(), mutate
                )

    async def test_duplicate_search_paths_fail_closed(self):
        def duplicate(request):
            candidate = self.valid_search(request)
            candidate["items"].append(
                copy.deepcopy(candidate["items"][0])
            )
            candidate["returned"] = 2
            candidate["scanned_scripts"] = 2
            return candidate

        await self.assert_rejected(
            SEARCH, self.search_args(), duplicate
        )

    async def test_descending_search_paths_fail_closed(self):
        def descending(request):
            candidate = self.valid_search(request)
            candidate["items"] = [
                {
                    "path": ["Workspace", "ZPlayerController"],
                    "name": "ZPlayerController",
                    "class_name": "LocalScript",
                },
                {
                    "path": ["Workspace", "APlayerController"],
                    "name": "APlayerController",
                    "class_name": "ModuleScript",
                },
            ]
            candidate["returned"] = 2
            candidate["scanned_scripts"] = 2
            return candidate

        await self.assert_rejected(
            SEARCH, self.search_args(), descending
        )

    async def test_bad_grep_hash_offsets_and_preview_fail_closed(self):
        mutations = []

        def uppercase_hash(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["source_sha256"] = "A" * 64
            return candidate

        mutations.append(uppercase_hash)

        def wrong_match_end(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["match_end_byte"] = 13
            return candidate

        mutations.append(wrong_match_end)

        def outside_source(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["match_start_byte"] = 19
            candidate["items"][0]["match_end_byte"] = 24
            return candidate

        mutations.append(outside_source)

        def wrong_preview(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["preview"] = "hello xxxxxx world"
            return candidate

        mutations.append(wrong_preview)

        def wrong_preview_origin(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["preview_start_byte"] = 2
            return candidate

        mutations.append(wrong_preview_origin)

        def malformed_truncation_flag(request):
            candidate = self.valid_grep(request)
            candidate["items"][0][
                "preview_prefix_truncated"
            ] = 1
            return candidate

        mutations.append(malformed_truncation_flag)

        def oversize_preview(request):
            candidate = self.valid_grep(request)
            candidate["items"][0].update(
                {
                    "source_length": 518,
                    "match_start_byte": 1,
                    "match_end_byte": 6,
                    "preview": "needle" + "é" * 256,
                    "preview_suffix_truncated": False,
                }
            )
            return candidate

        mutations.append(oversize_preview)

        def impossible_column(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["column_byte"] = 8
            return candidate

        mutations.append(impossible_column)

        def false_prefix_origin(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["preview_prefix_truncated"] = True
            return candidate

        mutations.append(false_prefix_origin)

        def impossible_source_suffix(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["preview_suffix_truncated"] = True
            return candidate

        mutations.append(impossible_source_suffix)

        def impossible_short_suffix(request):
            candidate = self.valid_grep(request)
            candidate["source_bytes_scanned"] = 100
            candidate["items"][0]["source_length"] = 100
            candidate["items"][0]["preview_suffix_truncated"] = True
            return candidate

        mutations.append(impossible_short_suffix)

        def multiline_preview(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["preview"] = "hello needle\nworld"
            return candidate

        mutations.append(multiline_preview)

        def impossible_first_line(request):
            candidate = self.valid_grep(request)
            candidate["items"][0]["line_number"] = 2
            return candidate

        mutations.append(impossible_first_line)

        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                await self.assert_rejected(
                    GREP, self.grep_args(), mutation
                )

    async def test_grep_duplicates_order_revision_and_groups_fail_closed(self):
        def two_match_result(request):
            candidate = self.valid_grep(request)
            first = candidate["items"][0]
            first.update(
                {
                    "source_length": 15,
                    "match_start_byte": 1,
                    "match_end_byte": 6,
                    "column_byte": 1,
                    "preview": "needle x needle",
                }
            )
            second = copy.deepcopy(first)
            second["match_start_byte"] = 10
            second["match_end_byte"] = 15
            second["column_byte"] = 10
            candidate["items"] = [first, second]
            candidate["returned"] = 2
            candidate["source_bytes_scanned"] = 15
            return candidate

        builders = []

        def duplicate(request):
            candidate = two_match_result(request)
            candidate["items"][1]["match_start_byte"] = 1
            candidate["items"][1]["match_end_byte"] = 6
            return candidate

        builders.append(duplicate)

        def out_of_order(request):
            candidate = two_match_result(request)
            candidate["items"].reverse()
            return candidate

        builders.append(out_of_order)

        def changed_revision(request):
            candidate = two_match_result(request)
            candidate["items"][1]["source_sha256"] = "2" * 64
            return candidate

        builders.append(changed_revision)

        def noncontiguous_group(request):
            candidate = two_match_result(request)
            middle = copy.deepcopy(candidate["items"][0])
            middle.update(
                {
                    "path": ["ServerScriptService", "Other"],
                    "name": "Other",
                    "source_sha256": "3" * 64,
                }
            )
            candidate["items"].insert(1, middle)
            candidate["returned"] = 3
            candidate["page_size"] = 3
            return candidate

        builders.append(noncontiguous_group)

        def descending_groups(request):
            candidate = two_match_result(request)
            first, second = candidate["items"]
            first["path"] = ["ServerScriptService", "ZSearch"]
            first["name"] = "ZSearch"
            second["path"] = ["ServerScriptService", "ASearch"]
            second["name"] = "ASearch"
            second["source_sha256"] = "2" * 64
            candidate["scanned_scripts"] = 2
            candidate["source_bytes_scanned"] = 30
            return candidate

        builders.append(descending_groups)

        for index, builder in enumerate(builders):
            with self.subTest(case=index):
                arguments = self.grep_args()
                if index == 3:
                    arguments["page_size"] = 3
                await self.assert_rejected(GREP, arguments, builder)

    async def test_valid_utf8_boundary_truncated_long_preview(self):
        task, request = await self.invoke(GREP, self.grep_args())
        candidate = self.valid_grep(request)
        item = candidate["items"][0]
        preview = "x" * 253 + "needle" + "y" * 250
        self.assertEqual(509, len(preview.encode("utf-8")))
        item.update(
            {
                "source_length": 600,
                "match_start_byte": 300,
                "match_end_byte": 305,
                "column_byte": 300,
                "preview_start_byte": 47,
                "preview": preview,
                "preview_prefix_truncated": True,
                "preview_suffix_truncated": True,
            }
        )
        candidate["source_bytes_scanned"] = 600

        self.assertTrue(self.studio.respond(request, candidate))
        self.assertEqual(candidate, await task)

    async def test_overlapping_grep_matches_fail_closed(self):
        arguments = self.grep_args()
        arguments["query"] = "aa"

        def overlapping(request):
            candidate = self.valid_grep(request)
            candidate["query"] = "aa"
            candidate["source_bytes_scanned"] = 3
            first = candidate["items"][0]
            first.update(
                {
                    "source_length": 3,
                    "match_start_byte": 1,
                    "match_end_byte": 2,
                    "column_byte": 1,
                    "preview": "aaa",
                }
            )
            second = copy.deepcopy(first)
            second["match_start_byte"] = 2
            second["match_end_byte"] = 3
            second["column_byte"] = 2
            candidate["items"] = [first, second]
            candidate["returned"] = 2
            return candidate

        await self.assert_rejected(GREP, arguments, overlapping)

    async def test_malformed_pending_arguments_cannot_bless_success(self):
        malformed = (
            {},
            {"keywords": "one,ONE"},
            {"keywords": "one", "scan_limit": True},
            {"keywords": "one", "unexpected": "field"},
            {
                "keywords": "one",
                "continuation_cursor": "not-a-cursor",
            },
        )
        for index, arguments in enumerate(malformed):
            with self.subTest(case=index):
                await self.assert_rejected(
                    SEARCH,
                    arguments,
                    lambda request: self.valid_search(
                        {
                            **request,
                            "args": self.search_args(),
                        }
                    ),
                )

    async def test_oversized_utf8_canonical_result_fails_closed(self):
        request_id = "é" * 100_000
        task, request = await self.invoke(
            SEARCH, self.search_args(), request_id=request_id
        )
        candidate = self.valid_search(request)

        self.assertTrue(
            self.studio.session.receive_response(
                self.studio.generation,
                request_id,
                success=True,
                result=candidate,
            )
        )
        with self.assertRaisesRegex(RemoteToolError, INVALID_RESPONSE):
            await task
        self.assertEqual("play", self.studio.session.mode)
        self.assertIsNone(self.studio.session.uncertainty_state)

    async def test_invalid_job_result_fails_without_state_changes(self):
        job = self.studio.session.start_job(
            SEARCH + "_v2",
            SEARCH,
            self.search_args(),
            1_000,
        )
        request = await self.studio.next_request()
        candidate = self.valid_search(request)
        candidate["generation"] = True

        self.assertTrue(self.studio.respond(request, candidate))
        await job.task

        self.assertEqual("failed", job.status)
        self.assertEqual("studio_tool_error", job.error["code"])
        self.assertEqual(
            "Targeted Studio returned an invalid script query response",
            job.error["message"],
        )
        self.assertEqual("play", self.studio.session.mode)
        self.assertIsNone(self.studio.session.uncertainty_state)


if __name__ == "__main__":
    unittest.main()
