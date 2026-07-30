from __future__ import annotations

import copy
import hashlib
import json
import os
import pwd
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .catalog import ToolCatalog
from .errors import ValidationError


MAX_CATALOG_BYTES = 2_000_000
MAX_TOOLS = 512
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMPATIBILITY_MANIFEST = (
    ROOT / "config" / "upstream-compatibility-map.json"
)
DEFAULT_DURABLE_CATALOG = ROOT / "config" / "durable-tool-catalog.json"
DEFAULT_HANDLER_SOURCE = ROOT / "scripts" / "durable_operation_handlers.luau"
DEFAULT_UPSTREAM_BASELINE = ROOT / "config" / "tool-catalog.json"
INSTALLED_V1_CACHE_PARTS = (
    "Library",
    "Application Support",
    "StudioMCP",
    "tools-cache.json",
)

# A candidate may use this review-only extension to declare how an upstream
# addition maps into the deliberately smaller durable surface. This metadata
# never auto-enables or publishes a tool. The import command still requires an
# explicit reviewed approval and replaces only the stored upstream snapshot.
FAMILY_TO_DURABLE_HANDLER = {
    "state_read": "studio_get_state",
    "tree_read": "studio_list_tree",
    "instance_inspection": "studio_inspect_instance",
    "script_name_search": "studio_search_scripts",
    "script_content_search": "studio_grep_scripts",
    "script_read": "studio_read_script",
    "script_update": "studio_update_script",
    "multi_edit": "studio_multi_edit",
    "multi_edit_recovery": "studio_recover_multi_edit",
    "attribute_update": "studio_set_attribute",
    "console_read": "studio_get_console",
    "screenshot": "studio_capture_screenshot",
    "scriptable_input": "studio_fire_input_binding",
    "play_lifecycle": "studio_start_stop_play",
}

FAMILY_ALLOWED_ARGUMENTS = {
    "state_read": frozenset(),
    "tree_read": frozenset(
        {
            "root_path",
            "max_depth",
            "max_results",
            "name_filter",
            "class_filter",
            "class_is_a",
            "scan_limit",
            "page_size",
            "continuation_cursor",
        }
    ),
    "instance_inspection": frozenset(
        {
            "path",
            "child_limit",
            "descendant_max_depth",
            "descendant_scan_limit",
            "time_limit_ms",
        }
    ),
    "script_name_search": frozenset(
        {
            "keywords",
            "root_path",
            "max_depth",
            "scan_limit",
            "page_size",
            "time_limit_ms",
            "continuation_cursor",
        }
    ),
    "script_content_search": frozenset(
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
        }
    ),
    "script_read": frozenset({"path", "max_chars"}),
    "script_update": frozenset(
        {"path", "expected_sha256", "new_source"}
    ),
    "multi_edit": frozenset(
        {"datamodel_type", "targets", "creates"}
    ),
    "multi_edit_recovery": frozenset({"transaction_id"}),
    "attribute_update": frozenset(
        {
            "path",
            "name",
            "expected_exists",
            "expected_value_type",
            "expected_value",
            "value_type",
            "value",
        }
    ),
    "console_read": frozenset({"max_entries"}),
    "screenshot": frozenset({"max_width", "max_height"}),
    "scriptable_input": frozenset({"path", "state_type", "state"}),
    "play_lifecycle": frozenset({"is_start"}),
}


@dataclass(frozen=True)
class CompatibilityMapping:
    upstream_name: str
    family: str
    durable_handler: str


@dataclass(frozen=True)
class CompatibilityManifest:
    manifest_version: str
    durable_catalog_version: str
    schema_policy: str
    durable_handler_schema_sha256: Mapping[str, str]
    durable_handler_output_schema_sha256: Mapping[str, str]
    mappings: Mapping[str, CompatibilityMapping]


@dataclass(frozen=True)
class CatalogChange:
    kind: str
    name: str
    prior_name: Optional[str] = None
    family: Optional[str] = None
    durable_handler: Optional[str] = None
    compatibility: str = "review_required"

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "compatibility": self.compatibility,
        }
        if self.prior_name is not None:
            result["prior_name"] = self.prior_name
        if self.family is not None:
            result["family"] = self.family
        if self.durable_handler is not None:
            result["durable_handler"] = self.durable_handler
        return result


