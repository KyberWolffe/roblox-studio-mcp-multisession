from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts import render_studio_plugin


ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
TOKEN = "t" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"


class Phase2ScriptLuauTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HANDLERS.read_text(encoding="utf-8")
        cls.search = cls.source[
            cls.source.index("local function searchScripts(") :
            cls.source.index("local function readGrepSource(")
        ]
        cls.grep = cls.source[
            cls.source.index("local function readGrepSource(") :
            cls.source.index("local function newInspectionValue(")
        ]

    def test_closed_argument_keys_and_bounds_are_present(self) -> None:
        search_keys = self.source[
            self.source.index("studio_search_scripts = table.freeze({") :
            self.source.index("studio_grep_scripts = table.freeze({")
        ]
        grep_keys = self.source[
            self.source.index("studio_grep_scripts = table.freeze({") :
            self.source.index("studio_read_script = table.freeze({")
        ]
        self.assertEqual(
            {
                "keywords",
                "root_path",
                "max_depth",
                "scan_limit",
                "page_size",
                "time_limit_ms",
                "continuation_cursor",
            },
            set(re.findall(r"\n\t\t([a-z_]+) = true,", search_keys)),
        )
        self.assertEqual(
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
            },
            set(re.findall(r"\n\t\t([a-z_]+) = true,", grep_keys)),
        )
        for marker in (
            "MAX_SCRIPT_QUERY_BYTES = 256",
            "MAX_SCRIPT_KEYWORDS = 8",
            "MAX_SCRIPT_CURSOR_BYTES = 2_048",
            "MAX_SCRIPT_SCAN_LIMIT = 5_000",
            "MAX_SCRIPT_SEARCH_PAGE_SIZE = 10",
            "MAX_SCRIPT_GREP_PAGE_SIZE = 50",
            "MAX_SCRIPT_SOURCE_BUDGET = 4_194_304",
            "MAX_SCRIPT_SOURCE_LINES = 20_000",
            "MAX_SCRIPT_PREVIEW_BYTES = 512",
            "MAX_SCRIPT_SEARCH_OUTPUT_BYTES = 200_000",
            "MAX_SCRIPT_GREP_OUTPUT_BYTES = 500_000",
        ):
            self.assertIn(marker, self.source)

    def test_search_is_versioned_literal_subsequence_and_read_only(self) -> None:
        self.assertIn("normalizeScriptKeywords(args.keywords)", self.search)
        self.assertIn("nameMatchesScriptKeywords(instance.Name, keywords)", self.search)
        matcher = self.source[
            self.source.index("local function nameMatchesScriptKeywords(") :
            self.source.index("local function newScriptResult(")
        ]
        self.assertIn("string.lower(name)", matcher)
        self.assertRegex(
            matcher,
            re.compile(
                r"string\.find\(\s*foldedName,\s*byteText,\s*"
                r"nextIndex,\s*true\s*\)",
                re.MULTILINE,
            ),
        )
        self.assertIn(
            '"all_keywords_ascii_case_insensitive_literal_subsequence"',
            self.source,
        )
        self.assertIn('"script-name-query-v1"', self.source)
        self.assertIn('requireEditMode()', self.search)
        for mutator in (
            "UpdateSourceAsync",
            "SetAttribute",
            "Instance.new",
            "loadstring",
            "require(",
        ):
            self.assertNotIn(mutator, self.search)

    def test_grep_is_literal_revision_fenced_and_read_only(self) -> None:
        self.assertIn("ScriptEditorService:GetEditorSource(target)", self.grep)
        self.assertRegex(
            self.grep,
            re.compile(
                r"string\.find\(\s*haystack,\s*needle,\s*"
                r"searchStart,\s*true\s*\)",
                re.MULTILINE,
            ),
        )
        self.assertIn("sourceDigest = sourceSha256(source)", self.grep)
        self.assertIn("stale_script_source_cursor", self.grep)
        self.assertIn("utf8.len(source)", self.grep)
        self.assertIn("grepPreview(", self.grep)
        self.assertIn('"script-grep-query-v1"', self.source)
        self.assertIn('result.match_mode = "literal"', self.grep)
        self.assertIn("requireEditMode()", self.grep)
        for mutator in (
            "UpdateSourceAsync",
            "SetAttribute",
            "Instance.new",
            "loadstring",
            "require(",
        ):
            self.assertNotIn(mutator, self.grep)

    def test_cursor_domains_and_session_fences_are_distinct(self) -> None:
        cursor = self.source[
            self.source.index("local SCRIPT_SEARCH_CURSOR_KEYS") :
            self.source.index("local function isPlaceScript(")
        ]
        for marker in (
            '"studio-script-search-cursor-v1"',
            '"studio-script-grep-cursor-v1"',
            "payload.s ~= peer.studio_id",
            "payload.c ~= CLIENT_INSTANCE_ID",
            "payload.d ~= DOCUMENT_EPOCH",
            "payload.g ~= peer.generation",
            "payload.q ~= querySha256",
            "treePositionLineage(root, position)",
            "REGISTRATION_SECRET",
        ):
            self.assertIn(marker, self.source)
        self.assertIn("b = true", cursor)
        self.assertIn("h = true", cursor)
        self.assertIn("constantTimeTextEqual(", cursor)

    def test_results_are_identity_bearing_and_encoded_size_bounded(self) -> None:
        envelope = self.source[
            self.source.index("local function newScriptResult(") :
            self.source.index("local function assertBoundedScriptResult(")
        ]
        for field in (
            'adapter = "studio-mcp-v2-durable-plugin"',
            "studio_id = peer.studio_id",
            "client_instance_id = CLIENT_INSTANCE_ID",
            "document_epoch = DOCUMENT_EPOCH",
            "generation = peer.generation",
            "request_id = requestId",
        ):
            self.assertIn(field, envelope)
        self.assertIn(
            "assertBoundedScriptResult("
            "result, DURABLE_BOUNDS.MAX_SCRIPT_SEARCH_OUTPUT_BYTES)",
            self.search,
        )
        self.assertIn(
            "assertBoundedScriptResult("
            "result, DURABLE_BOUNDS.MAX_SCRIPT_GREP_OUTPUT_BYTES)",
            self.grep,
        )

    def test_traversal_frontier_and_cooperative_checks_are_bounded(self) -> None:
        self.assertIn("MAX_TREE_RETAINED_CHILDREN = 20_000", self.source)
        self.assertIn("local function assertTreeFrontierBound(", self.source)
        self.assertIn("assertTreeFrontierBound(frames, children)", self.source)
        resolver = self.source[
            self.source.index("local function resolveExactPath(") :
            self.source.index("local function requireLuaSourceContainer(")
        ]
        self.assertIn(
            "#children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE",
            resolver,
        )
        for operation in (self.search, self.grep):
            self.assertIn("TREE_COOPERATIVE_YIELD_INTERVAL", operation)
            self.assertIn("task.wait()", operation)
            self.assertIn("assertExpectedDocument()", operation)
            self.assertIn("requireEditMode()", operation)
            self.assertLess(
                operation.index("local startedAt = os.clock()"),
                operation.index("resolveExactPath(rootPath, true)"),
            )

    def test_renderer_publishes_only_the_two_new_durable_names(self) -> None:
        rendered = render_studio_plugin.render_durable(TOKEN, RUN_ID)
        capability_start = rendered.index("local CAPABILITIES = table.freeze({")
        capability_end = rendered.index(
            "local REQUEST_KEYS = table.freeze({", capability_start
        )
        capabilities = rendered[capability_start:capability_end]
        self.assertEqual(1, capabilities.count('"studio_search_scripts"'))
        self.assertEqual(1, capabilities.count('"studio_grep_scripts"'))
        self.assertEqual(1, capabilities.count("studio_search_scripts = true"))
        self.assertEqual(1, capabilities.count("studio_grep_scripts = true"))
        self.assertNotIn('"script_search"', capabilities)
        self.assertNotIn('"script_grep"', capabilities)
        self.assertNotIn("active_studio", rendered)
        self.assertNotIn("default_studio", rendered)


if __name__ == "__main__":
    unittest.main()
