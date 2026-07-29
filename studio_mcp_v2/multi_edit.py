from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .errors import ValidationError


MAX_MULTI_EDIT_TARGETS = 16
MAX_MULTI_EDIT_EDITS_PER_TARGET = 64
MAX_MULTI_EDIT_EDITS = 128
MAX_MULTI_EDIT_LITERAL_BYTES = 262_144
MAX_MULTI_EDIT_ARGUMENT_BYTES = 350_000
MAX_MULTI_EDIT_SOURCE_BYTES = 262_144
MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES = 1_048_576
MAX_MULTI_EDIT_REPLACEMENT_SPANS = 1_024
MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES = 8_192
MAX_MULTI_EDIT_RECEIPT_BYTES = 100_000
MAX_PATH_SEGMENTS = 64
MAX_PATH_SEGMENT_BYTES = 100

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MULTI_EDIT_ORDERING_VERSION = "target-input-edit-input-v1"
MULTI_EDIT_ATOMICITY = (
    "preflight-all-per-target-cas-compensating-no-cross-script-atomicity-v1"
)
MULTI_EDIT_RECEIPT_CONTRACT = "broker-validated-downstream-ack-v1"


def _utf8(value: Any, label: str, *, maximum: int, allow_empty: bool) -> bytes:
    if not isinstance(value, str):
        raise ValidationError(label + " must be UTF-8 text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(label + " must be valid UTF-8") from exc
    if (not allow_empty and not encoded) or len(encoded) > maximum:
        qualifier = "non-empty " if not allow_empty else ""
        raise ValidationError(
            f"{label} must be {qualifier}UTF-8 text of at most {maximum} bytes"
        )
    return encoded


def _normalize_path(value: Any, label: str) -> Tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_PATH_SEGMENTS
    ):
        raise ValidationError(
            f"{label} must contain 1-{MAX_PATH_SEGMENTS} exact path segments"
        )
    normalized: List[str] = []
    for index, segment in enumerate(value):
        encoded = _utf8(
            segment,
            f"{label}[{index}]",
            maximum=MAX_PATH_SEGMENT_BYTES,
            allow_empty=False,
        )
        if any(unicodedata.category(character) == "Cc" for character in segment):
            raise ValidationError(f"{label}[{index}] contains a control character")
        if b"\x00" in encoded:
            raise ValidationError(f"{label}[{index}] contains a NUL byte")
        normalized.append(segment)
    return tuple(normalized)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValidationError("value is not bounded canonical UTF-8 JSON") from exc


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalize_multi_edit_arguments(arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValidationError("multi-edit arguments must be an object")
    if frozenset(arguments) != frozenset({"datamodel_type", "targets"}):
        raise ValidationError(
            "multi-edit requires exactly datamodel_type and targets"
        )
    if arguments.get("datamodel_type") != "Edit":
        raise ValidationError("multi-edit datamodel_type must be Edit")
    raw_targets = arguments.get("targets")
    if (
        not isinstance(raw_targets, list)
        or not 1 <= len(raw_targets) <= MAX_MULTI_EDIT_TARGETS
    ):
        raise ValidationError(
            f"targets must contain 1-{MAX_MULTI_EDIT_TARGETS} entries"
        )

    normalized_targets: List[Dict[str, Any]] = []
    seen_paths = set()
    edit_count = 0
    literal_bytes = 0
    aggregate_path_bytes = 0
    for target_index, raw_target in enumerate(raw_targets):
        if (
            not isinstance(raw_target, dict)
            or frozenset(raw_target)
            != frozenset({"path", "expected_sha256", "edits"})
        ):
            raise ValidationError(
                f"targets[{target_index}] has fields outside the closed schema"
            )
        path = _normalize_path(
            raw_target.get("path"), f"targets[{target_index}].path"
        )
        if path in seen_paths:
            raise ValidationError("multi-edit target paths must be unique")
        seen_paths.add(path)
        aggregate_path_bytes += sum(
            len(segment.encode("utf-8")) for segment in path
        )
        if aggregate_path_bytes > MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES:
            raise ValidationError(
                "aggregate multi-edit target paths exceed 8192 UTF-8 bytes"
            )
        expected_sha256 = raw_target.get("expected_sha256")
        if (
            not isinstance(expected_sha256, str)
            or SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise ValidationError(
                f"targets[{target_index}].expected_sha256 must be a "
                "lowercase SHA-256 digest"
            )
        raw_edits = raw_target.get("edits")
        if (
            not isinstance(raw_edits, list)
            or not 1 <= len(raw_edits) <= MAX_MULTI_EDIT_EDITS_PER_TARGET
        ):
            raise ValidationError(
                f"targets[{target_index}].edits must contain "
                f"1-{MAX_MULTI_EDIT_EDITS_PER_TARGET} entries"
            )
        normalized_edits: List[Dict[str, Any]] = []
        for edit_index, raw_edit in enumerate(raw_edits):
            if (
                not isinstance(raw_edit, dict)
                or not frozenset(raw_edit).issubset(
                    {
                        "old_string",
                        "new_string",
                        "replace_all",
                        "start_byte",
                        "end_byte",
                    }
                )
                or not {"old_string", "new_string"}.issubset(raw_edit)
            ):
                raise ValidationError(
                    f"targets[{target_index}].edits[{edit_index}] "
                    "has fields outside the closed schema"
                )
            old_string = raw_edit.get("old_string")
            new_string = raw_edit.get("new_string")
            old_bytes = _utf8(
                old_string,
                (
                    f"targets[{target_index}].edits[{edit_index}]"
                    ".old_string"
                ),
                maximum=MAX_MULTI_EDIT_LITERAL_BYTES,
                allow_empty=False,
            )
            new_bytes = _utf8(
                new_string,
                (
                    f"targets[{target_index}].edits[{edit_index}]"
                    ".new_string"
                ),
                maximum=MAX_MULTI_EDIT_LITERAL_BYTES,
                allow_empty=True,
            )
            if old_string == new_string:
                raise ValidationError(
                    f"targets[{target_index}].edits[{edit_index}] "
                    "must change the matched text"
                )
            replace_all = raw_edit.get("replace_all", False)
            if not isinstance(replace_all, bool):
                raise ValidationError(
                    f"targets[{target_index}].edits[{edit_index}]"
                    ".replace_all must be boolean"
                )
            has_start = "start_byte" in raw_edit
            has_end = "end_byte" in raw_edit
            if has_start is not has_end:
                raise ValidationError(
                    f"targets[{target_index}].edits[{edit_index}] "
                    "must provide start_byte and end_byte together"
                )
            start_byte = raw_edit.get("start_byte")
            end_byte = raw_edit.get("end_byte")
            if has_start:
                if replace_all:
                    raise ValidationError(
                        f"targets[{target_index}].edits[{edit_index}] "
                        "cannot combine byte offsets with replace_all"
                    )
                if (
                    not isinstance(start_byte, int)
                    or isinstance(start_byte, bool)
                    or not isinstance(end_byte, int)
                    or isinstance(end_byte, bool)
                    or start_byte < 0
                    or end_byte <= start_byte
                    or end_byte > MAX_MULTI_EDIT_SOURCE_BYTES
                    or end_byte - start_byte != len(old_bytes)
                ):
                    raise ValidationError(
                        f"targets[{target_index}].edits[{edit_index}] "
                        "byte offsets must be a bounded zero-based half-open "
                        "range exactly the UTF-8 byte length of old_string"
                    )
            literal_bytes += len(old_bytes) + len(new_bytes)
            if literal_bytes > MAX_MULTI_EDIT_LITERAL_BYTES:
                raise ValidationError(
                    "aggregate multi-edit literals exceed 262144 UTF-8 bytes"
                )
            edit_count += 1
            if edit_count > MAX_MULTI_EDIT_EDITS:
                raise ValidationError(
                    f"multi-edit may contain at most {MAX_MULTI_EDIT_EDITS} edits"
                )
            normalized_edit = {
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": replace_all,
            }
            if has_start:
                normalized_edit["start_byte"] = start_byte
                normalized_edit["end_byte"] = end_byte
            normalized_edits.append(normalized_edit)
        normalized_targets.append(
            {
                "path": list(path),
                "expected_sha256": expected_sha256,
                "edits": normalized_edits,
            }
        )

    normalized = {
        "datamodel_type": "Edit",
        "targets": normalized_targets,
    }
    if len(canonical_json_bytes(normalized)) > MAX_MULTI_EDIT_ARGUMENT_BYTES:
        raise ValidationError("encoded multi-edit arguments exceed 350000 bytes")
    return normalized


def _append_canonical(parts: List[str], value: Any) -> None:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ValidationError("multi-edit receipt contains a non-canonical value")
    encoded = text.encode("utf-8")
    parts.extend((str(len(encoded)), ":", text, ";"))


def _append_path(parts: List[str], path: Sequence[str]) -> None:
    _append_canonical(parts, len(path))
    for segment in path:
        _append_canonical(parts, segment)


def prepare_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for value in (
        "studio-multi-edit-prepare-v1",
        receipt["studio_id"],
        receipt["client_instance_id"],
        receipt["document_epoch"],
        receipt["generation"],
        receipt["request_id"],
        receipt["transaction_id"],
        receipt["ordering_version"],
        receipt["atomicity"],
        receipt["target_count"],
        receipt["edit_count"],
        receipt["aggregate_source_bytes"],
        receipt["aggregate_planned_source_bytes"],
    ):
        _append_canonical(parts, value)
    targets = receipt["targets"]
    _append_canonical(parts, len(targets))
    for target in targets:
        _append_canonical(parts, target["index"])
        _append_path(parts, target["path"])
        for field_name in (
            "expected_sha256",
            "prepared_sha256",
            "planned_sha256",
            "source_length",
            "planned_source_length",
            "edit_count",
            "replacement_count",
            "status",
        ):
            _append_canonical(parts, target[field_name])
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def mutation_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for value in (
        "studio-multi-edit-mutation-v1",
        receipt["phase"],
        receipt["studio_id"],
        receipt["client_instance_id"],
        receipt["document_epoch"],
        receipt["generation"],
        receipt["request_id"],
        receipt["transaction_id"],
        receipt["prepare_request_id"],
        receipt["prepare_sha256"],
        receipt["ordering_version"],
        receipt["atomicity"],
        receipt["receipt_contract"],
        receipt["outcome"],
        receipt["safe_terminal"],
        receipt["recovery_required"],
        receipt["target_count"],
        receipt["edit_count"],
    ):
        _append_canonical(parts, value)
    targets = receipt["targets"]
    _append_canonical(parts, len(targets))
    for target in targets:
        _append_canonical(parts, target["index"])
        _append_path(parts, target["path"])
        for field_name in (
            "expected_sha256",
            "prepared_sha256",
            "planned_sha256",
            "observed_before_sha256",
            "observed_after_sha256",
            "source_length",
            "planned_source_length",
            "edit_count",
            "replacement_count",
            "status",
        ):
            _append_canonical(parts, target[field_name])
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def public_arguments_sha256(arguments: Mapping[str, Any]) -> str:
    return canonical_json_sha256(copy.deepcopy(dict(arguments)))


def total_edit_count(targets: Iterable[Mapping[str, Any]]) -> int:
    return sum(len(target["edits"]) for target in targets)