@dataclass(frozen=True)
class CatalogReview:
    baseline_sha256: str
    candidate_sha256: str
    changes: Tuple[CatalogChange, ...]
    fail_closed: bool

    def as_dict(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for change in self.changes:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        return {
            "baseline_sha256": self.baseline_sha256,
            "candidate_sha256": self.candidate_sha256,
            "counts": counts,
            "fail_closed": self.fail_closed,
            "changes": [change.as_dict() for change in self.changes],
        }


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number is forbidden: " + value)


def _validate_schema(
    schema: Any,
    tool_name: str,
    *,
    schema_name: str = "inputSchema",
) -> Dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValidationError(
            "Tool "
            + tool_name
            + " "
            + schema_name
            + " must be an object schema"
        )
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if (
        not isinstance(properties, dict)
        or len(properties) > 128
        or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in properties.items()
        )
    ):
        raise ValidationError(
            "Tool "
            + tool_name
            + " "
            + schema_name
            + " schema properties are invalid"
        )
    if (
        not isinstance(required, list)
        or len(required) > 128
        or any(not isinstance(item, str) for item in required)
        or len(set(required)) != len(required)
        or any(item not in properties for item in required)
    ):
        raise ValidationError(
            "Tool "
            + tool_name
            + " "
            + schema_name
            + " schema required list is invalid"
        )
    # A canonicalization pass also rejects values JSON cannot represent.
    try:
        _canonical(schema)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "Tool "
            + tool_name
            + " "
            + schema_name
            + " schema is not bounded JSON"
        ) from exc
    return schema


def validate_catalog_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Catalog must be a JSON object")
    tools = payload.get("tools")
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise ValidationError("Catalog tools must be a bounded array")
    try:
        _canonical(payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationError(
            "Catalog payload contains non-JSON data"
        ) from exc
    seen = set()
    validated_tools: List[Dict[str, Any]] = []
    for raw in tools:
        if not isinstance(raw, dict):
            raise ValidationError("Catalog tool entries must be objects")
        name = raw.get("name")
        if (
            not isinstance(name, str)
            or TOOL_NAME.fullmatch(name) is None
            or name in seen
        ):
            raise ValidationError("Catalog tool name is invalid or duplicated")
        seen.add(name)
        _validate_schema(raw.get("inputSchema"), name)
        if "outputSchema" in raw:
            _validate_schema(
                raw.get("outputSchema"),
                name,
                schema_name="outputSchema",
            )
        family = raw.get("x_studio_mcp_v2_family")
        if family is not None and not isinstance(family, str):
            raise ValidationError(
                "Tool " + name + " review family must be text"
            )
        renamed_from = raw.get("x_studio_mcp_v2_renamed_from")
        if renamed_from is not None and (
            not isinstance(renamed_from, str)
            or TOOL_NAME.fullmatch(renamed_from) is None
        ):
            raise ValidationError(
                "Tool " + name + " renamed-from value is invalid"
            )
        try:
            _canonical(raw)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError(
                "Tool " + name + " contains non-JSON data"
            ) from exc
        validated_tools.append(copy.deepcopy(raw))
    result = copy.deepcopy(payload)
    result["tools"] = validated_tools
    return result


def load_catalog(path: Path) -> Tuple[Dict[str, Any], bytes]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValidationError("Catalog path must be a regular local file")
    data = target.read_bytes()
    if not data or len(data) > MAX_CATALOG_BYTES:
        raise ValidationError("Catalog file is empty or exceeds the size bound")
    try:
        payload = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ValidationError("Catalog file must be valid UTF-8 JSON") from exc
    return validate_catalog_payload(payload), data


def load_compatibility_manifest(path: Path) -> CompatibilityManifest:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValidationError(
            "Compatibility manifest must be a regular local file"
        )
    data = target.read_bytes()
    if not data or len(data) > 256_000:
        raise ValidationError(
            "Compatibility manifest is empty or exceeds the size bound"
        )
    try:
        payload = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ValidationError(
            "Compatibility manifest must be valid UTF-8 JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "format",
            "manifest_version",
            "durable_catalog_version",
            "schema_policy",
            "durable_handler_schema_sha256",
            "durable_handler_output_schema_sha256",
            "mappings",
        }
        or payload.get("format")
        != "studio-mcp-v2-upstream-compatibility-map"
        or payload.get("manifest_version") != "3"
        or not isinstance(payload.get("durable_catalog_version"), str)
        or payload.get("schema_policy") != "exact_handler_contract"
        or not isinstance(payload.get("mappings"), list)
    ):
        raise ValidationError("Compatibility manifest header is invalid")
    schema_digests = payload["durable_handler_schema_sha256"]
    output_schema_digests = payload[
        "durable_handler_output_schema_sha256"
    ]
    expected_handlers = frozenset(FAMILY_TO_DURABLE_HANDLER.values())
    if (
        not isinstance(schema_digests, dict)
        or frozenset(schema_digests) != expected_handlers
        or any(
            not isinstance(handler, str)
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
            for handler, digest in schema_digests.items()
        )
    ):
        raise ValidationError(
            "Compatibility manifest must pin every durable handler schema "
            "to one exact lowercase SHA-256"
        )
    if (
        not isinstance(output_schema_digests, dict)
        or frozenset(output_schema_digests) != expected_handlers
        or any(
            not isinstance(handler, str)
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
            for handler, digest in output_schema_digests.items()
        )
    ):
        raise ValidationError(
            "Compatibility manifest must pin every durable handler output "
            "schema, including explicit absence, to one exact lowercase "
            "SHA-256"
        )
    mappings: Dict[str, CompatibilityMapping] = {}
    for raw in payload["mappings"]:
        if not isinstance(raw, dict) or set(raw) != {
            "upstream_name",
            "family",
            "durable_handler",
        }:
            raise ValidationError(
                "Compatibility mapping must use the exact closed fields"
            )
        upstream_name = raw["upstream_name"]
        family = raw["family"]
        handler = raw["durable_handler"]
        if (
            not isinstance(upstream_name, str)
            or TOOL_NAME.fullmatch(upstream_name) is None
            or upstream_name in mappings
            or family not in FAMILY_TO_DURABLE_HANDLER
            or handler != FAMILY_TO_DURABLE_HANDLER[family]
        ):
            raise ValidationError(
                "Compatibility mapping is unknown, duplicated, or mismatched"
            )
        mappings[upstream_name] = CompatibilityMapping(
            upstream_name=upstream_name,
            family=family,
            durable_handler=handler,
        )
    return CompatibilityManifest(
        manifest_version=payload["manifest_version"],
        durable_catalog_version=payload["durable_catalog_version"],
        schema_policy=payload["schema_policy"],
        durable_handler_schema_sha256=dict(schema_digests),
        durable_handler_output_schema_sha256=dict(
            output_schema_digests
        ),
        mappings=mappings,
    )


