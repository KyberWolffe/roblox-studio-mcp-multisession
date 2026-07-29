from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts import render_studio_plugin


ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
TOKEN = "t" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"


class Phase2TreeLuauTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HANDLERS.read_text(encoding="utf-8")

    def test_tree_query_is_closed_and_bounded(self):
        tree_keys = self.source[
            self.source.index("studio_list_tree = table.freeze({"):
            self.source.index("studio_read_script = table.freeze({")
        ]
        for name in (
            "root_path",
            "max_depth",
            "max_results",
            "name_filter",
            "class_filter",
            "class_is_a",
            "scan_limit",
            "page_size",
            "continuation_cursor",
        ):
            self.assertIn(name + " = true", tree_keys)
        self.assertIn("MAX_TREE_FILTER_BYTES = 100", self.source)
        self.assertIn("MAX_TREE_CURSOR_BYTES = 512", self.source)
        self.assertIn("MAX_TREE_SCAN_LIMIT = 5_000", self.source)
        self.assertIn("DEFAULT_TREE_SCAN_LIMIT = 2_000", self.source)
        self.assertIn("MAX_TREE_PAGE_SIZE = 500", self.source)
        self.assertIn(
            "MAX_TREE_CHILDREN_PER_INSTANCE = 10_000",
            self.source,
        )
        self.assertIn("MAX_TREE_OUTPUT_BYTES = 600_000", self.source)
        self.assertIn(
            '"max_results and page_size are mutually exclusive"',
            self.source,
        )
        self.assertIn('"class_is_a requires class_filter"', self.source)
        self.assertIn(
            "#rootPath + maxDepth > DURABLE_BOUNDS.MAX_PATH_SEGMENTS",
            self.source,
        )
        self.assertIn(
            '"root_path plus max_depth exceeds the reusable path bound"',
            self.source,
        )

    def test_name_filter_is_case_insensitive_literal_not_pattern(self):
        matcher = self.source[
            self.source.index("local function treeInstanceMatches("):
            self.source.index("local function listTree(")
        ]
        self.assertIn("string.lower(instance.Name)", matcher)
        self.assertRegex(
            matcher,
            re.compile(
                r"string\.find\(\s*"
                r"string\.lower\(instance\.Name\),\s*"
                r"nameFilterLower,\s*1,\s*true\s*\)",
                re.MULTILINE,
            ),
        )
        self.assertIn("instance.ClassName == classFilter", matcher)
        self.assertIn("instance:IsA(classFilter)", matcher)
        self.assertNotIn("string.match(instance.Name", matcher)

    def test_cursor_is_session_query_and_lineage_fenced(self):
        cursor = self.source[
            self.source.index("local TREE_CURSOR_KEYS"):
            self.source.index("local function readScript(")
        ]
        self.assertIn("TREE_CURSOR_VERSION = 1", self.source)
        self.assertIn('TREE_SORT_VERSION = "name-class-v1"', self.source)
        self.assertIn("payload.s ~= peer.studio_id", cursor)
        self.assertIn("payload.d ~= DOCUMENT_EPOCH", cursor)
        self.assertIn("payload.g ~= peer.generation", cursor)
        self.assertIn("payload.q ~= querySha256", cursor)
        self.assertIn("treePositionLineage(root, startPosition)", cursor)
        self.assertIn("expectedLineage, observedLineage", cursor)
        self.assertIn("constantTimeTextEqual(", cursor)
        self.assertIn("REGISTRATION_SECRET", cursor)
        self.assertIn("treeCursorIntegrity(json)", cursor)
        self.assertIn(
            '"^([A-Za-z0-9+/]+)(=*)%.([0-9a-f]+)$"',
            cursor,
        )
        self.assertIn("#padding > 2", cursor)
        self.assertIn("invalid_continuation_cursor", cursor)
        self.assertIn("stale_continuation_cursor", cursor)
        self.assertIn("continuation_cursor_query_mismatch", cursor)

    def test_traversal_is_iterative_cooperative_and_output_bounded(self):
        tree = self.source[
            self.source.index("local function sortedTreeChildren("):
            self.source.index("local function readScript(")
        ]
        self.assertIn("local function newTreeTraversal(", tree)
        self.assertIn("local function nextTreeNode(", tree)
        self.assertNotIn("local function visit(", tree)
        self.assertIn(
            "scanned % DURABLE_BOUNDS.TREE_COOPERATIVE_YIELD_INTERVAL == 0",
            tree,
        )
        self.assertIn("task.wait()", tree)
        self.assertIn("assertExpectedDocument()", tree)
        self.assertIn(
            "outputBytes + encodedBytes > DURABLE_BOUNDS.MAX_TREE_OUTPUT_BYTES",
            tree,
        )
        self.assertIn("if previous.Name == current.Name then", tree)
        self.assertIn(
            '"Sibling instances with duplicate names cannot produce "',
            tree,
        )
        self.assertIn("tree_name_unsupported", tree)
        self.assertIn(
            "child_count = boundedTreeChildCount(instance)",
            tree,
        )
        self.assertNotIn(
            "previous.ClassName == current.ClassName",
            tree,
        )
        self.assertIn("tree_width_unsupported", tree)
        self.assertIn('truncationReason = "output_bytes"', tree)
        self.assertIn('and "page_size" or "scan_limit"', tree)
        self.assertIn("continuation_cursor = continuationCursor", tree)

    def test_legacy_exact_path_and_max_results_contract_remains(self):
        self.assertIn(
            "local root = resolveExactPath(rootPath, true)",
            self.source,
        )
        self.assertIn(
            "local pageSize = args.page_size or args.max_results or 200",
            self.source,
        )
        self.assertIn("max_results = pageSize", self.source)
        self.assertIn("max_depth = maxDepth", self.source)
        self.assertIn("items = items", self.source)
        self.assertIn("truncated = continuationCursor ~= nil", self.source)

    def test_durable_renderer_embeds_the_phase2_handler_only(self):
        rendered = render_studio_plugin.render_durable(
            TOKEN,
            RUN_ID,
        )
        self.assertIn("local function decodeTreeCursorPayload(", rendered)
        self.assertIn("local function treePositionLineage(", rendered)
        self.assertIn("continuation_cursor = continuationCursor", rendered)
        self.assertIn("studio_id = peer.studio_id", rendered)
        self.assertNotIn("active_studio", rendered)
        self.assertNotIn("default_studio", rendered)


if __name__ == "__main__":
    unittest.main()
