from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


MAX_PATH_SEGMENTS = 64
MAX_PATH_SEGMENT_BYTES = 100
MAX_CHILDREN = 10_000
MAX_QUERY_BYTES = 256
MAX_KEYWORDS = 8
MAX_KEYWORD_BYTES = 64
MAX_SOURCE_BYTES = 262_144
MAX_SOURCE_LINES = 20_000
MAX_PREVIEW_BYTES = 512
SORT_VERSION = "name-class-v1"
SEARCH_DOMAIN = "studio-script-search-cursor-v1"
GREP_DOMAIN = "studio-script-grep-cursor-v1"
SEARCH_SEMANTICS = (
    "all_keywords_ascii_case_insensitive_literal_subsequence"
)
SCRIPT_CLASSES = frozenset(("Script", "LocalScript", "ModuleScript"))
ASCII_FOLD_TABLE = bytes.maketrans(
    bytes(range(256)),
    bytes(
        value + 32 if ord("A") <= value <= ord("Z") else value
        for value in range(256)
    ),
)


class InvalidCursor(ValueError):
    pass


class StaleCursor(ValueError):
    pass


@dataclass
class Node:
    name: str
    class_name: str = "Folder"
    source: Optional[Union[str, bytes]] = None
    children: List["Node"] = field(default_factory=list)


@dataclass(frozen=True)
class Entry:
    node: Node
    path: Tuple[str, ...]
    position: Tuple[int, ...]
    depth: int


@dataclass(frozen=True)
class SessionFence:
    studio_id: str
    client_instance_id: str
    document_epoch: str
    generation: int


@dataclass
class Page:
    items: List[Dict[str, Any]]
    cursor: Optional[str]
    stop_reason: str
    scanned: int
    source_bytes: int = 0


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def ascii_fold(value: bytes) -> bytes:
    return value.translate(ASCII_FOLD_TABLE)