def installed_v1_cache_candidate() -> Path:
    """Resolve the exact legacy cache from the passwd database, never HOME."""

    current_uid = os.getuid()
    try:
        home = Path(pwd.getpwuid(current_uid).pw_dir)
    except (KeyError, TypeError) as exc:
        raise ValidationError(
            "Current user has no passwd-database home directory"
        ) from exc
    if not home.is_absolute():
        raise ValidationError(
            "Passwd-database home directory must be absolute"
        )
    candidate = home.joinpath(*INSTALLED_V1_CACHE_PARTS)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise ValidationError("Installed v1 tools cache is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != current_uid
    ):
        raise ValidationError(
            "Installed v1 tools cache is not a regular user-owned file"
        )
    load_catalog(candidate)
    return candidate


def audit_installed_v1_cache(
    *,
    baseline_path: Path = DEFAULT_UPSTREAM_BASELINE,
    compatibility_manifest_path: Path = DEFAULT_COMPATIBILITY_MANIFEST,
    durable_catalog_path: Path = DEFAULT_DURABLE_CATALOG,
    max_changed_names: int = 64,
) -> Dict[str, Any]:
    """Return a bounded read-only diff; never return tool schemas or payloads."""

    if not 1 <= max_changed_names <= 128:
        raise ValueError("max_changed_names must be from 1 to 128")
    try:
        candidate = installed_v1_cache_candidate()
        baseline, baseline_bytes = load_catalog(Path(baseline_path))
        installed, installed_bytes = load_catalog(candidate)
        durable, _durable_bytes = load_catalog(Path(durable_catalog_path))
        manifest = load_compatibility_manifest(
            Path(compatibility_manifest_path)
        )
        review = review_catalogs(
            baseline,
            installed,
            baseline_bytes=baseline_bytes,
            candidate_bytes=installed_bytes,
            compatibility_manifest=manifest,
            durable_payload=durable,
        )
    except (OSError, ValidationError, ValueError):
        return {
            "available": False,
            "status": "unavailable_or_unsafe",
        }
    counts: Dict[str, int] = {}
    changed = []
    for change in review.changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
        if change.kind != "unchanged" and len(changed) < max_changed_names:
            changed.append(
                {
                    "name": change.name,
                    "kind": change.kind,
                    "compatibility": change.compatibility,
                }
            )
    cache_version = installed.get("catalog_version")
    if not isinstance(cache_version, str):
        raw_date = installed.get("date")
        if isinstance(raw_date, str) and 0 < len(raw_date) <= 160:
            cache_version = raw_date
        elif (
            isinstance(raw_date, int)
            and not isinstance(raw_date, bool)
            and 0 <= raw_date <= 9_999_999_999
        ):
            cache_version = "date-" + str(raw_date)
        else:
            cache_version = None
    return {
        "available": True,
        "status": "review_candidate",
        "path": str(candidate),
        "version": cache_version,
        "source_sha256": review.candidate_sha256,
        "tool_count": len(installed["tools"]),
        "counts": {
            "unchanged": counts.get("unchanged", 0),
            "removed": counts.get("removed", 0),
            "added": counts.get("new", 0),
            "renamed": counts.get("renamed", 0),
            "schema_changed": counts.get("schema_changed", 0),
            "output_schema_changed": counts.get(
                "output_schema_changed", 0
            ),
            "metadata_changed": counts.get("metadata_changed", 0),
        },
        "fail_closed": review.fail_closed,
        "changed": changed,
        "changed_truncated": sum(
            count
            for kind, count in counts.items()
            if kind != "unchanged"
        )
        > len(changed),
    }


