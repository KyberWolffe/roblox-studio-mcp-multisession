from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
CURSOR_SECRET = "phase2-model-secret"


@dataclass
class Node:
    name: str
    class_name: str = "Folder"
    children: list["Node"] = field(default_factory=list)


@dataclass
class Frame:
    node: Node
    path: tuple[str, ...]
    position: tuple[int, ...]
    depth: int
    emitted: bool
    next_child: int


def sorted_children(node: Node) -> list[Node]:
    result = sorted(node.children, key=lambda child: (child.name, child.class_name))
    if len({child.name for child in result}) != len(result):
        raise ValueError("duplicate exact-path name")
    return result


def new_traversal(
    root: Node,
    root_path: tuple[str, ...],
    position: tuple[int, ...],
) -> list[Frame]:
    frames: list[Frame] = []
    current = root
    current_path = root_path
    current_position: tuple[int, ...] = ()
    for level, ordinal in enumerate(position, start=1):
        children = sorted_children(current)
        current_index = ordinal - 1
        if current_index < 0 or current_index >= len(children):
            raise ValueError("stale position")
        frames.append(
            Frame(
                current,
                current_path,
                current_position,
                level - 1,
                True,
                ordinal + 1,
            )
        )
        current_position += (ordinal,)
        current = children[current_index]
        current_path += (current.name,)
    frames.append(
        Frame(
            current,
            current_path,
            current_position,
            len(position),
            False,
            1,
        )
    )
    return frames


def next_node(frames: list[Frame], max_depth: int):
    while frames:
        frame = frames[-1]
        if not frame.emitted:
            frame.emitted = True
            return frame.node, frame.path, frame.position
        if frame.depth >= max_depth:
            frames.pop()
            continue
        children = sorted_children(frame.node)
        index = frame.next_child - 1
        if index >= len(children):
            frames.pop()
            continue
        frame.next_child += 1
        child = children[index]
        frames.append(
            Frame(
                child,
                frame.path + (child.name,),
                frame.position + (index + 1,),
                frame.depth + 1,
                False,
                1,
            )
        )
    return None


def lineage(root: Node, position: tuple[int, ...]) -> str:
    values: list[object] = [
        "studio-tree-lineage-v1",
        "name-class-v1",
        root.name,
        root.class_name,
    ]
    current = root
    for ordinal in position:
        children = sorted_children(current)
        if ordinal < 1 or ordinal > len(children):
            raise ValueError("stale position")
        first = max(1, ordinal - 2)
        last = min(len(children), ordinal + 2)
        values.extend((len(children), ordinal, first, last))
        for index in range(first, last + 1):
            child = children[index - 1]
            values.extend((index, child.name, child.class_name))
        current = children[ordinal - 1]
    values.extend((current.name, current.class_name))
    canonical = "".join(f"{len(str(value))}:{value};" for value in values)
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    integrity = hashlib.sha256(
        (
            CURSOR_SECRET
            + "\0studio-tree-cursor-v1\0"
            + raw
            + "\0"
            + CURSOR_SECRET
        ).encode()
    ).hexdigest()
    encoded = base64.b64encode(raw.encode()).decode()
    return encoded + "." + integrity


def decode_cursor(cursor: str) -> dict:
    encoded, supplied = cursor.split(".", 1)
    raw = base64.b64decode(encoded, validate=True).decode()
    expected = encode_cursor(json.loads(raw)).rsplit(".", 1)[1]
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("cursor integrity")
    return json.loads(raw)


def page(
    root: Node,
    *,
    max_depth: int,
    page_size: int,
    scan_limit: int,
    name_filter: Optional[str] = None,
    start_position: tuple[int, ...] = (),
    output_limit: int = 600_000,
):
    frames = new_traversal(root, (), start_position)
    items: list[dict] = []
    scanned = 0
    output_bytes = 0
    next_position = None
    while True:
        current = next_node(frames, max_depth)
        if current is None:
            break
        node, path, position = current
        scanned += 1
        matches = (
            name_filter is None
            or name_filter.lower() in node.name.lower()
        )
        if matches:
            item = {
                "path": list(path),
                "name": node.name,
                "class_name": node.class_name,
                "child_count": len(node.children),
            }
            encoded_bytes = (
                len(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                )
                + 1
            )
            if output_bytes + encoded_bytes > output_limit:
                next_position = position
                break
            items.append(item)
            output_bytes += encoded_bytes
        if len(items) >= page_size or scanned >= scan_limit:
            following = next_node(frames, max_depth)
            if following is not None:
                next_position = following[2]
            break
    return items, next_position, scanned


def full_preorder(root: Node, max_depth: int) -> list[Node]:
    frames = new_traversal(root, (), ())
    result = []
    while True:
        current = next_node(frames, max_depth)
        if current is None:
            return result
        result.append(current[0])