def validate_segment(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("path segment must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("path segment is not valid UTF-8") from exc
    if not 1 <= len(encoded) <= MAX_PATH_SEGMENT_BYTES:
        raise ValueError("path segment byte bound")
    if any(byte < 32 or byte == 127 for byte in encoded):
        raise ValueError("path segment control byte")
    return encoded


def validate_root_and_depth(
    root_path: Tuple[str, ...], max_depth: int
) -> None:
    if not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth")
    if len(root_path) + max_depth > MAX_PATH_SEGMENTS:
        raise ValueError("root plus depth exceeds reusable path bound")
    for segment in root_path:
        validate_segment(segment)


def sorted_children(node: Node) -> List[Node]:
    if len(node.children) > MAX_CHILDREN:
        raise ValueError("child bound")
    ordered = sorted(
        node.children,
        key=lambda child: (
            validate_segment(child.name),
            validate_segment(child.class_name),
        ),
    )
    for previous, current in zip(ordered, ordered[1:]):
        if previous.name == current.name:
            raise ValueError("duplicate sibling exact-path name")
    return ordered


def deterministic_entries(
    root: Node,
    root_path: Tuple[str, ...],
    max_depth: int,
) -> List[Entry]:
    validate_root_and_depth(root_path, max_depth)
    validate_segment(root.name)
    entries: List[Entry] = []
    stack = [(root, root_path, (), 0)]
    while stack:
        node, path, position, depth = stack.pop()
        validate_segment(node.name)
        if len(path) > MAX_PATH_SEGMENTS:
            raise ValueError("path bound")
        entries.append(Entry(node, path, position, depth))
        if depth >= max_depth:
            continue
        children = sorted_children(node)
        for index in range(len(children), 0, -1):
            child = children[index - 1]
            child_path = path + (child.name,)
            if len(child_path) > MAX_PATH_SEGMENTS:
                raise ValueError("path bound")
            stack.append(
                (
                    child,
                    child_path,
                    position + (index,),
                    depth + 1,
                )
            )
    return entries


def position_lineage(root: Node, position: Tuple[int, ...]) -> str:
    values: List[Any] = [
        "studio-script-lineage-v1",
        SORT_VERSION,
        root.name,
        root.class_name,
    ]
    current = root
    for ordinal in position:
        children = sorted_children(current)
        if ordinal < 1 or ordinal > len(children):
            raise StaleCursor("position no longer exists")
        first = max(1, ordinal - 2)
        last = min(len(children), ordinal + 2)
        values.extend((len(children), ordinal, first, last))
        for index in range(first, last + 1):
            child = children[index - 1]
            values.extend((index, child.name, child.class_name))
        current = children[ordinal - 1]
    values.extend((current.name, current.class_name))
    canonical = "".join(
        "{}:{};".format(len(str(value)), value) for value in values
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_keywords(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("keywords must be text")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("keywords must be printable ASCII") from exc
    if not 1 <= len(raw) <= MAX_QUERY_BYTES:
        raise ValueError("keywords total byte bound")
    if any(byte < 32 or byte > 126 for byte in raw):
        raise ValueError("keywords must be printable ASCII")

    result: List[str] = []
    seen = set()
    for part in value.split(","):
        token = part.strip(" ")
        token_bytes = token.encode("ascii")
        if not 1 <= len(token_bytes) <= MAX_KEYWORD_BYTES:
            raise ValueError("keyword token byte bound")
        folded = token.lower()
        if folded in seen:
            raise ValueError("duplicate normalized keyword")
        seen.add(folded)
        result.append(folded)
        if len(result) > MAX_KEYWORDS:
            raise ValueError("too many keywords")
    return tuple(result)


def validate_literal_query(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("query must be text")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("query must be printable ASCII") from exc
    if not 1 <= len(encoded) <= MAX_QUERY_BYTES:
        raise ValueError("query byte bound")
    if any(byte < 32 or byte > 126 for byte in encoded):
        raise ValueError("query must be printable ASCII")
    return encoded


def literal_ordered_subsequence(needle: bytes, haystack: bytes) -> bool:
    offset = 0
    for byte in haystack:
        if offset < len(needle) and byte == needle[offset]:
            offset += 1
    return offset == len(needle)


def script_name_matches(node: Node, keywords: Iterable[str]) -> bool:
    if node.class_name not in SCRIPT_CLASSES:
        return False
    folded_name = ascii_fold(node.name.encode("utf-8"))
    return all(
        literal_ordered_subsequence(
            ascii_fold(keyword.encode("ascii")),
            folded_name,
        )
        for keyword in keywords
    )


class CursorCodec:
    def __init__(self, secret: bytes = b"phase2-script-model-secret"):
        self.secret = secret

    def encode(self, domain: str, payload: Dict[str, Any]) -> str:
        raw = canonical_json(payload)
        signature = hmac.new(
            self.secret,
            domain.encode("ascii") + b"\0" + raw,
            hashlib.sha256,
        ).hexdigest()
        return base64.b64encode(raw).decode("ascii") + "." + signature

    def decode(self, domain: str, cursor: str) -> Dict[str, Any]:
        if not isinstance(cursor, str):
            raise InvalidCursor("cursor must be text")
        try:
            encoded, supplied = cursor.split(".", 1)
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise InvalidCursor("cursor encoding") from exc
        expected = hmac.new(
            self.secret,
            domain.encode("ascii") + b"\0" + raw,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise InvalidCursor("cursor signature")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidCursor("cursor payload") from exc
        if not isinstance(payload, dict):
            raise InvalidCursor("cursor payload shape")
        return payload


def common_payload(
    *,
    kind: str,
    session: SessionFence,
    query_sha: str,
    limits_sha: str,
    position: Tuple[int, ...],
    lineage: str,
) -> Dict[str, Any]:
    return {
        "v": 1,
        "kind": kind,
        "studio_id": session.studio_id,
        "client_instance_id": session.client_instance_id,
        "document_epoch": session.document_epoch,
        "generation": session.generation,
        "query_sha256": query_sha,
        "limits_sha256": limits_sha,
        "sort": SORT_VERSION,
        "position": list(position),
        "lineage": lineage,
    }


def validate_common_payload(
    payload: Dict[str, Any],
    *,
    kind: str,
    session: SessionFence,
    query_sha: str,
    limits_sha: str,
    root: Node,
    extra_keys: Iterable[str] = (),
) -> Tuple[int, ...]:
    expected_keys = {
        "v",
        "kind",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "query_sha256",
        "limits_sha256",
        "sort",
        "position",
        "lineage",
    }.union(extra_keys)
    if set(payload) != expected_keys:
        raise InvalidCursor("cursor payload is not closed")
    if payload["v"] != 1 or payload["kind"] != kind:
        raise InvalidCursor("cursor version or kind")
    position_value = payload["position"]
    if (
        not isinstance(position_value, list)
        or len(position_value) > MAX_PATH_SEGMENTS
        or any(
            not isinstance(item, int) or item < 1
            for item in position_value
        )
    ):
        raise InvalidCursor("cursor traversal position")
    position = tuple(position_value)
    fences = {
        "studio_id": session.studio_id,
        "client_instance_id": session.client_instance_id,
        "document_epoch": session.document_epoch,
        "generation": session.generation,
        "query_sha256": query_sha,
        "limits_sha256": limits_sha,
        "sort": SORT_VERSION,
    }
    for key, expected in fences.items():
        if payload[key] != expected:
            raise StaleCursor("cursor {} fence".format(key))
    try:
        current_lineage = position_lineage(root, position)
    except (ValueError, StaleCursor) as exc:
        raise StaleCursor("cursor lineage position") from exc
    if payload["lineage"] != current_lineage:
        raise StaleCursor("cursor lineage changed")
    return position


def item_bytes(item: Dict[str, Any]) -> int:
    return len(canonical_json(item)) + 1


def index_for_position(
    entries: List[Entry], position: Tuple[int, ...]
) -> int:
    for index, entry in enumerate(entries):
        if entry.position == position:
            return index
    raise StaleCursor("cursor position is stale")


def search_page(
    root: Node,
    *,
    root_path: Tuple[str, ...],
    max_depth: int,
    keywords: str,
    session: SessionFence,
    codec: CursorCodec,
    page_size: int,
    scan_limit: int,
    time_steps: int,
    output_limit: int,
    cursor: Optional[str] = None,
) -> Page:
    normalized = parse_keywords(keywords)
    if min(page_size, scan_limit, time_steps, output_limit) < 1:
        raise ValueError("limits must be positive")
    query_sha = sha256_json(
        {
            "version": "script-name-query-v1",
            "root_path": list(root_path),
            "max_depth": max_depth,
            "keywords": list(normalized),
            "semantics": SEARCH_SEMANTICS,
        }
    )
    limits_sha = sha256_json(
        {
            "page_size": page_size,
            "scan_limit": scan_limit,
            "time_steps": time_steps,
            "output_limit": output_limit,
        }
    )
    entries = deterministic_entries(root, root_path, max_depth)
    if cursor is None:
        index = 0
    else:
        payload = codec.decode(SEARCH_DOMAIN, cursor)
        position = validate_common_payload(
            payload,
            kind="script_search",
            session=session,
            query_sha=query_sha,
            limits_sha=limits_sha,
            root=root,
        )
        index = index_for_position(entries, position)

    items: List[Dict[str, Any]] = []
    output_bytes = 0
    scanned = 0
    time_used = 0
    reason = "complete"
    while index < len(entries):
        if scanned >= scan_limit:
            reason = "scan"
            break
        if time_used >= time_steps:
            reason = "time"
            break
        entry = entries[index]
        scanned += 1
        time_used += 1
        if script_name_matches(entry.node, normalized):
            item = {
                "path": list(entry.path),
                "name": entry.node.name,
                "class_name": entry.node.class_name,
            }
            encoded_size = item_bytes(item)
            if output_bytes + encoded_size > output_limit:
                if not items:
                    raise ValueError("one search item exceeds output budget")
                reason = "output"
                break
            items.append(item)
            output_bytes += encoded_size
        index += 1
        if len(items) >= page_size:
            reason = "page"
            break

    next_cursor = None
    if index < len(entries):
        next_position = entries[index].position
        payload = common_payload(
            kind="script_search",
            session=session,
            query_sha=query_sha,
            limits_sha=limits_sha,
            position=next_position,
            lineage=position_lineage(root, next_position),
        )
        next_cursor = codec.encode(SEARCH_DOMAIN, payload)
    else:
        reason = "complete"
    return Page(items, next_cursor, reason, scanned)


def validate_source(value: Optional[Union[str, bytes]]) -> bytes:
    if value is None:
        raw = b""
    elif isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("source is not valid UTF-8") from exc
    elif isinstance(value, bytes):
        raw = value
    else:
        raise ValueError("source must be text")
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source byte bound")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source is not valid UTF-8") from exc
    if raw.count(b"\n") + 1 > MAX_SOURCE_LINES:
        raise ValueError("source line bound")
    return raw


def utf8_preview(
    source: bytes, start_zero: int, end_exclusive: int
) -> str:
    line_start = source.rfind(b"\n", 0, start_zero) + 1
    line_end = source.find(b"\n", end_exclusive)
    if line_end < 0:
        line_end = len(source)
    match_length = end_exclusive - start_zero
    left_budget = max(0, (MAX_PREVIEW_BYTES - match_length) // 2)
    window_start = max(line_start, start_zero - left_budget)
    window_end = min(line_end, window_start + MAX_PREVIEW_BYTES)
    if window_end < end_exclusive:
        window_end = end_exclusive
        window_start = max(line_start, window_end - MAX_PREVIEW_BYTES)

    while (
        window_start < start_zero
        and 0x80 <= source[window_start] <= 0xBF
    ):
        window_start += 1
    while (
        window_end > end_exclusive
        and window_end < len(source)
        and 0x80 <= source[window_end] <= 0xBF
    ):
        window_end -= 1
    preview = source[window_start:window_end]
    if len(preview) > MAX_PREVIEW_BYTES:
        raise AssertionError("preview model exceeded its byte bound")
    return preview.decode("utf-8")


def occurrence_item(
    entry: Entry,
    source: bytes,
    query_length: int,
    start_zero: int,
) -> Dict[str, Any]:
    line_start = source.rfind(b"\n", 0, start_zero) + 1
    return {
        "path": list(entry.path),
        "name": entry.node.name,
        "class_name": entry.node.class_name,
        "start_byte": start_zero + 1,
        "end_byte": start_zero + query_length,
        "line": source.count(b"\n", 0, start_zero) + 1,
        "column": start_zero - line_start + 1,
        "preview": utf8_preview(
            source,
            start_zero,
            start_zero + query_length,
        ),
    }


def all_literal_occurrences(
    source: bytes,
    query: bytes,
    *,
    case_sensitive: bool,
) -> List[int]:
    """Return deterministic left-to-right, non-overlapping byte starts."""
    searchable = source if case_sensitive else ascii_fold(source)
    needle = query if case_sensitive else ascii_fold(query)
    result = []
    next_start = 0
    while True:
        start = searchable.find(needle, next_start)
        if start < 0:
            return result
        result.append(start)
        next_start = start + len(needle)


def grep_page(
    root: Node,
    *,
    root_path: Tuple[str, ...],
    max_depth: int,
    query: str,
    case_sensitive: bool,
    session: SessionFence,
    codec: CursorCodec,
    page_size: int,
    scan_limit: int,
    source_byte_limit: int,
    time_steps: int,
    output_limit: int,
    cursor: Optional[str] = None,
) -> Page:
    query_bytes = validate_literal_query(query)
    if (
        min(
            page_size,
            scan_limit,
            source_byte_limit,
            time_steps,
            output_limit,
        )
        < 1
    ):
        raise ValueError("limits must be positive")
    if source_byte_limit < MAX_SOURCE_BYTES:
        raise ValueError("source budget must fit one maximum-size script")
    query_sha = sha256_json(
        {
            "version": "script-grep-query-v1",
            "root_path": list(root_path),
            "max_depth": max_depth,
            "query": query,
            "case_sensitive": case_sensitive,
            "literal": True,
            "occurrence_semantics": "non_overlapping_left_to_right",
        }
    )
    limits_sha = sha256_json(
        {
            "page_size": page_size,
            "scan_limit": scan_limit,
            "source_byte_limit": source_byte_limit,
            "time_steps": time_steps,
            "output_limit": output_limit,
        }
    )
    entries = deterministic_entries(root, root_path, max_depth)
    active: Optional[Dict[str, Any]] = None
    if cursor is None:
        index = 0
    else:
        payload = codec.decode(GREP_DOMAIN, cursor)
        position = validate_common_payload(
            payload,
            kind="script_grep",
            session=session,
            query_sha=query_sha,
            limits_sha=limits_sha,
            root=root,
            extra_keys=("active",),
        )
        index = index_for_position(entries, position)
        active_value = payload["active"]
        if active_value is not None:
            if (
                not isinstance(active_value, dict)
                or set(active_value) != {"source_sha256", "next_byte"}
                or not isinstance(active_value["source_sha256"], str)
                or len(active_value["source_sha256"]) != 64
                or not isinstance(active_value["next_byte"], int)
                or active_value["next_byte"] < 1
            ):
                raise InvalidCursor("grep active cursor shape")
            node = entries[index].node
            if node.class_name not in SCRIPT_CLASSES:
                raise StaleCursor("grep cursor no longer names a script")
            source = validate_source(node.source)
            if (
                hashlib.sha256(source).hexdigest()
                != active_value["source_sha256"]
            ):
                raise StaleCursor("grep source revision changed")
            if active_value["next_byte"] > len(source) + 1:
                raise InvalidCursor("grep next byte")
            active = dict(active_value)

    items: List[Dict[str, Any]] = []
    output_bytes = 0
    scanned = 0
    source_bytes_used = 0
    time_used = 0
    reason = "complete"
    while index < len(entries):
        entry = entries[index]
        node = entry.node
        if active is None:
            if scanned >= scan_limit:
                reason = "scan"
                break
            if node.class_name not in SCRIPT_CLASSES:
                if time_used >= time_steps:
                    reason = "time"
                    break
                scanned += 1
                time_used += 1
                index += 1
                continue
            source = validate_source(node.source)
            if source_bytes_used + len(source) > source_byte_limit:
                reason = "source"
                break
            scanned += 1
            source_bytes_used += len(source)
            active = {
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "next_byte": 1,
            }
        else:
            if scanned >= scan_limit:
                reason = "scan"
                break
            source = validate_source(node.source)
            if (
                hashlib.sha256(source).hexdigest()
                != active["source_sha256"]
            ):
                raise StaleCursor("grep source revision changed")
            if source_bytes_used + len(source) > source_byte_limit:
                reason = "source"
                break
            scanned += 1
            source_bytes_used += len(source)

        searchable = source if case_sensitive else ascii_fold(source)
        needle = (
            query_bytes
            if case_sensitive
            else ascii_fold(query_bytes)
        )
        final_start = len(source) - len(query_bytes)
        next_zero = active["next_byte"] - 1
        if final_start < 0:
            if time_used >= time_steps:
                reason = "time"
                break
            time_used += 1
            active = None
            index += 1
            continue

        while next_zero <= final_start:
            if time_used >= time_steps:
                active["next_byte"] = next_zero + 1
                reason = "time"
                break
            time_used += 1
            is_match = (
                searchable[next_zero : next_zero + len(needle)] == needle
            )
            if not is_match:
                next_zero += 1
                active["next_byte"] = next_zero + 1
                continue

            item = occurrence_item(
                entry,
                source,
                len(query_bytes),
                next_zero,
            )
            encoded_size = item_bytes(item)
            if output_bytes + encoded_size > output_limit:
                if not items:
                    raise ValueError("one grep item exceeds output budget")
                active["next_byte"] = next_zero + 1
                reason = "output"
                break
            items.append(item)
            output_bytes += encoded_size
            # The durable v1 contract resumes after the inclusive match end,
            # so literal occurrences are deliberately non-overlapping.
            next_zero += len(needle)
            active["next_byte"] = next_zero + 1
            if len(items) >= page_size:
                reason = "page"
                break

        if reason in {"time", "output", "page"}:
            break
        active = None
        index += 1

    next_cursor = None
    if index < len(entries):
        next_position = entries[index].position
        payload = common_payload(
            kind="script_grep",
            session=session,
            query_sha=query_sha,
            limits_sha=limits_sha,
            position=next_position,
            lineage=position_lineage(root, next_position),
        )
        payload["active"] = active
        next_cursor = codec.encode(GREP_DOMAIN, payload)
    else:
        reason = "complete"
    return Page(
        items,
        next_cursor,
        reason,
        scanned,
        source_bytes_used,
    )


def collect_pages(page_call, *, maximum_pages: int = 100_000):
    cursor = None
    items: List[Dict[str, Any]] = []
    reasons = set()
    for _ in range(maximum_pages):
        page = page_call(cursor)
        items.extend(page.items)
        reasons.add(page.stop_reason)
        if page.cursor is None:
            return items, reasons
        cursor = page.cursor
    raise AssertionError("reference pagination did not terminate")


def expected_search(
    root: Node,
    root_path: Tuple[str, ...],
    max_depth: int,
    keywords: str,
) -> List[Tuple[Tuple[str, ...], str]]:
    normalized = parse_keywords(keywords)
    return [
        (entry.path, entry.node.class_name)
        for entry in deterministic_entries(root, root_path, max_depth)
        if script_name_matches(entry.node, normalized)
    ]


def expected_grep(
    root: Node,
    root_path: Tuple[str, ...],
    max_depth: int,
    query: str,
    *,
    case_sensitive: bool,
) -> List[Tuple[Tuple[str, ...], int, int]]:
    query_bytes = validate_literal_query(query)
    result = []
    for entry in deterministic_entries(root, root_path, max_depth):
        if entry.node.class_name not in SCRIPT_CLASSES:
            continue
        source = validate_source(entry.node.source)
        for start in all_literal_occurrences(
            source,
            query_bytes,
            case_sensitive=case_sensitive,
        ):
            result.append(
                (
                    entry.path,
                    start + 1,
                    start + len(query_bytes),
                )
            )
    return result


SESSION = SessionFence(
    studio_id="11111111-1111-4111-8111-111111111111",
    client_instance_id="22222222-2222-4222-8222-222222222222",
    document_epoch="33333333-3333-4333-8333-333333333333",
    generation=7,
)


class ScriptSearchReferenceModelTests(unittest.TestCase):
    def test_keyword_parser_and_literal_ordered_subsequence_contract(self):
        self.assertEqual(
            ("door", "controller", "*.[?]"),
            parse_keywords(" Door , CONTROLLER, *.[?] "),
        )
        node = Node("D-o_O-r controller *.[?]", "ModuleScript")
        self.assertTrue(
            script_name_matches(
                node,
                parse_keywords("door,controller,*.[?]"),
            )
        )
        self.assertFalse(
            script_name_matches(
                Node("Door controller xyz", "Script"),
                parse_keywords("*"),
            )
        )
        self.assertFalse(
            script_name_matches(
                Node("A-C-B", "Script"),
                parse_keywords("abc"),
            )
        )
        self.assertFalse(
            script_name_matches(
                Node("DoorController", "Folder"),
                parse_keywords("door"),
            )
        )

        invalid = (
            "",
            " ",
            ",door",
            "door,",
            "door,,other",
            "door,DOOR",
            "x" * 65,
            ",".join("k{}".format(index) for index in range(9)),
            "line\nfeed",
            "unicodé",
            "x" * 257,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_keywords(value)

    def test_dfs_exact_paths_and_bounds_fail_closed(self):
        root = Node(
            "DataModel",
            children=[
                Node(
                    "Zed",
                    children=[Node("Nested", "Script", "return 1")],
                ),
                Node("Alpha", "LocalScript", "return 2"),
                Node("Middle", "ModuleScript", "return 3"),
            ],
        )
        entries = deterministic_entries(root, (), 2)
        self.assertEqual(
            [
                (),
                ("Alpha",),
                ("Middle",),
                ("Zed",),
                ("Zed", "Nested"),
            ],
            [entry.path for entry in entries],
        )
        self.assertEqual(
            [(), (1,), (2,), (3,), (3, 1)],
            [entry.position for entry in entries],
        )
        self.assertEqual(
            [(), ("Alpha",), ("Middle",), ("Zed",)],
            [
                entry.path
                for entry in deterministic_entries(root, (), 1)
            ],
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            deterministic_entries(
                Node(
                    "DataModel",
                    children=[
                        Node("Same", "Folder"),
                        Node("Same", "Script", ""),
                    ],
                ),
                (),
                1,
            )
        with self.assertRaisesRegex(ValueError, "child bound"):
            sorted_children(
                Node(
                    "TooWide",
                    children=[
                        Node("N{:05d}".format(index))
                        for index in range(MAX_CHILDREN + 1)
                    ],
                )
            )
        for bad_name in ("", "x" * 101, "bad\nname", "\ud800"):
            with self.subTest(bad_name=repr(bad_name)):
                with self.assertRaises(ValueError):
                    deterministic_entries(
                        Node("DataModel", children=[Node(bad_name)]),
                        (),
                        1,
                    )
        with self.assertRaisesRegex(ValueError, "root plus depth"):
            deterministic_entries(
                Node("DataModel"),
                tuple("P{:02d}".format(index) for index in range(63)),
                2,
            )

    def test_randomized_pagination_has_no_lost_or_duplicate_results(self):
        generator = random.Random(0x5C12A7)
        codec = CursorCodec()
        observed_reasons = set()
        for case in range(120):
            counter = 0

            def make_node(depth: int) -> Node:
                nonlocal counter
                counter += 1
                name = "Node-{:04d}-{}".format(
                    counter,
                    generator.choice(("Door", "Control", "Other", "*")),
                )
                class_name = generator.choice(
                    (
                        "Folder",
                        "Model",
                        "Script",
                        "LocalScript",
                        "ModuleScript",
                    )
                )
                node = Node(
                    name,
                    class_name,
                    "return {}".format(counter)
                    if class_name in SCRIPT_CLASSES
                    else None,
                )
                if depth:
                    for _ in range(generator.randint(0, 4)):
                        node.children.append(make_node(depth - 1))
                return node

            root = make_node(generator.randint(2, 5))
            max_depth = generator.randint(1, 5)
            keywords = generator.choice(
                ("door", "control", "do,or", "*", "absent")
            )
            expected = expected_search(root, (), max_depth, keywords)

            mode = case % 4
            page_size = 1 if mode == 0 else generator.randint(3, 10)
            scan_limit = (
                generator.randint(1, 3) if mode == 1 else 5_000
            )
            time_steps = (
                generator.randint(1, 3) if mode == 2 else 5_000
            )
            output_limit = 200_000
            if mode == 3 and expected:
                matching_sizes = [
                    item_bytes(
                        {
                            "path": list(path),
                            "name": path[-1] if path else root.name,
                            "class_name": class_name,
                        }
                    )
                    for path, class_name in expected
                ]
                output_limit = max(matching_sizes) + 8

            def call(next_cursor):
                return search_page(
                    root,
                    root_path=(),
                    max_depth=max_depth,
                    keywords=keywords,
                    session=SESSION,
                    codec=codec,
                    page_size=page_size,
                    scan_limit=scan_limit,
                    time_steps=time_steps,
                    output_limit=output_limit,
                    cursor=next_cursor,
                )

            actual_items, reasons = collect_pages(call)
            observed_reasons.update(reasons)
            actual = [
                (tuple(item["path"]), item["class_name"])
                for item in actual_items
            ]
            self.assertEqual(expected, actual, case)
            self.assertEqual(len(actual), len(set(actual)), case)

        self.assertTrue(
            {"page", "scan", "time", "output", "complete"}
            <= observed_reasons
        )

    def test_search_cursor_fences_every_context_and_domain(self):
        root = Node(
            "DataModel",
            children=[
                Node("AlphaScript", "Script", ""),
                Node("BetaScript", "Script", ""),
            ],
        )
        codec = CursorCodec()
        first = search_page(
            root,
            root_path=(),
            max_depth=1,
            keywords="script",
            session=SESSION,
            codec=codec,
            page_size=1,
            scan_limit=10,
            time_steps=10,
            output_limit=10_000,
        )
        self.assertIsNotNone(first.cursor)

        def resume(**overrides):
            arguments = {
                "root": root,
                "root_path": (),
                "max_depth": 1,
                "keywords": "script",
                "session": SESSION,
                "codec": codec,
                "page_size": 1,
                "scan_limit": 10,
                "time_steps": 10,
                "output_limit": 10_000,
                "cursor": first.cursor,
            }
            arguments.update(overrides)
            return search_page(**arguments)

        for field, changed_session in (
            (
                "studio",
                SessionFence(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    SESSION.client_instance_id,
                    SESSION.document_epoch,
                    SESSION.generation,
                ),
            ),
            (
                "client",
                SessionFence(
                    SESSION.studio_id,
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    SESSION.document_epoch,
                    SESSION.generation,
                ),
            ),
            (
                "document",
                SessionFence(
                    SESSION.studio_id,
                    SESSION.client_instance_id,
                    "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    SESSION.generation,
                ),
            ),
            (
                "generation",
                SessionFence(
                    SESSION.studio_id,
                    SESSION.client_instance_id,
                    SESSION.document_epoch,
                    SESSION.generation + 1,
                ),
            ),
        ):
            with self.subTest(field=field):
                with self.assertRaises(StaleCursor):
                    resume(session=changed_session)
        with self.assertRaises(StaleCursor):
            resume(keywords="alpha")
        with self.assertRaises(StaleCursor):
            resume(scan_limit=9)

        root.children.insert(1, Node("BetweenScript", "Script", ""))
        with self.assertRaises(StaleCursor):
            resume()

        tampered = first.cursor[:-1] + (
            "0" if first.cursor[-1] != "0" else "1"
        )
        with self.assertRaises(InvalidCursor):
            resume(cursor=tampered)
        with self.assertRaises(InvalidCursor):
            codec.decode(GREP_DOMAIN, first.cursor)

        old_sort = codec.decode(SEARCH_DOMAIN, first.cursor)
        old_sort["sort"] = "obsolete-sort-v0"
        with self.assertRaises(StaleCursor):
            resume(
                cursor=codec.encode(SEARCH_DOMAIN, old_sort),
            )
        stale_position = codec.decode(SEARCH_DOMAIN, first.cursor)
        stale_position["position"] = [MAX_CHILDREN + 1]
        with self.assertRaises(StaleCursor):
            resume(
                cursor=codec.encode(SEARCH_DOMAIN, stale_position),
            )


class ScriptGrepReferenceModelTests(unittest.TestCase):
    def test_literal_nonoverlap_offsets_lines_columns_and_utf8_preview(
        self,
    ):
        source = "éé NEEDLE café\nbananana\n" + ("界" * 300) + " needle"
        node = Node("SearchMe", "ModuleScript", source)
        entry = Entry(node, ("SearchMe",), (1,), 1)
        raw = validate_source(source)

        insensitive = all_literal_occurrences(
            raw,
            b"needle",
            case_sensitive=False,
        )
        self.assertEqual(
            [raw.index(b"NEEDLE"), raw.rindex(b"needle")],
            insensitive,
        )
        first = occurrence_item(entry, raw, len(b"needle"), insensitive[0])
        self.assertEqual(6, first["start_byte"])
        self.assertEqual(11, first["end_byte"])
        self.assertEqual(1, first["line"])
        self.assertEqual(6, first["column"])

        last = occurrence_item(entry, raw, len(b"needle"), insensitive[-1])
        self.assertEqual(3, last["line"])
        self.assertLessEqual(
            len(last["preview"].encode("utf-8")),
            MAX_PREVIEW_BYTES,
        )
        last["preview"].encode("utf-8").decode("utf-8")
        self.assertIn("needle", last["preview"])

        self.assertEqual(
            [0],
            all_literal_occurrences(
                b"ababa",
                b"aba",
                case_sensitive=True,
            ),
        )
        self.assertEqual(
            [],
            all_literal_occurrences(
                b"Needle",
                b"needle",
                case_sensitive=True,
            ),
        )
        self.assertEqual(
            [1],
            all_literal_occurrences(
                b"x.*[?]y",
                b".*[?]",
                case_sensitive=False,
            ),
        )

    def test_source_validation_fails_closed_on_size_utf8_and_lines(self):
        self.assertEqual(b"", validate_source(""))
        self.assertEqual(
            b"x" * MAX_SOURCE_BYTES,
            validate_source(b"x" * MAX_SOURCE_BYTES),
        )
        for source in (
            b"x" * (MAX_SOURCE_BYTES + 1),
            b"\xff",
            ("\n" * MAX_SOURCE_LINES).encode("ascii"),
            "\ud800",
        ):
            with self.subTest(source_type=type(source).__name__):
                with self.assertRaises(ValueError):
                    validate_source(source)

    def test_randomized_grep_pagination_has_no_loss_or_duplication(self):
        generator = random.Random(0x9A3E9)
        codec = CursorCodec()
        observed_reasons = set()
        for case in range(80):
            counter = 0

            def make_node(depth: int) -> Node:
                nonlocal counter
                counter += 1
                class_name = generator.choice(
                    (
                        "Folder",
                        "Script",
                        "LocalScript",
                        "ModuleScript",
                    )
                )
                pieces = [
                    generator.choice(
                        ("x", "Needle", "needle", "é", ".*", "\n")
                    )
                    for _ in range(generator.randint(0, 25))
                ]
                node = Node(
                    "N{:04d}".format(counter),
                    class_name,
                    "".join(pieces)
                    if class_name in SCRIPT_CLASSES
                    else None,
                )
                if depth:
                    for _ in range(generator.randint(0, 3)):
                        node.children.append(make_node(depth - 1))
                return node

            root = make_node(generator.randint(2, 4))
            max_depth = generator.randint(1, 4)
            query = generator.choice(("needle", ".*", "xx"))
            case_sensitive = bool(generator.getrandbits(1))
            expected = expected_grep(
                root,
                (),
                max_depth,
                query,
                case_sensitive=case_sensitive,
            )

            mode = case % 4
            page_size = 1 if mode == 0 else generator.randint(3, 12)
            scan_limit = (
                generator.randint(1, 3) if mode == 1 else 5_000
            )
            time_steps = (
                generator.randint(1, 5) if mode == 2 else 50_000
            )
            output_limit = 500_000
            if mode == 3 and expected:
                output_limit = 700

            def call(next_cursor):
                return grep_page(
                    root,
                    root_path=(),
                    max_depth=max_depth,
                    query=query,
                    case_sensitive=case_sensitive,
                    session=SESSION,
                    codec=codec,
                    page_size=page_size,
                    scan_limit=scan_limit,
                    source_byte_limit=MAX_SOURCE_BYTES,
                    time_steps=time_steps,
                    output_limit=output_limit,
                    cursor=next_cursor,
                )

            actual_items, reasons = collect_pages(call)
            observed_reasons.update(reasons)
            actual = [
                (
                    tuple(item["path"]),
                    item["start_byte"],
                    item["end_byte"],
                )
                for item in actual_items
            ]
            self.assertEqual(expected, actual, case)
            self.assertEqual(len(actual), len(set(actual)), case)
            for item in actual_items:
                self.assertGreaterEqual(item["line"], 1)
                self.assertGreaterEqual(item["column"], 1)
                self.assertLessEqual(
                    len(item["preview"].encode("utf-8")),
                    MAX_PREVIEW_BYTES,
                )

        self.assertTrue(
            {"page", "scan", "time", "output", "complete"}
            <= observed_reasons
        )

    def test_source_byte_and_output_stops_resume_exactly(self):
        source_a = ("x" * 131_070) + "hit"
        source_b = ("y" * 131_070) + "HIT"
        root = Node(
            "DataModel",
            children=[
                Node("A", "Script", source_a),
                Node("B", "LocalScript", source_b),
            ],
        )
        codec = CursorCodec()

        def source_limited(next_cursor):
            return grep_page(
                root,
                root_path=(),
                max_depth=1,
                query="hit",
                case_sensitive=False,
                session=SESSION,
                codec=codec,
                page_size=50,
                scan_limit=50,
                source_byte_limit=MAX_SOURCE_BYTES,
                time_steps=300_000,
                output_limit=500_000,
                cursor=next_cursor,
            )

        items, reasons = collect_pages(source_limited, maximum_pages=10)
        self.assertIn("source", reasons)
        self.assertEqual(
            [
                (("A",), len(source_a) - 2),
                (("B",), len(source_b) - 2),
            ],
            [
                (tuple(item["path"]), item["start_byte"])
                for item in items
            ],
        )

        dense = Node(
            "DataModel",
            children=[Node("Dense", "Script", "hit hit hit")],
        )
        sample_entry = deterministic_entries(dense, (), 1)[1]
        sample_item = occurrence_item(
            sample_entry,
            b"hit hit hit",
            3,
            0,
        )
        one_item_output = item_bytes(sample_item) + 4

        def output_limited(next_cursor):
            return grep_page(
                dense,
                root_path=(),
                max_depth=1,
                query="hit",
                case_sensitive=True,
                session=SESSION,
                codec=codec,
                page_size=50,
                scan_limit=50,
                source_byte_limit=MAX_SOURCE_BYTES,
                time_steps=1_000,
                output_limit=one_item_output,
                cursor=next_cursor,
            )

        items, reasons = collect_pages(output_limited)
        self.assertIn("output", reasons)
        self.assertEqual([1, 5, 9], [item["start_byte"] for item in items])

    def test_mid_script_cursor_source_and_all_context_fences(self):
        root = Node(
            "DataModel",
            children=[
                Node(
                    "Needles",
                    "ModuleScript",
                    "needle before needle after",
                )
            ],
        )
        codec = CursorCodec()

        def page_for(
            *,
            target_root=root,
            target_session=SESSION,
            target_query="needle",
            target_cursor=None,
            page_size=1,
        ):
            return grep_page(
                target_root,
                root_path=(),
                max_depth=1,
                query=target_query,
                case_sensitive=False,
                session=target_session,
                codec=codec,
                page_size=page_size,
                scan_limit=10,
                source_byte_limit=MAX_SOURCE_BYTES,
                time_steps=1_000,
                output_limit=10_000,
                cursor=target_cursor,
            )

        first = page_for()
        self.assertEqual([1], [item["start_byte"] for item in first.items])
        self.assertIsNotNone(first.cursor)
        payload = codec.decode(GREP_DOMAIN, first.cursor)
        self.assertIsInstance(payload["active"], dict)
        self.assertEqual(7, payload["active"]["next_byte"])
        self.assertEqual(
            hashlib.sha256(
                validate_source(root.children[0].source)
            ).hexdigest(),
            payload["active"]["source_sha256"],
        )

        resumed = page_for(target_cursor=first.cursor)
        self.assertEqual(
            [15],
            [item["start_byte"] for item in resumed.items],
        )

        root.children[0].source = "needle MUTATED needle"
        with self.assertRaises(StaleCursor):
            page_for(target_cursor=first.cursor)
        root.children[0].source = "needle before needle after"

        with self.assertRaises(StaleCursor):
            page_for(
                target_query="before",
                target_cursor=first.cursor,
            )
        with self.assertRaises(StaleCursor):
            page_for(
                target_session=SessionFence(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    SESSION.client_instance_id,
                    SESSION.document_epoch,
                    SESSION.generation,
                ),
                target_cursor=first.cursor,
            )
        with self.assertRaises(StaleCursor):
            page_for(target_cursor=first.cursor, page_size=2)

        tampered = first.cursor[:-1] + (
            "0" if first.cursor[-1] != "0" else "1"
        )
        with self.assertRaises(InvalidCursor):
            page_for(target_cursor=tampered)
        with self.assertRaises(InvalidCursor):
            codec.decode(SEARCH_DOMAIN, first.cursor)

    def test_time_cursor_advances_through_unmatched_bytes_without_loss(self):
        root = Node(
            "DataModel",
            children=[
                Node(
                    "Slow",
                    "Script",
                    "xxxxxxxxxxneedle--needle",
                )
            ],
        )
        codec = CursorCodec()

        def call(next_cursor):
            return grep_page(
                root,
                root_path=(),
                max_depth=1,
                query="needle",
                case_sensitive=True,
                session=SESSION,
                codec=codec,
                page_size=50,
                scan_limit=10,
                source_byte_limit=MAX_SOURCE_BYTES,
                time_steps=2,
                output_limit=10_000,
                cursor=next_cursor,
            )

        items, reasons = collect_pages(call)
        self.assertIn("time", reasons)
        self.assertEqual([11, 19], [item["start_byte"] for item in items])


if __name__ == "__main__":
    unittest.main()