def _tool_map(payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {tool["name"]: tool for tool in payload["tools"]}


def _output_contract(tool: Mapping[str, Any]) -> Any:
    """Return the reviewed output shape, using JSON null for explicit absence."""

    return tool["outputSchema"] if "outputSchema" in tool else None


def _family_compatibility(
    tool: Mapping[str, Any],
    manifest: Optional[CompatibilityManifest],
    durable_tools: Mapping[str, Mapping[str, Any]],
) -> Tuple[Optional[str], Optional[str], str]:
    if manifest is None:
        return None, None, "unknown_family"
    mapping = manifest.mappings.get(str(tool.get("name", "")))
    if mapping is None:
        return None, None, "unknown_family"
    family = mapping.family
    handler = mapping.durable_handler
    allowed = FAMILY_ALLOWED_ARGUMENTS.get(family)
    durable = durable_tools.get(handler)
    if allowed is None or durable is None:
        return family, None, "unknown_family"
    properties = set(tool["inputSchema"].get("properties", {}))
    if not properties.issubset(allowed):
        return family, handler, "incompatible_schema"
    if "studio_id" in properties:
        # Upstream snapshots describe remote operations. Explicit studio_id is
        # injected only by the local v2 ToolCatalog publication boundary.
        return family, handler, "incompatible_schema"
    if _canonical(tool["inputSchema"]) != _canonical(
        durable["inputSchema"]
    ):
        return family, handler, "incompatible_schema"
    if _canonical(_output_contract(tool)) != _canonical(
        _output_contract(durable)
    ):
        return family, handler, "incompatible_output_schema"
    return family, handler, "compatible_candidate"


def review_catalogs(
    baseline_payload: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
    *,
    baseline_bytes: Optional[bytes] = None,
    candidate_bytes: Optional[bytes] = None,
    compatibility_manifest: Optional[CompatibilityManifest] = None,
    durable_payload: Optional[Mapping[str, Any]] = None,
) -> CatalogReview:
    baseline = validate_catalog_payload(dict(baseline_payload))
    candidate = validate_catalog_payload(dict(candidate_payload))
    old = _tool_map(baseline)
    new = _tool_map(candidate)
    durable_tools = (
        _tool_map(validate_catalog_payload(dict(durable_payload)))
        if durable_payload is not None
        else {}
    )
    old_names = set(old)
    new_names = set(new)
    removed = old_names - new_names
    added = new_names - old_names
    changes: List[CatalogChange] = []

    renamed_old = set()
    renamed_new = set()
    for name in sorted(added):
        prior_name = new[name].get("x_studio_mcp_v2_renamed_from")
        if prior_name is None:
            continue
        if prior_name not in removed or prior_name in renamed_old:
            changes.append(
                CatalogChange(
                    "renamed",
                    name,
                    prior_name=str(prior_name),
                    compatibility="invalid_rename_claim",
                )
            )
            renamed_new.add(name)
            continue
        family, handler, compatibility = _family_compatibility(
            new[name],
            compatibility_manifest,
            durable_tools,
        )
        changes.append(
            CatalogChange(
                "renamed",
                name,
                prior_name=prior_name,
                family=family,
                durable_handler=handler,
                compatibility=compatibility,
            )
        )
        renamed_old.add(prior_name)
        renamed_new.add(name)

    for name in sorted(removed - renamed_old):
        changes.append(CatalogChange("removed", name))
    for name in sorted(added - renamed_new):
        family, handler, compatibility = _family_compatibility(
            new[name],
            compatibility_manifest,
            durable_tools,
        )
        changes.append(
            CatalogChange(
                "new",
                name,
                family=family,
                durable_handler=handler,
                compatibility=compatibility,
            )
        )
    for name in sorted(old_names & new_names):
        old_schema = _canonical(old[name]["inputSchema"])
        new_schema = _canonical(new[name]["inputSchema"])
        old_output_schema = _canonical(_output_contract(old[name]))
        new_output_schema = _canonical(_output_contract(new[name]))
        schema_changed = old_schema != new_schema
        output_schema_changed = old_output_schema != new_output_schema
        if schema_changed:
            family, handler, compatibility = _family_compatibility(
                new[name],
                compatibility_manifest,
                durable_tools,
            )
            changes.append(
                CatalogChange(
                    "schema_changed",
                    name,
                    family=family,
                    durable_handler=handler,
                    compatibility=compatibility,
                )
            )
        elif output_schema_changed:
            family, handler, compatibility = _family_compatibility(
                new[name],
                compatibility_manifest,
                durable_tools,
            )
            changes.append(
                CatalogChange(
                    "output_schema_changed",
                    name,
                    family=family,
                    durable_handler=handler,
                    compatibility=compatibility,
                )
            )
        else:
            old_metadata = {
                key: value
                for key, value in old[name].items()
                if key not in {"inputSchema", "outputSchema"}
            }
            new_metadata = {
                key: value
                for key, value in new[name].items()
                if key not in {"inputSchema", "outputSchema"}
            }
            if _canonical(old_metadata) != _canonical(new_metadata):
                changes.append(
                    CatalogChange("metadata_changed", name)
                )
            else:
                changes.append(
                    CatalogChange(
                        "unchanged",
                        name,
                        compatibility="unchanged",
                    )
                )

    baseline_data = baseline_bytes or _canonical(baseline).encode("utf-8")
    candidate_data = candidate_bytes or _canonical(candidate).encode("utf-8")
    blocking = {
        "unknown_family",
        "incompatible_schema",
        "incompatible_output_schema",
        "invalid_rename_claim",
    }
    fail_closed = any(
        change.compatibility in blocking for change in changes
    )
    return CatalogReview(
        baseline_sha256=_sha256(baseline_data),
        candidate_sha256=_sha256(candidate_data),
        changes=tuple(changes),
        fail_closed=fail_closed,
    )


def validate_durable_contract(
    durable_payload: Mapping[str, Any],
    *,
    compatibility_manifest: CompatibilityManifest,
    handler_source_path: Path = DEFAULT_HANDLER_SOURCE,
) -> Dict[str, Any]:
    durable = validate_catalog_payload(dict(durable_payload))
    if durable.get("format") != "studio-mcp-v2-durable-catalog":
        raise ValidationError("Generated durable catalog format is invalid")
    if (
        durable.get("catalog_version")
        != compatibility_manifest.durable_catalog_version
    ):
        raise ValidationError(
            "Generated durable catalog version does not match the manifest"
        )
    expected_handlers = frozenset(FAMILY_TO_DURABLE_HANDLER.values())
    durable_tools = _tool_map(durable)
    actual_handlers = frozenset(durable_tools)
    if actual_handlers != expected_handlers:
        raise ValidationError(
            "Generated catalog changed the exact durable handler allowlist"
        )
    for family, handler in FAMILY_TO_DURABLE_HANDLER.items():
        schema = durable_tools[handler]["inputSchema"]
        properties = schema.get("properties")
        if (
            set(schema)
            != {
                "type",
                "properties",
                "required",
                "additionalProperties",
            }
            or schema.get("type") != "object"
            or not isinstance(properties, dict)
            or set(properties) != FAMILY_ALLOWED_ARGUMENTS[family]
            or schema.get("additionalProperties") is not False
        ):
            raise ValidationError(
                "Generated durable "
                + family
                + " schema changed its exact closed argument contract"
            )
        actual_schema_sha256 = _sha256(
            _canonical(schema).encode("utf-8")
        )
        if (
            compatibility_manifest.durable_handler_schema_sha256.get(
                handler
            )
            != actual_schema_sha256
        ):
            raise ValidationError(
                "Generated durable "
                + handler
                + " input schema does not match its reviewed SHA-256"
            )
        output_schema = _output_contract(durable_tools[handler])
        actual_output_schema_sha256 = _sha256(
            _canonical(output_schema).encode("utf-8")
        )
        if (
            compatibility_manifest
            .durable_handler_output_schema_sha256.get(handler)
            != actual_output_schema_sha256
        ):
            raise ValidationError(
                "Generated durable "
                + handler
                + " output schema does not match its reviewed SHA-256"
            )
    handler_path = Path(handler_source_path)
    if handler_path.is_symlink() or not handler_path.is_file():
        raise ValidationError(
            "Durable handler source must be a regular local file"
        )
    handler_source = handler_path.read_text(encoding="utf-8")
    for handler in expected_handlers:
        if handler_source.count(
            'request.operation == "' + handler + '"'
        ) != 1:
            raise ValidationError(
                "Durable handler source does not implement exactly " + handler
            )
    catalog = ToolCatalog(durable["tools"])
    exposed = catalog.tools_for_mcp()
    for tool in exposed:
        schema = tool.get("inputSchema")
        if (
            not isinstance(schema, dict)
            or "studio_id" not in schema.get("required", [])
            or schema.get("properties", {})
            .get("studio_id", {})
            .get("format")
            != "uuid"
        ):
            raise ValidationError(
                "Generated operational schema lacks explicit studio_id"
            )
    raw_names = set(actual_handlers)
    if any(
        mapping.upstream_name in raw_names
        and mapping.upstream_name != mapping.durable_handler
        for mapping in compatibility_manifest.mappings.values()
    ):
        raise ValidationError(
            "Generated catalog exposed a raw upstream alias"
        )
    upstream = durable.get("upstream")
    if (
        not isinstance(upstream, dict)
        or not isinstance(upstream.get("version"), str)
        or not upstream["version"]
        or not isinstance(upstream.get("source_sha256"), str)
        or SHA256.fullmatch(upstream["source_sha256"]) is None
        or upstream.get("compatibility")
        not in {
            "reviewed-local-subset-only",
            "reviewed-exact-handler-mapping",
        }
    ):
        raise ValidationError(
            "Generated durable upstream provenance is invalid"
        )
    return {
        "catalog_version": durable["catalog_version"],
        "remote_names": sorted(actual_handlers),
        "mcp_names": sorted(tool["name"] for tool in exposed),
        "all_operations_require_studio_id": True,
        "handler_allowlist_unchanged": True,
        "closed_handler_schemas": True,
        "closed_handler_contracts": True,
        "reviewed_handler_schema_count": len(expected_handlers),
        "reviewed_handler_output_schema_count": len(
            expected_handlers
        ),
        "schema_policy": compatibility_manifest.schema_policy,
    }


def regenerate_durable_catalog(
    durable_payload: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
    candidate_bytes: bytes,
    review: CatalogReview,
    compatibility_manifest: CompatibilityManifest,
) -> Dict[str, Any]:
    durable = validate_catalog_payload(dict(durable_payload))
    candidate = validate_catalog_payload(dict(candidate_payload))
    durable_tools = _tool_map(durable)
    candidate_tools = _tool_map(candidate)
    generated_from = []
    generated_pairs = set()
    for change in review.changes:
        if change.compatibility != "compatible_candidate":
            continue
        mapping = compatibility_manifest.mappings.get(change.name)
        if mapping is None or mapping.durable_handler != change.durable_handler:
            raise ValidationError(
                "Reviewed compatibility mapping changed during generation"
            )
        source_tool = candidate_tools.get(change.name)
        target_tool = durable_tools.get(mapping.durable_handler)
        if source_tool is None or target_tool is None:
            raise ValidationError(
                "Reviewed compatibility source or handler disappeared"
            )
        generation_pair = (change.name, mapping.durable_handler)
        if generation_pair in generated_pairs:
            continue
        generated_pairs.add(generation_pair)
        # Exact-handler input and output compatibility were already checked by
        # review_catalogs. Copy only inputSchema. outputSchema, names,
        # descriptions, annotations, and handler identity remain
        # operator-owned local policy and are never copied from upstream.
        target_tool["inputSchema"] = copy.deepcopy(source_tool["inputSchema"])
        generated_from.append(
            {
                "upstream_name": change.name,
                "durable_handler": mapping.durable_handler,
                "change_kind": change.kind,
                "schema_policy": compatibility_manifest.schema_policy,
            }
        )
    candidate_sha256 = _sha256(candidate_bytes)
    upstream_version = candidate.get("catalog_version")
    if not isinstance(upstream_version, str) or not upstream_version:
        upstream_version = "sha256-" + candidate_sha256[:12]
    durable["upstream"] = {
        "version": upstream_version,
        "source_sha256": candidate_sha256,
        "compatibility": "reviewed-exact-handler-mapping",
    }
    durable["compatibility_generation"] = sorted(
        generated_from,
        key=lambda item: (
            item["durable_handler"],
            item["upstream_name"],
        ),
    )
    validate_durable_contract(
        durable,
        compatibility_manifest=compatibility_manifest,
    )
    return durable


def _catalog_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=False)
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix="." + path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def prepare_catalog_import(
    baseline_path: Path,
    candidate_path: Path,
    *,
    compatibility_manifest_path: Path = DEFAULT_COMPATIBILITY_MANIFEST,
    durable_catalog_path: Path = DEFAULT_DURABLE_CATALOG,
    regenerate_durable: bool = True,
) -> Dict[str, Any]:
    """Validate an import and generated catalog without writing any file."""

    baseline_payload, baseline_bytes = load_catalog(Path(baseline_path))
    candidate_payload, candidate_bytes = load_catalog(Path(candidate_path))
    durable_payload, durable_bytes = load_catalog(Path(durable_catalog_path))
    manifest = load_compatibility_manifest(
        Path(compatibility_manifest_path)
    )
    review = review_catalogs(
        baseline_payload,
        candidate_payload,
        baseline_bytes=baseline_bytes,
        candidate_bytes=candidate_bytes,
        compatibility_manifest=manifest,
        durable_payload=durable_payload,
    )
    if review.fail_closed:
        raise ValidationError(
            "Catalog contains an unknown or incompatible operation family"
        )
    contract = validate_durable_contract(
        durable_payload,
        compatibility_manifest=manifest,
    )
    generated_sha256 = _sha256(durable_bytes)
    generated_from = []
    if regenerate_durable:
        generated = regenerate_durable_catalog(
            durable_payload,
            candidate_payload,
            candidate_bytes,
            review,
            manifest,
        )
        generated_bytes = _catalog_bytes(generated)
        contract = validate_durable_contract(
            json.loads(generated_bytes.decode("utf-8")),
            compatibility_manifest=manifest,
        )
        generated_sha256 = _sha256(generated_bytes)
        generated_from = copy.deepcopy(
            generated.get("compatibility_generation", [])
        )
    return {
        "ready": True,
        "mutated": False,
        "review": review.as_dict(),
        "contract": contract,
        "current_durable_sha256": _sha256(durable_bytes),
        "generated_durable_sha256": generated_sha256,
        "generated_from": generated_from,
    }