class Phase2TreePaginationModelTests(unittest.TestCase):
    def test_randomized_pages_never_skip_or_duplicate_stable_nodes(self):
        generator = random.Random(0xC0D3)
        for case in range(100):
            counter = 0

            def make_node(depth: int) -> Node:
                nonlocal counter
                counter += 1
                node = Node(f"node-{counter:04d}")
                if depth > 0:
                    for _ in range(generator.randint(0, 4)):
                        node.children.append(make_node(depth - 1))
                return node

            root = make_node(generator.randint(1, 5))
            max_depth = generator.randint(0, 5)
            page_size = generator.randint(1, 7)
            scan_limit = generator.randint(1, 9)
            name_filter = generator.choice((None, "node-0", "3", "absent"))
            expected = [
                node.name
                for node in full_preorder(root, max_depth)
                if name_filter is None
                or name_filter.lower() in node.name.lower()
            ]
            actual: list[str] = []
            position: tuple[int, ...] = ()
            for _ in range(2_000):
                items, next_position, _ = page(
                    root,
                    max_depth=max_depth,
                    page_size=page_size,
                    scan_limit=scan_limit,
                    name_filter=name_filter,
                    start_position=position,
                )
                actual.extend(item["name"] for item in items)
                if next_position is None:
                    break
                position = next_position
            else:
                self.fail(f"pagination did not terminate for case {case}")
            self.assertEqual(expected, actual, case)
            self.assertEqual(len(actual), len(set(actual)), case)

    def test_output_limit_resumes_the_first_unreturned_node(self):
        root = Node(
            "root",
            children=[
                Node("a" * 20),
                Node("b" * 20),
                Node("c" * 20),
            ],
        )
        all_items, _, _ = page(
            root,
            max_depth=1,
            page_size=10,
            scan_limit=10,
            output_limit=10_000,
        )
        one_item_limit = max(
            len(
                json.dumps(
                    item,
                    separators=(",", ":"),
                ).encode()
            )
            + 1
            for item in all_items
        )
        collected = []
        position: tuple[int, ...] = ()
        for _ in range(10):
            items, next_position, _ = page(
                root,
                max_depth=1,
                page_size=10,
                scan_limit=10,
                start_position=position,
                output_limit=one_item_limit,
            )
            collected.extend(item["name"] for item in items)
            if next_position is None:
                break
            position = next_position
        self.assertEqual(
            ["root", "a" * 20, "b" * 20, "c" * 20],
            collected,
        )

    def test_deep_cursor_unwinds_to_each_remaining_ancestor_sibling(self):
        root = Node(
            "root",
            children=[
                Node(
                    "a",
                    children=[
                        Node("a1", children=[Node("a1x")]),
                        Node("a2"),
                    ],
                ),
                Node("b"),
            ],
        )
        collected = []
        position: tuple[int, ...] = ()
        while True:
            items, next_position, _ = page(
                root,
                max_depth=3,
                page_size=1,
                scan_limit=1,
                start_position=position,
            )
            collected.extend(item["name"] for item in items)
            if next_position is None:
                break
            position = next_position
        self.assertEqual(
            ["root", "a", "a1", "a1x", "a2", "b"],
            collected,
        )

    def test_cursor_integrity_query_fence_and_lineage_detect_mutation(self):
        root = Node(
            "root",
            children=[Node("a"), Node("b"), Node("c")],
        )
        position = (2,)
        payload = {
            "v": 1,
            "s": "studio-a",
            "d": "epoch-a",
            "g": 3,
            "q": "q" * 64,
            "p": list(position),
            "l": lineage(root, position),
        }
        cursor = encode_cursor(payload)
        self.assertEqual(payload, decode_cursor(cursor))
        tampered = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
        with self.assertRaisesRegex(ValueError, "integrity"):
            decode_cursor(tampered)

        root.children.insert(1, Node("aa"))
        self.assertNotEqual(payload["l"], lineage(root, position))

        source = HANDLERS.read_text(encoding="utf-8")
        for fence in (
            "payload.s ~= peer.studio_id",
            "payload.d ~= DOCUMENT_EPOCH",
            "payload.g ~= peer.generation",
            "payload.q ~= querySha256",
            "treePositionLineage(root, startPosition)",
        ):
            self.assertIn(fence, source)

    def test_mixed_class_duplicate_names_and_overdeep_paths_fail_closed(self):
        root = Node(
            "root",
            children=[
                Node("Thing", class_name="Folder"),
                Node("Thing", class_name="Model"),
            ],
        )
        with self.assertRaisesRegex(ValueError, "duplicate exact-path name"):
            full_preorder(root, 1)

        source = HANDLERS.read_text(encoding="utf-8")
        self.assertIn(
            "#rootPath + maxDepth > DURABLE_BOUNDS.MAX_PATH_SEGMENTS",
            source,
        )
        self.assertIn(
            "if previous.Name == current.Name then",
            source,
        )
        self.assertIn("tree_name_unsupported", source)


if __name__ == "__main__":
    unittest.main()