def import_reviewed_catalog(
    baseline_path: Path,
    candidate_path: Path,
    *,
    approve_reviewed_changes: bool,
    expected_candidate_sha256: Optional[str] = None,
    compatibility_manifest_path: Path = DEFAULT_COMPATIBILITY_MANIFEST,
    durable_catalog_path: Optional[Path] = None,
    regenerate_durable: bool = False,
) -> Dict[str, Any]:
    destination = Path(baseline_path)
    candidate = Path(candidate_path)
    if destination.resolve() == candidate.resolve():
        raise ValidationError("Candidate and destination must be different files")
    baseline_payload, baseline_bytes = load_catalog(destination)
    candidate_payload, candidate_bytes = load_catalog(candidate)
    if expected_candidate_sha256 is not None:
        if (
            not isinstance(expected_candidate_sha256, str)
            or SHA256.fullmatch(expected_candidate_sha256) is None
        ):
            raise ValidationError(
                "Expected candidate SHA-256 must be a lowercase digest"
            )
        if _sha256(candidate_bytes) != expected_candidate_sha256:
            raise ValidationError(
                "Catalog candidate changed after checksum review"
            )
    compatibility_manifest = load_compatibility_manifest(
        compatibility_manifest_path
    )
    durable_target = Path(
        durable_catalog_path or DEFAULT_DURABLE_CATALOG
    )
    durable_payload, durable_bytes = load_catalog(durable_target)
    review = review_catalogs(
        baseline_payload,
        candidate_payload,
        baseline_bytes=baseline_bytes,
        candidate_bytes=candidate_bytes,
        compatibility_manifest=compatibility_manifest,
        durable_payload=durable_payload,
    )
    if review.fail_closed:
        raise ValidationError(
            "Catalog contains an unknown or incompatible operation family"
        )
    changed = any(change.kind != "unchanged" for change in review.changes)
    if changed and not approve_reviewed_changes:
        raise ValidationError(
            "Catalog changes require --approve-reviewed-changes"
        )
    # Re-validate immediately before mutation. The exact candidate bytes are
    # then copied from this validated read, never re-opened through a symlink.
    validate_catalog_payload(candidate_payload)
    generated_durable_bytes = None
    contract = validate_durable_contract(
        durable_payload,
        compatibility_manifest=compatibility_manifest,
    )
    if regenerate_durable:
        generated = regenerate_durable_catalog(
            durable_payload,
            candidate_payload,
            candidate_bytes,
            review,
            compatibility_manifest,
        )
        contract = validate_durable_contract(
            generated,
            compatibility_manifest=compatibility_manifest,
        )
        generated_durable_bytes = _catalog_bytes(generated)
        # Parse the exact serialized bytes before any destination mutation.
        validate_catalog_payload(
            json.loads(generated_durable_bytes.decode("utf-8"))
        )

    targets: List[Tuple[Path, bytes, bytes]] = [
        (destination, baseline_bytes, candidate_bytes)
    ]
    if generated_durable_bytes is not None:
        if durable_target.parent.resolve() != destination.parent.resolve():
            raise ValidationError(
                "Transactional catalog targets must share one directory"
            )
        targets.append(
            (durable_target, durable_bytes, generated_durable_bytes)
        )
    backups: List[Tuple[Path, Path, bytes]] = []
    for target, prior_bytes, _replacement in targets:
        prior_sha = _sha256(prior_bytes)
        backup = target.with_name(
            target.name + ".backup-" + prior_sha[:12]
        )
        _atomic_write(backup, prior_bytes)
        backups.append((target, backup, prior_bytes))

    installed = []
    try:
        for target, _prior_bytes, replacement in targets:
            _atomic_write(target, replacement)
            installed_payload, installed_bytes = load_catalog(target)
            validate_catalog_payload(installed_payload)
            if _sha256(installed_bytes) != _sha256(replacement):
                raise ValidationError(
                    "Atomic catalog replacement hash mismatch"
                )
            installed.append(
                {
                    "target": target.name,
                    "installed_sha256": _sha256(installed_bytes),
                }
            )
    except Exception:
        for target, _backup, prior_bytes in backups:
            _atomic_write(target, prior_bytes)
        raise

    receipt_payload = {
        "format": "studio-mcp-v2-catalog-import-receipt",
        "candidate_sha256": review.candidate_sha256,
        "entries": [
            {
                "target": target.name,
                "backup": backup.name,
                "prior_sha256": _sha256(prior_bytes),
                "installed_sha256": next(
                    item["installed_sha256"]
                    for item in installed
                    if item["target"] == target.name
                ),
            }
            for target, backup, prior_bytes in backups
        ],
    }
    transaction_prior_sha = _sha256(
        "\0".join(
            _sha256(prior_bytes)
            for _target, _backup, prior_bytes in backups
        ).encode("ascii")
    )
    receipt = destination.parent / (
        "catalog-import-receipt-"
        + review.candidate_sha256[:12]
        + "-"
        + transaction_prior_sha[:12]
        + ".json"
    )
    try:
        _atomic_write(receipt, _catalog_bytes(receipt_payload))
    except Exception:
        for target, _backup, prior_bytes in backups:
            _atomic_write(target, prior_bytes)
        raise
    return {
        "installed": str(destination),
        "durable_catalog": (
            str(durable_target) if generated_durable_bytes is not None else None
        ),
        "backups": [str(backup) for _target, backup, _prior in backups],
        "receipt": str(receipt),
        "contract": contract,
        "review": review.as_dict(),
    }


def rollback_catalog_import(receipt_path: Path) -> Dict[str, Any]:
    receipt = Path(receipt_path)
    if receipt.is_symlink() or not receipt.is_file():
        raise ValidationError("Rollback receipt must be a regular local file")
    raw = receipt.read_bytes()
    if not raw or len(raw) > 256_000:
        raise ValidationError("Rollback receipt is empty or too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "Rollback receipt must be valid UTF-8 JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format")
        != "studio-mcp-v2-catalog-import-receipt"
        or not isinstance(payload.get("entries"), list)
        or not 1 <= len(payload["entries"]) <= 2
    ):
        raise ValidationError("Rollback receipt header is invalid")
    root = receipt.parent.resolve()
    restores: List[Tuple[Path, bytes, bytes]] = []
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "target",
            "backup",
            "prior_sha256",
            "installed_sha256",
        }:
            raise ValidationError("Rollback receipt entry is invalid")
        target_name = entry["target"]
        backup_name = entry["backup"]
        if (
            not isinstance(target_name, str)
            or Path(target_name).name != target_name
            or not isinstance(backup_name, str)
            or Path(backup_name).name != backup_name
            or not isinstance(entry["prior_sha256"], str)
            or SHA256.fullmatch(entry["prior_sha256"]) is None
            or not isinstance(entry["installed_sha256"], str)
            or SHA256.fullmatch(entry["installed_sha256"]) is None
        ):
            raise ValidationError("Rollback receipt path or hash is invalid")
        target = receipt.parent / target_name
        backup = receipt.parent / backup_name
        if (
            target.parent.resolve() != root
            or backup.parent.resolve() != root
        ):
            raise ValidationError("Rollback target escaped the receipt directory")
        _payload, current_bytes = load_catalog(target)
        _backup_payload, backup_bytes = load_catalog(backup)
        if _sha256(current_bytes) != entry["installed_sha256"]:
            raise ValidationError(
                "Rollback refused because the installed catalog changed"
            )
        if _sha256(backup_bytes) != entry["prior_sha256"]:
            raise ValidationError("Rollback backup hash does not match receipt")
        restores.append((target, current_bytes, backup_bytes))

    pre_rollback = []
    for target, current_bytes, _backup_bytes in restores:
        path = target.with_name(
            target.name
            + ".pre-rollback-"
            + _sha256(current_bytes)[:12]
        )
        _atomic_write(path, current_bytes)
        pre_rollback.append(str(path))
    try:
        for target, _current_bytes, backup_bytes in restores:
            _atomic_write(target, backup_bytes)
            _payload, verified = load_catalog(target)
            if _sha256(verified) != _sha256(backup_bytes):
                raise ValidationError("Rollback replacement hash mismatch")
    except Exception:
        for target, current_bytes, _backup_bytes in restores:
            _atomic_write(target, current_bytes)
        raise
    return {
        "rolled_back": [str(target) for target, _current, _backup in restores],
        "pre_rollback_backups": pre_rollback,
        "receipt": str(receipt),
    }
