from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import math
import re
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, Optional, Set

from .errors import (
    JobNotFoundError,
    RemoteToolError,
    RequestTimeoutError,
    SessionConflictError,
    SessionDisconnectedError,
    StaleGenerationError,
    UnsafeCancellationError,
)
from .multi_edit import (
    MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES,
    MAX_MULTI_EDIT_RECEIPT_BYTES,
    MAX_MULTI_EDIT_EDITS,
    MAX_MULTI_EDIT_EDITS_PER_TARGET,
    MAX_MULTI_EDIT_REPLACEMENT_SPANS,
    MAX_MULTI_EDIT_SOURCE_BYTES,
    MAX_MULTI_EDIT_TARGETS,
    MULTI_EDIT_ATOMICITY,
    MULTI_EDIT_ORDERING_VERSION,
    MULTI_EDIT_RECEIPT_CONTRACT,
    SHA256_RE,
    canonical_json_bytes,
    canonical_json_sha256,
    mutation_receipt_sha256,
    normalize_multi_edit_arguments,
    prepare_receipt_sha256,
    total_edit_count,
)

_DURABLE_STATE_MODES = frozenset(
    {"edit", "starting", "play", "settling", "stopping", "unknown"}
)
_DURABLE_STATE_MODE_SOURCES = frozenset(
    {"controller_predicates", "play_transition"}
)
_DURABLE_STATE_RAW_PREDICATES = frozenset(
    {
        "is_studio",
        "is_edit",
        "is_running",
        "is_run_mode",
        "is_server",
        "is_client",
        "edit_mode_active",
    }
)
_DURABLE_STATE_KEYS = frozenset(
    {
        "adapter",
        "source",
        "connected",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "broker_instance_id",
        "run_id",
        "session_tag",
        "name",
        "place_id",
        "game_id",
        "mode",
        "is_edit",
        "mode_source",
        "controller_context",
        "available_datamodel_types",
        "raw_mode_predicates",
        "play",
    }
)
_DURABLE_CONTROLLER_PLAY_KEYS = frozenset({"active", "state"})
_DURABLE_CONTROLLER_LAST_PLAY_KEYS = frozenset(
    {"last_state", "last_outcome", "last_transition_nonce"}
)
_DURABLE_TRANSITION_PLAY_KEYS = frozenset(
    {
        "active",
        "state",
        "accepted",
        "server_ready",
        "runner_finished",
        "transition_nonce",
    }
)
_DURABLE_TRANSITION_OPTIONAL_PLAY_KEYS = frozenset(
    {"stop_command_id", "error"}
)
_DURABLE_TRANSITION_MODE_BY_STATE = {
    "starting": "starting",
    "play": "play",
    "stopping": "stopping",
    "settling": "settling",
    "recovery_required": "unknown",
}
_DURABLE_LAST_PLAY_OUTCOMES = frozenset(
    {
        "stopped_edit_confirmed",
        "natural_stop_edit_confirmed",
        "recovery_natural_stop_edit_confirmed",
        "start_failed_edit_confirmed",
    }
)
_DURABLE_PLAY_FAILURE_CODES = frozenset(
    {
        "request_blocked",
        "request_encode_invalid",
        "request_exception",
        "request_exception_http_disabled",
        "response_invalid",
        "response_non_success",
        "response_oversize",
        "envelope_invalid",
        "bridge_already_ended",
    }
)

MAX_SESSION_JOBS = 128
MAX_ACTIVE_SESSION_JOBS = 32
MAX_RETAINED_JOB_BYTES = 32_000_000
MAX_JOB_RESOLUTION_RECEIPTS = 4
MAX_JOB_RETIREMENT_TOMBSTONES = 64

_DURABLE_SCRIPT_ADAPTER = "studio-mcp-v2-durable-plugin"
_DURABLE_SCRIPT_CLASSES = frozenset(
    {"Script", "LocalScript", "ModuleScript"}
)
_DURABLE_SCRIPT_COMMON_RESULT_KEYS = frozenset(
    {
        "adapter",
        "v",
        "operation",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "request_id",
        "root_path",
        "sort_version",
        "max_depth",
        "scan_limit",
        "page_size",
        "time_limit_ms",
        "items",
        "returned",
        "scanned_instances",
        "scanned_scripts",
        "truncated",
        "has_more",
        "continuation_cursor",
        "truncation_reason",
        "output_limit_bytes",
    }
)
_DURABLE_SCRIPT_SEARCH_RESULT_KEYS = (
    _DURABLE_SCRIPT_COMMON_RESULT_KEYS
    | frozenset({"keywords", "match_semantics", "query_version"})
)
_DURABLE_SCRIPT_GREP_RESULT_KEYS = (
    _DURABLE_SCRIPT_COMMON_RESULT_KEYS
    | frozenset(
        {
            "query",
            "match_mode",
            "case_sensitive",
            "query_version",
            "source_byte_limit",
            "source_bytes_scanned",
        }
    )
)
_DURABLE_SCRIPT_SEARCH_ITEM_KEYS = frozenset(
    {"path", "name", "class_name"}
)
_DURABLE_SCRIPT_GREP_ITEM_KEYS = frozenset(
    {
        "path",
        "name",
        "class_name",
        "source_sha256",
        "source_length",
        "match_start_byte",
        "match_end_byte",
        "line_number",
        "column_byte",
        "preview_start_byte",
        "preview",
        "preview_prefix_truncated",
        "preview_suffix_truncated",
    }
)
_DURABLE_SCRIPT_SEARCH_ARGUMENT_KEYS = frozenset(
    {
        "keywords",
        "root_path",
        "max_depth",
        "scan_limit",
        "page_size",
        "time_limit_ms",
        "continuation_cursor",
    }
)
_DURABLE_SCRIPT_GREP_ARGUMENT_KEYS = frozenset(
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
)
_DURABLE_SCRIPT_CURSOR_RE = re.compile(
    r"^([A-Za-z0-9+/]+={0,2})\.([0-9a-f]{64})$"
)
_DURABLE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DURABLE_TREE_CURSOR_RE = re.compile(
    r"^([A-Za-z0-9+/]+={0,2})\.([0-9a-f]{64})$"
)
_DURABLE_TREE_ARGUMENT_KEYS = frozenset(
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
)
_DURABLE_TREE_RESULT_KEYS = frozenset(
    {
        "adapter",
        "v",
        "operation",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "request_id",
        "root_path",
        "items",
        "truncated",
        "has_more",
        "continuation_cursor",
        "truncation_reason",
        "max_depth",
        "max_results",
        "page_size",
        "scan_limit",
        "scanned",
        "returned",
        "output_bytes",
        "name_filter",
        "class_filter",
        "class_is_a",
        "sort_version",
        "output_limit_bytes",
    }
)
_DURABLE_TREE_ITEM_KEYS = frozenset(
    {"path", "name", "class_name", "child_count"}
)
_DURABLE_TREE_OUTPUT_LIMIT_BYTES = 600_000
_DURABLE_CLASS_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]{0,99}$"
)
_DURABLE_ATTRIBUTE_NAME_RE = re.compile(
    r"^[A-Za-z0-9._/-]{1,100}$"
)
_DURABLE_INSPECT_ARGUMENT_KEYS = frozenset(
    {
        "path",
        "child_limit",
        "descendant_max_depth",
        "descendant_scan_limit",
        "time_limit_ms",
    }
)
_DURABLE_INSPECT_RESULT_KEYS = frozenset(
    {
        "adapter",
        "v",
        "operation",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "request_id",
        "datamodel_type",
        "path",
        "name",
        "class_name",
        "snapshot_contract",
        "property_allowlist_version",
        "value_encoding_version",
        "sort_version",
        "child_limit",
        "descendant_max_depth",
        "descendant_scan_limit",
        "time_limit_ms",
        "properties",
        "property_count",
        "properties_complete",
        "attributes",
        "attributes_total",
        "attributes_returned",
        "attributes_truncated",
        "tags",
        "tags_total",
        "tags_returned",
        "tags_truncated",
        "children",
        "children_total",
        "children_returned",
        "children_truncated",
        "children_truncation_reason",
        "descendant_count",
        "descendant_count_complete",
        "descendant_truncation_reason",
        "descendant_class_counts",
        "output_limit_bytes",
    }
)
_DURABLE_INSPECT_PROPERTY_KEYS = frozenset({"selector", "value"})
_DURABLE_INSPECT_ATTRIBUTE_KEYS = frozenset({"name", "value"})
_DURABLE_INSPECT_CHILD_KEYS = frozenset(
    {"name", "class_name", "addressable", "path"}
)
_DURABLE_INSPECT_CLASS_COUNT_KEYS = frozenset(
    {"class_name", "count"}
)
_DURABLE_INSPECT_VALUE_KEYS = frozenset(
    {
        "type",
        "boolean_value",
        "number_value",
        "text",
        "numbers",
        "labels",
        "byte_length",
        "truncated",
    }
)
_DURABLE_INSPECT_VALUE_TYPES = frozenset(
    {
        "nil",
        "unavailable",
        "unsupported",
        "boolean",
        "number",
        "string",
        "enum",
        "vector2",
        "vector3",
        "color3",
        "cframe",
        "udim",
        "udim2",
        "rect",
        "brick_color",
        "number_range",
        "number_sequence",
        "color_sequence",
        "font",
    }
)
_DURABLE_INSPECT_PROPERTY_SELECTORS = frozenset(
    {
        "Instance.Archivable",
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
        "BaseScript.Enabled",
        "BaseScript.RunContext",
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
        "LayerCollector.Enabled",
        "LayerCollector.ResetOnSpawn",
        "LayerCollector.ZIndexBehavior",
    }
)
_DURABLE_INSPECT_PROPERTY_VALUE_TYPES = {
    "Instance.Archivable": "boolean",
    "BasePart.Anchored": "boolean",
    "BasePart.CanCollide": "boolean",
    "BasePart.CanQuery": "boolean",
    "BasePart.CanTouch": "boolean",
    "BasePart.CastShadow": "boolean",
    "BasePart.CFrame": "cframe",
    "BasePart.CollisionGroup": "string",
    "BasePart.Color": "color3",
    "BasePart.Locked": "boolean",
    "BasePart.Massless": "boolean",
    "BasePart.Material": "enum",
    "BasePart.MaterialVariant": "string",
    "BasePart.Reflectance": "number",
    "BasePart.Size": "vector3",
    "BasePart.Transparency": "number",
    "BaseScript.Enabled": "boolean",
    "BaseScript.RunContext": "enum",
    "GuiObject.Active": "boolean",
    "GuiObject.AnchorPoint": "vector2",
    "GuiObject.BackgroundColor3": "color3",
    "GuiObject.BackgroundTransparency": "number",
    "GuiObject.BorderColor3": "color3",
    "GuiObject.BorderSizePixel": "number",
    "GuiObject.ClipsDescendants": "boolean",
    "GuiObject.LayoutOrder": "number",
    "GuiObject.Position": "udim2",
    "GuiObject.Rotation": "number",
    "GuiObject.Size": "udim2",
    "GuiObject.Visible": "boolean",
    "GuiObject.ZIndex": "number",
    "LayerCollector.Enabled": "boolean",
    "LayerCollector.ResetOnSpawn": "boolean",
    "LayerCollector.ZIndexBehavior": "enum",
}
_DURABLE_INSPECT_PROPERTY_ENUM_FAMILIES = {
    "BasePart.Material": "Material",
    "BaseScript.RunContext": "RunContext",
    "LayerCollector.ZIndexBehavior": "ZIndexBehavior",
}
_DURABLE_INSPECT_PROPERTY_GROUPS = (
    frozenset(
        {
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
    ),
    frozenset({"BaseScript.Enabled", "BaseScript.RunContext"}),
    frozenset(
        {
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
    ),
    frozenset(
        {
            "LayerCollector.Enabled",
            "LayerCollector.ResetOnSpawn",
            "LayerCollector.ZIndexBehavior",
        }
    ),
)
_DURABLE_INSPECT_SENTINELS = {
    "boolean_value": False,
    "number_value": 0,
    "text": "",
    "numbers": [],
    "labels": [],
    "byte_length": 0,
    "truncated": False,
}
_DURABLE_INSPECT_OUTPUT_LIMIT_BYTES = 500_000
_DURABLE_MULTI_EDIT_PREPARE_KEYS = frozenset(
    {
        "adapter",
        "v",
        "operation",
        "phase",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "request_id",
        "transaction_id",
        "ordering_version",
        "atomicity",
        "target_count",
        "edit_count",
        "aggregate_source_bytes",
        "aggregate_planned_source_bytes",
        "targets",
        "expires_in_ms",
        "prepare_sha256",
    }
)
_DURABLE_MULTI_EDIT_PREPARED_TARGET_KEYS = frozenset(
    {
        "index",
        "path",
        "expected_sha256",
        "prepared_sha256",
        "planned_sha256",
        "source_length",
        "planned_source_length",
        "edit_count",
        "replacement_count",
        "status",
    }
)
_DURABLE_MULTI_EDIT_MUTATION_KEYS = frozenset(
    {
        "adapter",
        "v",
        "operation",
        "phase",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "request_id",
        "transaction_id",
        "prepare_request_id",
        "prepare_sha256",
        "ordering_version",
        "atomicity",
        "receipt_contract",
        "outcome",
        "safe_terminal",
        "recovery_required",
        "target_count",
        "edit_count",
        "targets",
        "receipt_sha256",
    }
)
_DURABLE_MULTI_EDIT_TARGET_OUTCOME_KEYS = frozenset(
    {
        "index",
        "path",
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
    }
)


class LongPollTransport:
    """One Studio connection generation's outbound request queue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def send(self, envelope: Dict[str, Any]) -> None:
        if self._closed:
            raise SessionDisconnectedError("Studio transport is closed")
        # The queue is intentionally unbounded and local. A synchronous
        # put_nowait makes the dispatch boundary atomic with respect to task
        # cancellation: either nothing was queued, or the caller resumes and
        # records the exact dispatched request before its next suspension.
        self._queue.put_nowait(copy.deepcopy(envelope))

    async def poll(self, timeout_seconds: float) -> Optional[Dict[str, Any]]:
        if self._closed and self._queue.empty():
            raise SessionDisconnectedError("Studio transport is closed")
        try:
            return await asyncio.wait_for(self._queue.get(), timeout_seconds)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._closed = True


@dataclass
class PendingRequest:
    request_id: str
    generation: int
    remote_tool: str
    arguments: Dict[str, Any]
    future: asyncio.Future


def _job_admitted_contract(
    remote_tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "contract_version": "studio-job-admission-v1",
        "operation": remote_tool,
    }
    if remote_tool == "studio_multi_edit":
        summary.update(
            {
                "datamodel_type": "Edit",
                "target_count": len(arguments["targets"]),
                "edit_count": total_edit_count(
                    arguments["targets"]
                ),
                "ordering_version": MULTI_EDIT_ORDERING_VERSION,
                "atomicity": MULTI_EDIT_ATOMICITY,
                "targets": [
                    {
                        "index": index,
                        "path": copy.deepcopy(target["path"]),
                        "expected_sha256": target[
                            "expected_sha256"
                        ],
                        "edit_count": len(target["edits"]),
                    }
                    for index, target in enumerate(
                        arguments["targets"], start=1
                    )
                ],
            }
        )
        return summary
    if remote_tool == "studio_list_tree":
        summary.update(
            {
                "root_path": copy.deepcopy(
                    arguments.get("root_path", [])
                ),
                "max_depth": arguments.get("max_depth", 2),
                "scan_limit": arguments.get("scan_limit", 2_000),
                "page_size": arguments.get(
                    "page_size",
                    arguments.get("max_results", 200),
                ),
            }
        )
    elif remote_tool in {
        "studio_search_scripts",
        "studio_grep_scripts",
    }:
        summary.update(
            {
                "root_path": copy.deepcopy(
                    arguments.get("root_path", [])
                ),
                "max_depth": arguments.get("max_depth"),
                "scan_limit": arguments.get("scan_limit", 2_000),
                "page_size": arguments.get("page_size"),
                "time_limit_ms": arguments.get("time_limit_ms"),
            }
        )
        query_value = arguments.get(
            "keywords", arguments.get("query", "")
        )
        summary["query_sha256"] = canonical_json_sha256(
            query_value
        )
    elif remote_tool == "studio_inspect_instance":
        summary.update(
            {
                "path": copy.deepcopy(arguments.get("path")),
                "child_limit": arguments.get("child_limit", 50),
                "descendant_max_depth": arguments.get(
                    "descendant_max_depth"
                ),
                "descendant_scan_limit": arguments.get(
                    "descendant_scan_limit", 2_000
                ),
                "time_limit_ms": arguments.get(
                    "time_limit_ms", 3_000
                ),
            }
        )
    else:
        summary["arguments_sha256"] = canonical_json_sha256(
            arguments
        )
    return summary


@dataclass
class OperationAdmission:
    sequence: int
    predecessor: asyncio.Future
    completion: asyncio.Future
    retired: bool = False

    def complete(self) -> None:
        if self.retired:
            return
        self.retired = True
        if not self.completion.done():
            self.completion.set_result(None)

    def retire_after_predecessor(self) -> None:
        if self.retired:
            return
        self.retired = True

        def release(_: asyncio.Future) -> None:
            if not self.completion.done():
                self.completion.set_result(None)

        if self.predecessor.done():
            release(self.predecessor)
        else:
            self.predecessor.add_done_callback(release)


@dataclass
class JobRecord:
    job_id: str
    studio_id: str
    generation: int
    public_tool: str
    remote_tool: str
    arguments: Dict[str, Any]
    timeout_ms: int
    status: str = "queued"
    dispatched: bool = False
    result: Any = None
    error: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    client_instance_id: str = ""
    document_epoch: str = ""
    input_schema_sha256: str = ""
    output_schema_sha256: str = ""
    handler_contract_sha256: str = ""
    arguments_sha256: str = ""
    admitted_contract: Dict[str, Any] = field(default_factory=dict)
    dispatched_request_ids: list[str] = field(default_factory=list)
    dispatched_phases: list[str] = field(default_factory=list)
    cancellation_state: str = "not_requested"
    terminal_outcome: Optional[str] = None
    result_sha256: Optional[str] = None
    result_bytes: Optional[int] = None
    transaction_id: Optional[str] = None
    admission_sequence: int = 0
    phase_receipts: list[Dict[str, Any]] = field(default_factory=list)
    resolution_receipts: list[Dict[str, Any]] = field(
        default_factory=list
    )
    admission: Optional[OperationAdmission] = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if (
            self.status == "completed"
            and self.result is not None
            and self.result_sha256 is None
        ):
            encoded = canonical_json_bytes(self.result)
            self.result_sha256 = canonical_json_sha256(self.result)
            self.result_bytes = len(encoded)

    def snapshot(self) -> Dict[str, Any]:
        terminal = self.status in {
            "completed",
            "failed",
            "cancelled",
        }
        result_present = self.result_sha256 is not None
        error_present = self.error is not None
        payload: Dict[str, Any] = {
            "format": "studio-mcp-v2-job-receipt",
            "schema_version": 1,
            "job_id": self.job_id,
            "studio_id": self.studio_id,
            "client_instance_id": self.client_instance_id,
            "document_epoch": self.document_epoch,
            "generation": self.generation,
            "tool_name": self.public_tool,
            "remote_tool": self.remote_tool,
            "input_schema_sha256": self.input_schema_sha256,
            "output_schema_sha256": self.output_schema_sha256,
            "handler_contract_sha256": self.handler_contract_sha256,
            "arguments_sha256": self.arguments_sha256,
            "transaction_id": self.transaction_id,
            "admitted_contract": copy.deepcopy(self.admitted_contract),
            "timeout_ms": self.timeout_ms,
            "admission_sequence": self.admission_sequence,
            "status": self.status,
            "dispatched": self.dispatched,
            "dispatched_request_ids": list(
                self.dispatched_request_ids
            ),
            "dispatched_phases": list(self.dispatched_phases),
            "phase_receipts": copy.deepcopy(self.phase_receipts),
            "resolution_receipts": copy.deepcopy(
                self.resolution_receipts
            ),
            "cancellation_state": self.cancellation_state,
            "terminal": terminal,
            "terminal_outcome": self.terminal_outcome,
            "result_present": result_present,
            "error_present": error_present,
            "result_sha256": self.result_sha256,
            "result_bytes": self.result_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if result_present:
            payload["result"] = copy.deepcopy(self.result)
        if error_present:
            payload["error"] = copy.deepcopy(self.error)
        return payload


class StudioSession:
    """All operational state and serialization for one explicit Studio ID."""

    TERMINAL_JOB_STATES = frozenset(
        {"completed", "failed", "cancelled"}
    )
    PLAY_BRIDGE_RECOVERY_TOOLS = frozenset(
        {
            "rnd_get_state",
            "rnd_play_stop",
            "rnd_shutdown",
            "studio_get_state",
            "studio_start_stop_play",
        }
    )

    def __init__(
        self,
        studio_id: str,
        client_instance_id: str,
        document_epoch: str,
        registration_secret_hash: bytes,
        resume_token_hash: bytes,
        bootstrap_resume_token: str,
        transport: LongPollTransport,
        metadata: Dict[str, Any],
        capabilities: Iterable[str],
    ) -> None:
        self.studio_id = studio_id
        self.client_instance_id = client_instance_id
        self.document_epoch = document_epoch
        self.registration_secret_hash = registration_secret_hash
        self.resume_token_hash = resume_token_hash
        self.bootstrap_resume_token: Optional[str] = bootstrap_resume_token
        # A reconnect rotates the resume credential before the HTTP response is
        # delivered. Until the replacement generation completes its first
        # authenticated poll, retain only the prior credential's hash plus the
        # exact reconnect settlement set. This permits one idempotent response
        # retry without accepting the old credential for operational routes.
        self.connect_retry_resume_token_hash: Optional[bytes] = None
        self.connect_retry_settled_request_ids: Optional[frozenset[str]] = None
        self.connect_retry_reconnect_id: Optional[str] = None
        self.used_reconnect_ids: Set[str] = set()
        self.generation = 1
        self.connected = True
        self.transport: Optional[LongPollTransport] = transport
        self.metadata = copy.deepcopy(metadata)
        self.capabilities: Set[str] = set(capabilities)
        self.mode = str(metadata.get("mode", "unknown"))
        self.last_confirmed_mode = self.mode.lower()
        self.uncertainty_state: Optional[str] = None
        self.play_bridge_uncertain: Optional[str] = None
        self.disconnected_at_monotonic: Optional[float] = None
        self.terminal_disconnect_candidate = False
        self.terminal_disconnect_reason: Optional[str] = None
        self.console: Deque[Dict[str, Any]] = deque(maxlen=1000)
        self.console_sequence = 0
        self.jobs: Dict[str, JobRecord] = {}
        self.job_retirement_count = 0
        self.job_retirement_chain_sha256 = "0" * 64
        self.job_retirement_tombstones: Deque[
            Dict[str, Any]
        ] = deque(maxlen=MAX_JOB_RETIREMENT_TOMBSTONES)
        self.pending: Dict[str, PendingRequest] = {}
        self.used_request_ids: Set[str] = set()
        # Dispatched calls whose terminal outcome has not been proven. New
        # operations are quarantined until the same-generation late response
        # arrives or a reconnecting Studio supplies its settlement ledger.
        self.uncertain_requests: Dict[str, Dict[str, Any]] = {}
        self.uncertain_pending: Dict[str, PendingRequest] = {}
        self.multi_edit_recovery: Optional[Dict[str, Any]] = None
        self.multi_edit_prepared_receipt: Optional[Dict[str, Any]] = None
        # Conservative v2 baseline: every Studio-bound operation is exclusive.
        self.operation_lock = asyncio.Lock()
        # Explicit admission chaining makes the order deterministic even when
        # a background job task has been created but has not yet reached the
        # operation lock. Each session owns its own chain, so different Studio
        # sessions remain concurrent.
        self._next_admission_sequence = 1
        self._admission_tail: Optional[asyncio.Future] = None
        self.last_seen_monotonic = time.monotonic()
        self.has_polled = False

    def reserve_operation(self) -> OperationAdmission:
        loop = asyncio.get_running_loop()
        predecessor = self._admission_tail
        if predecessor is None:
            predecessor = loop.create_future()
            predecessor.set_result(None)
        completion = loop.create_future()
        admission = OperationAdmission(
            sequence=self._next_admission_sequence,
            predecessor=predecessor,
            completion=completion,
        )
        self._next_admission_sequence += 1
        self._admission_tail = completion
        return admission

    def snapshot(self) -> Dict[str, Any]:
        return {
            "studio_id": self.studio_id,
            "client_instance_id": self.client_instance_id,
            "document_epoch": self.document_epoch,
            "generation": self.generation,
            "connected": self.connected,
            "metadata": copy.deepcopy(self.metadata),
            "capabilities": sorted(self.capabilities),
            "mode": self.mode,
            "last_confirmed_mode": self.last_confirmed_mode,
            "uncertainty_state": self.uncertainty_state,
            "play_bridge_uncertain": self.play_bridge_uncertain,
            "multi_edit_recovery": (
                copy.deepcopy(self.multi_edit_recovery)
                if self.multi_edit_recovery is not None
                else None
            ),
            "uncertain_request_count": len(self.uncertain_requests),
            "pending_count": len(self.pending),
            "job_counts": self._job_counts(),
            "job_retirement_count": self.job_retirement_count,
            "job_retirement_chain_sha256": (
                self.job_retirement_chain_sha256
            ),
            "job_retirement_tombstone_limit": (
                MAX_JOB_RETIREMENT_TOMBSTONES
            ),
            "job_retirement_tombstones": copy.deepcopy(
                list(self.job_retirement_tombstones)
            ),
            "console_sequence": self.console_sequence,
        }

    def _job_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for job in self.jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        return counts

    @staticmethod
    def _job_retained_bytes(job: JobRecord) -> int:
        try:
            return len(
                canonical_json_bytes(
                    {
                        "arguments": job.arguments,
                        "admitted_contract": job.admitted_contract,
                        "phase_receipts": job.phase_receipts,
                        "resolution_receipts": (
                            job.resolution_receipts
                        ),
                        "result": job.result,
                        "error": job.error,
                    }
                )
            )
        except ValidationError:
            return MAX_RETAINED_JOB_BYTES + 1

    def _retire_job(self, job: JobRecord) -> None:
        tombstone = {
            "job_id": job.job_id,
            "studio_id": job.studio_id,
            "client_instance_id": job.client_instance_id,
            "document_epoch": job.document_epoch,
            "generation": job.generation,
            "tool_name": job.public_tool,
            "remote_tool": job.remote_tool,
            "input_schema_sha256": job.input_schema_sha256,
            "output_schema_sha256": job.output_schema_sha256,
            "handler_contract_sha256": job.handler_contract_sha256,
            "arguments_sha256": job.arguments_sha256,
            "status": job.status,
            "terminal_outcome": job.terminal_outcome,
            "result_sha256": job.result_sha256,
            "result_bytes": job.result_bytes,
            "phase_receipts_sha256": canonical_json_sha256(
                job.phase_receipts
            ),
            "resolution_receipts_sha256": canonical_json_sha256(
                job.resolution_receipts
            ),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
        previous_chain_sha256 = self.job_retirement_chain_sha256
        next_chain_sha256 = canonical_json_sha256(
            {
                "previous_sha256": previous_chain_sha256,
                "retired": tombstone,
            }
        )
        tombstone["previous_chain_sha256"] = previous_chain_sha256
        tombstone["chain_sha256"] = next_chain_sha256
        self.job_retirement_chain_sha256 = next_chain_sha256
        self.job_retirement_tombstones.append(tombstone)
        self.job_retirement_count += 1
        self.jobs.pop(job.job_id, None)

    def _compact_terminal_jobs(
        self,
        *,
        required_bytes: int = 0,
        protected_job_id: Optional[str] = None,
    ) -> None:
        while True:
            retained_bytes = sum(
                self._job_retained_bytes(job)
                for job in self.jobs.values()
            )
            if (
                len(self.jobs) < MAX_SESSION_JOBS
                and retained_bytes + required_bytes
                <= MAX_RETAINED_JOB_BYTES
            ):
                return
            candidates = sorted(
                (
                    job
                    for job in self.jobs.values()
                    if job.status in self.TERMINAL_JOB_STATES
                    and job.job_id != protected_job_id
                ),
                key=lambda job: (
                    job.updated_at,
                    job.created_at,
                    job.job_id,
                ),
            )
            if not candidates:
                return
            self._retire_job(candidates[0])

    def _finalize_job_storage(self, job: JobRecord) -> None:
        if job.status in self.TERMINAL_JOB_STATES:
            job.arguments = {}
            job.admission = None
        self._compact_terminal_jobs(protected_job_id=job.job_id)

    def replace_connection(
        self,
        *,
        resume_token_hash: bytes,
        bootstrap_resume_token: str,
        retry_resume_token_hash: bytes,
        reconnect_id: str,
        transport: LongPollTransport,
        metadata: Dict[str, Any],
        capabilities: Iterable[str],
        settled_request_ids: Iterable[str],
    ) -> int:
        self._disconnect_current("Studio reconnected; old generation fenced")
        for request_id in settled_request_ids:
            if isinstance(request_id, str):
                pending = self.uncertain_pending.get(request_id)
                if (
                    pending is not None
                    and pending.remote_tool
                    in {
                        "studio_multi_edit",
                        "studio_recover_multi_edit",
                    }
                    and pending.arguments.get("_phase") != "prepare"
                ):
                    # A bare request ID proves only that the plugin stopped
                    # executing. It cannot prove a safe mutation outcome.
                    # Prepare is the deliberate exception: its handler is
                    # source-free and cannot dispatch a mutation.
                    continue
                if (
                    pending is not None
                    and pending.remote_tool == "studio_multi_edit"
                    and pending.arguments.get("_phase") == "prepare"
                ):
                    self._settle_safe_unapplied_job(
                        request_id,
                        "prepare_settled_after_reconnect_no_apply",
                    )
                elif pending is not None:
                    self._settle_safe_missing_receipt_job(request_id)
                self.uncertain_requests.pop(request_id, None)
                self.uncertain_pending.pop(request_id, None)
        self.generation += 1
        self.used_request_ids.clear()
        self.resume_token_hash = resume_token_hash
        self.bootstrap_resume_token = bootstrap_resume_token
        self.connect_retry_resume_token_hash = retry_resume_token_hash
        self.connect_retry_settled_request_ids = frozenset(settled_request_ids)
        self.connect_retry_reconnect_id = reconnect_id
        self.used_reconnect_ids.add(reconnect_id)
        self.transport = transport
        self.connected = True
        self.metadata = copy.deepcopy(metadata)
        self.capabilities = set(capabilities)
        self.mode = str(metadata.get("mode", "unknown"))
        self.last_confirmed_mode = self.mode.lower()
        self.disconnected_at_monotonic = None
        self.terminal_disconnect_candidate = False
        self.terminal_disconnect_reason = None
        self._refresh_uncertainty()
        self.console.clear()
        self.console_sequence = 0
        self.last_seen_monotonic = time.monotonic()
        self.has_polled = False
        return self.generation

    def mark_seen(self, *, polled: bool = False) -> None:
        self.last_seen_monotonic = time.monotonic()
        if polled:
            self.has_polled = True
            self.bootstrap_resume_token = None
            self.connect_retry_resume_token_hash = None
            self.connect_retry_settled_request_ids = None
            self.connect_retry_reconnect_id = None

    def lease_is_stale(self, lease_timeout_seconds: float) -> bool:
        return (
            time.monotonic() - self.last_seen_monotonic
            > lease_timeout_seconds
        )

    def disconnect(self, generation: int, reason: str) -> bool:
        if generation != self.generation:
            return False
        self._disconnect_current(reason)
        return True

    def _disconnect_current(self, reason: str) -> None:
        disconnect_mode = self.mode.lower()
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.connected = False
        self.last_confirmed_mode = disconnect_mode
        self.mode = "unknown"
        self.disconnected_at_monotonic = time.monotonic()
        self.terminal_disconnect_candidate = False
        self.terminal_disconnect_reason = str(reason)[:160]
        error = SessionDisconnectedError(reason)
        for pending in list(self.pending.values()):
            self.uncertain_requests[pending.request_id] = {
                "generation": pending.generation,
                "operation": pending.remote_tool,
                "reason": "connection_lost_after_dispatch",
            }
            self.uncertain_pending[pending.request_id] = pending
            if not pending.future.done():
                pending.future.set_exception(error)
        self.pending.clear()
        for job in self.jobs.values():
            if job.status not in self.TERMINAL_JOB_STATES:
                job.error = error.as_dict()
                job.updated_at = time.time()
                if (
                    job.dispatched
                    and job.remote_tool == "studio_multi_edit"
                    and "apply" not in job.dispatched_phases
                ):
                    job.status = "failed"
                    job.terminal_outcome = (
                        "prepare_interrupted_no_apply"
                    )
                elif job.dispatched:
                    job.status = "outcome_unknown"
                    job.terminal_outcome = "connection_lost_after_dispatch"
                else:
                    job.status = "failed"
                    job.terminal_outcome = (
                        "not_dispatched_connection_lost"
                    )
                if not job.dispatched and job.task is not None:
                    if job.admission is not None:
                        job.admission.retire_after_predecessor()
                    job.task.cancel()
        for job in list(self.jobs.values()):
            if job.status in self.TERMINAL_JOB_STATES:
                job.arguments = {}
                job.admission = None
        self._compact_terminal_jobs()
        self._recompute_terminal_disconnect_candidate()

    def _recompute_terminal_disconnect_candidate(self) -> None:
        if self.connected:
            return
        terminal_candidate = (
            self.last_confirmed_mode == "edit"
            and self.play_bridge_uncertain is None
            and self.multi_edit_recovery is None
            and not self.operation_lock.locked()
            and not self.pending
            and not self.uncertain_requests
            and all(
                job.status in self.TERMINAL_JOB_STATES
                for job in self.jobs.values()
            )
        )
        self.terminal_disconnect_candidate = terminal_candidate
        self._refresh_uncertainty(
            fallback=(
                None
                if terminal_candidate
                else self.terminal_disconnect_reason
            )
        )

    def assert_generation_online(
        self,
        admitted_generation: int,
        *,
        allow_uncertain: bool = False,
    ) -> None:
        if admitted_generation != self.generation:
            raise StaleGenerationError(
                "The operation was admitted before this Studio reconnected"
            )
        if not self.connected or self.transport is None:
            raise SessionDisconnectedError("The explicitly targeted Studio is offline")
        if self.uncertain_requests and not allow_uncertain:
            raise SessionConflictError(
                "This Studio is quarantined until prior dispatched request "
                "outcomes are reconciled"
            )

    def assert_operation_admissible(
        self,
        admitted_generation: int,
        remote_tool: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        operation_arguments = arguments or {}
        recovery_transaction_id = (
            self.multi_edit_recovery.get("transaction_id")
            if self.multi_edit_recovery is not None
            else None
        )
        uncertainty_ids_match = (
            set(self.uncertain_requests)
            == set(self.uncertain_pending)
        )
        recovery_generation = (
            self.multi_edit_recovery.get("generation")
            if self.multi_edit_recovery is not None
            else None
        )
        recovery_allowed = (
            remote_tool == "studio_recover_multi_edit"
            and self.multi_edit_recovery is not None
            and operation_arguments.get("transaction_id")
            == recovery_transaction_id
            and type(recovery_generation) is int
            and recovery_generation == admitted_generation
            and uncertainty_ids_match
            and all(
                pending.generation == recovery_generation
                and self.uncertain_requests[request_id].get(
                    "generation"
                )
                == recovery_generation
                and pending.arguments.get("transaction_id")
                == recovery_transaction_id
                and (
                    (
                        pending.remote_tool == "studio_multi_edit"
                        and pending.arguments.get("_phase") == "apply"
                    )
                    or (
                        pending.remote_tool
                        == "studio_recover_multi_edit"
                        and pending.arguments.get("_phase") is None
                    )
                )
                for request_id, pending in (
                    self.uncertain_pending.items()
                )
            )
        )
        self.assert_generation_online(
            admitted_generation,
            allow_uncertain=recovery_allowed,
        )
        if (
            self.multi_edit_recovery is not None
            and not recovery_allowed
        ):
            raise SessionConflictError(
                "This Studio has an exact multi-edit recovery fence; only "
                "the matching bounded recovery transaction is admitted"
            )
        if (
            self.play_bridge_uncertain is not None
            and remote_tool not in self.PLAY_BRIDGE_RECOVERY_TOOLS
        ):
            raise SessionConflictError(
                "This Studio is in Play bridge recovery; only bounded "
                "state, Stop, and shutdown operations are admitted"
            )

    def _refresh_uncertainty(self, fallback: Optional[str] = None) -> None:
        if self.uncertain_requests:
            self.uncertainty_state = (
                "outcome_unknown: "
                + ",".join(sorted(self.uncertain_requests))
            )
        else:
            self.uncertainty_state = fallback

    async def invoke(
        self,
        remote_tool: str,
        arguments: Dict[str, Any],
        timeout_ms: int,
        *,
        request_id: Optional[str] = None,
        expected_generation: Optional[int] = None,
        before_dispatch: Optional[Callable[[], None]] = None,
        on_dispatched: Optional[Callable[..., None]] = None,
        admission: Optional[OperationAdmission] = None,
    ) -> Any:
        admitted_generation = (
            self.generation
            if expected_generation is None
            else expected_generation
        )
        operation_admission = admission or self.reserve_operation()
        predecessor_reached = False
        try:
            await asyncio.shield(operation_admission.predecessor)
            predecessor_reached = True
            async with self.operation_lock:
                if (
                    remote_tool == "studio_multi_edit"
                    and "_phase" not in arguments
                ):
                    normalized = normalize_multi_edit_arguments(arguments)
                    return await self._invoke_multi_edit_locked(
                        normalized,
                        timeout_ms,
                        request_id=request_id,
                        admitted_generation=admitted_generation,
                        before_dispatch=before_dispatch,
                        on_dispatched=on_dispatched,
                    )
                return await self._invoke_locked(
                    remote_tool,
                    arguments,
                    timeout_ms,
                    request_id=request_id,
                    admitted_generation=admitted_generation,
                    before_dispatch=before_dispatch,
                    on_dispatched=on_dispatched,
                )
        finally:
            if predecessor_reached:
                operation_admission.complete()
            else:
                operation_admission.retire_after_predecessor()
            self._recompute_terminal_disconnect_candidate()

    async def _invoke_multi_edit_locked(
        self,
        arguments: Dict[str, Any],
        timeout_ms: int,
        *,
        request_id: Optional[str],
        admitted_generation: int,
        before_dispatch: Optional[Callable[[], None]],
        on_dispatched: Optional[Callable[..., None]],
    ) -> Any:
        transaction_id = str(uuid.uuid4())
        prepare_request_id = request_id or str(uuid.uuid4())
        apply_request_id = str(uuid.uuid4())
        started = time.monotonic()

        def remaining_timeout() -> int:
            elapsed_ms = int((time.monotonic() - started) * 1_000)
            remaining = timeout_ms - elapsed_ms
            if remaining < 1:
                raise RequestTimeoutError(
                    "Multi-edit exhausted its bounded total deadline before "
                    "the next phase",
                    details={
                        "transaction_id": transaction_id,
                        "phase": "between_phases",
                    },
                )
            return remaining

        prepare_args = {
            "_phase": "prepare",
            "transaction_id": transaction_id,
            "datamodel_type": "Edit",
            "targets": copy.deepcopy(arguments["targets"]),
        }
        phase = "prepare"
        phase_request_id = prepare_request_id
        apply_dispatched = False

        def observe_dispatched(
            operation_request_id: str,
            dispatched_phase: str,
            dispatched_transaction_id: Optional[str],
        ) -> None:
            nonlocal apply_dispatched
            if dispatched_phase == "apply":
                apply_dispatched = True
            if on_dispatched is not None:
                on_dispatched(
                    operation_request_id,
                    dispatched_phase,
                    dispatched_transaction_id,
                )

        try:
            prepared = await self._invoke_locked(
                "studio_multi_edit",
                prepare_args,
                remaining_timeout(),
                request_id=prepare_request_id,
                admitted_generation=admitted_generation,
                before_dispatch=before_dispatch,
                on_dispatched=observe_dispatched,
            )
            self.multi_edit_prepared_receipt = copy.deepcopy(prepared)
            phase = "apply"
            phase_request_id = apply_request_id
            apply_args = {
                "_phase": "apply",
                "transaction_id": transaction_id,
                "prepare_request_id": prepare_request_id,
                "prepare_sha256": prepared["prepare_sha256"],
                "prepared_targets": copy.deepcopy(prepared["targets"]),
            }
            result = await self._invoke_locked(
                "studio_multi_edit",
                apply_args,
                remaining_timeout(),
                request_id=apply_request_id,
                admitted_generation=admitted_generation,
                before_dispatch=before_dispatch,
                on_dispatched=observe_dispatched,
            )
            if result.get("recovery_required") is True:
                self._set_multi_edit_recovery(
                    transaction_id,
                    apply_request_id,
                    admitted_generation,
                    "receipt_requires_recovery",
                )
            return result
        except RemoteToolError as exc:
            if phase == "apply" and apply_dispatched:
                self._set_multi_edit_recovery(
                    transaction_id,
                    phase_request_id,
                    admitted_generation,
                    "apply_error_without_safe_receipt",
                )
                exc.details.update(
                    {
                        "transaction_id": transaction_id,
                        "phase": phase,
                        "request_id": phase_request_id,
                    }
                )
            elif phase == "apply":
                self.multi_edit_prepared_receipt = None
            raise
        except (
            RequestTimeoutError,
            SessionDisconnectedError,
            StaleGenerationError,
            asyncio.CancelledError,
        ) as exc:
            if phase == "apply" and apply_dispatched:
                self._set_multi_edit_recovery(
                    transaction_id,
                    phase_request_id,
                    admitted_generation,
                    "apply_outcome_unknown",
                )
            elif phase == "apply":
                # Prepare never mutates source. If apply was never sent, the
                # public operation is safely not applied and no compensating
                # recovery is authorized or necessary.
                self.multi_edit_prepared_receipt = None
            if hasattr(exc, "details"):
                exc.details.update(
                    {
                        "transaction_id": transaction_id,
                        "phase": phase,
                        "request_id": phase_request_id,
                    }
                )
            raise
        except Exception as exc:
            if phase == "apply" and apply_dispatched:
                self._set_multi_edit_recovery(
                    transaction_id,
                    phase_request_id,
                    admitted_generation,
                    "apply_exception_after_dispatch",
                )
            elif phase == "apply":
                # Admission, authorization, or local validation can fail
                # after a source-free prepare receipt but before apply is
                # sent. The prepared plan may remain in the plugin's bounded
                # cache, but no source mutation was dispatched and the host
                # must not retain a phantom recovery obligation.
                self.multi_edit_prepared_receipt = None
            if hasattr(exc, "details"):
                exc.details.update(
                    {
                        "transaction_id": transaction_id,
                        "phase": phase,
                        "request_id": phase_request_id,
                    }
                )
            raise

    def _set_multi_edit_recovery(
        self,
        transaction_id: str,
        request_id: str,
        generation: int,
        state: str,
    ) -> None:
        self.multi_edit_recovery = {
            "transaction_id": transaction_id,
            "request_id": request_id,
            "generation": generation,
            "state": state,
        }
        self.uncertainty_state = "multi_edit:" + state

    async def _invoke_locked(
        self,
        remote_tool: str,
        arguments: Dict[str, Any],
        timeout_ms: int,
        *,
        request_id: Optional[str],
        admitted_generation: int,
        before_dispatch: Optional[Callable[[], None]],
        on_dispatched: Optional[Callable[..., None]],
    ) -> Any:
        if remote_tool == "studio_recover_multi_edit" and (
            self.multi_edit_recovery is None
            or arguments.get("transaction_id")
            != self.multi_edit_recovery.get("transaction_id")
        ):
            raise SessionConflictError(
                "No exact recovery-required multi-edit transaction matches "
                "this explicit Studio session"
            )
        # Critical no-replay fence for calls that waited through reconnect.
        self.assert_operation_admissible(
            admitted_generation,
            remote_tool,
            arguments,
        )
        if before_dispatch is not None:
            before_dispatch()
        if remote_tool not in self.capabilities:
            from .errors import CapabilityError

            raise CapabilityError(
                "The targeted Studio did not advertise " + remote_tool
            )
        operation_request_id = request_id or str(uuid.uuid4())
        if operation_request_id in self.used_request_ids:
            raise StaleGenerationError(
                "Request ID reuse is forbidden within a Studio generation"
            )
        self.used_request_ids.add(operation_request_id)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = PendingRequest(
            request_id=operation_request_id,
            generation=admitted_generation,
            remote_tool=remote_tool,
            arguments=copy.deepcopy(arguments),
            future=future,
        )
        self.pending[operation_request_id] = pending
        envelope = {
            "v": 2,
            "kind": "request",
            "studio_id": self.studio_id,
            "document_epoch": self.document_epoch,
            "generation": admitted_generation,
            "request_id": operation_request_id,
            "operation": remote_tool,
            "args": copy.deepcopy(arguments),
            "deadline_ms": timeout_ms,
        }
        dispatched = False
        try:
            assert self.transport is not None
            await self.transport.send(envelope)
            dispatched = True
            if on_dispatched is not None:
                dispatched_phase = arguments.get("_phase")
                if not isinstance(dispatched_phase, str):
                    dispatched_phase = (
                        "recover"
                        if remote_tool == "studio_recover_multi_edit"
                        else "direct"
                    )
                try:
                    on_dispatched(
                        operation_request_id,
                        dispatched_phase,
                        arguments.get("transaction_id"),
                    )
                except Exception:
                    uncertainty = {
                        "generation": admitted_generation,
                        "operation": remote_tool,
                        "reason": "post_dispatch_observer_failed",
                    }
                    transaction_id = arguments.get("transaction_id")
                    if isinstance(transaction_id, str):
                        uncertainty["transaction_id"] = transaction_id
                    self.uncertain_requests[
                        operation_request_id
                    ] = uncertainty
                    self.uncertain_pending[
                        operation_request_id
                    ] = pending
                    if (
                        remote_tool
                        in {
                            "studio_multi_edit",
                            "studio_recover_multi_edit",
                        }
                        and dispatched_phase != "prepare"
                        and isinstance(transaction_id, str)
                    ):
                        self._set_multi_edit_recovery(
                            transaction_id,
                            operation_request_id,
                            admitted_generation,
                            "post_dispatch_observer_failed",
                        )
                    else:
                        self._refresh_uncertainty()
                    raise
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout_ms / 1000
            )
            # A response can wake this task just before a reconnect. Fence
            # only immutable connection identity here; a valid recovery-
            # required receipt intentionally leaves the session quarantined.
            self.assert_generation_online(
                admitted_generation, allow_uncertain=True
            )
            return result
        except asyncio.TimeoutError:
            self.uncertain_requests[operation_request_id] = {
                "generation": admitted_generation,
                "operation": remote_tool,
                "reason": "response_timeout_after_dispatch",
            }
            self.uncertain_pending[operation_request_id] = pending
            self._refresh_uncertainty()
            raise RequestTimeoutError(
                "Timed out waiting for the targeted Studio response"
            )
        except asyncio.CancelledError:
            if dispatched and not future.done():
                self.uncertain_requests[operation_request_id] = {
                    "generation": admitted_generation,
                    "operation": remote_tool,
                    "reason": "local_wait_cancelled_after_dispatch",
                }
                self.uncertain_pending[operation_request_id] = pending
                self._refresh_uncertainty()
            raise
        finally:
            current = self.pending.get(operation_request_id)
            if current is pending:
                self.pending.pop(operation_request_id, None)
            if not future.done():
                future.cancel()

    @staticmethod
    def _canonical_uuid(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 36:
            return False
        try:
            return str(uuid.UUID(value)) == value
        except (ValueError, AttributeError, TypeError):
            return False

    @staticmethod
    def _bounded_text(
        value: Any, maximum: int, *, allow_empty: bool = False
    ) -> bool:
        if not isinstance(value, str) or (not allow_empty and not value):
            return False
        try:
            return len(value.encode("utf-8")) <= maximum
        except UnicodeEncodeError:
            return False

    @staticmethod
    def _bounded_integer(
        value: Any, minimum: int, maximum: int
    ) -> bool:
        return (
            type(value) is int
            and minimum <= value <= maximum
        )

    @staticmethod
    def _printable_ascii_script_text(value: Any) -> bool:
        return (
            type(value) is str
            and 1 <= len(value) <= 256
            and value.isascii()
            and all(32 <= ord(character) <= 126 for character in value)
        )

    @staticmethod
    def _ascii_fold_bytes(value: bytes) -> bytes:
        return value.translate(
            bytes.maketrans(
                b"ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                b"abcdefghijklmnopqrstuvwxyz",
            )
        )

    @staticmethod
    def _ordered_byte_subsequence(
        needle: bytes, haystack: bytes
    ) -> bool:
        position = 0
        for byte in needle:
            position = haystack.find(bytes((byte,)), position)
            if position < 0:
                return False
            position += 1
        return True

    @staticmethod
    def _valid_script_cursor(
        value: Any, *, allow_empty: bool
    ) -> bool:
        if type(value) is not str:
            return False
        if value == "":
            return allow_empty
        if len(value) > 2_048:
            return False
        matched = _DURABLE_SCRIPT_CURSOR_RE.fullmatch(value)
        if matched is None:
            return False
        encoded = matched.group(1)
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return False
        return (
            bool(decoded)
            and base64.b64encode(decoded).decode("ascii") == encoded
        )

    @staticmethod
    def _valid_tree_cursor(value: Any, *, allow_empty: bool) -> bool:
        if type(value) is not str:
            return False
        if value == "":
            return allow_empty
        if len(value) > 512:
            return False
        matched = _DURABLE_TREE_CURSOR_RE.fullmatch(value)
        if matched is None:
            return False
        encoded = matched.group(1)
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return False
        return (
            bool(decoded)
            and base64.b64encode(decoded).decode("ascii") == encoded
        )

    @staticmethod
    def _normalized_script_path(
        value: Any, *, allow_empty: bool
    ) -> Optional[tuple[str, ...]]:
        if (
            type(value) is not list
            or (not allow_empty and not value)
            or len(value) > 64
        ):
            return None
        normalized = []
        for segment in value:
            if type(segment) is not str:
                return None
            try:
                encoded = segment.encode("utf-8")
            except UnicodeEncodeError:
                return None
            if (
                not 1 <= len(encoded) <= 100
                or any(
                    unicodedata.category(character) == "Cc"
                    for character in segment
                )
            ):
                return None
            normalized.append(segment)
        return tuple(normalized)

    def _normalized_tree_request(
        self, arguments: Any
    ) -> Optional[Dict[str, Any]]:
        if (
            type(arguments) is not dict
            or not frozenset(arguments).issubset(
                _DURABLE_TREE_ARGUMENT_KEYS
            )
        ):
            return None
        root_path = self._normalized_script_path(
            arguments.get("root_path", []), allow_empty=True
        )
        if root_path is None:
            return None
        max_depth = arguments.get("max_depth", 2)
        scan_limit = arguments.get("scan_limit", 2_000)
        if "max_results" in arguments and "page_size" in arguments:
            return None
        page_size = arguments.get(
            "page_size", arguments.get("max_results", 200)
        )
        name_filter = arguments.get("name_filter", "")
        class_filter = arguments.get("class_filter", "")
        class_is_a = arguments.get("class_is_a", False)
        if (
            not self._bounded_integer(max_depth, 0, 6)
            or len(root_path) + max_depth > 64
            or not self._bounded_integer(scan_limit, 1, 5_000)
            or not self._bounded_integer(page_size, 1, 500)
            or type(class_is_a) is not bool
            or (class_is_a and not class_filter)
        ):
            return None
        if name_filter:
            if (
                not self._bounded_text(name_filter, 100)
                or any(
                    unicodedata.category(character) == "Cc"
                    for character in name_filter
                )
            ):
                return None
        elif type(name_filter) is not str:
            return None
        if class_filter:
            if (
                type(class_filter) is not str
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]{0,99}", class_filter
                )
                is None
            ):
                return None
        elif type(class_filter) is not str:
            return None
        if "continuation_cursor" in arguments and not self._valid_tree_cursor(
            arguments["continuation_cursor"], allow_empty=False
        ):
            return None
        return {
            "root_path": root_path,
            "max_depth": max_depth,
            "scan_limit": scan_limit,
            "page_size": page_size,
            "name_filter": name_filter,
            "class_filter": class_filter,
            "class_is_a": class_is_a,
        }

    def _valid_durable_tree_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        expected = self._normalized_tree_request(pending.arguments)
        if expected is None:
            return False
        if (
            type(result) is not dict
            or frozenset(result) != _DURABLE_TREE_RESULT_KEYS
            or result.get("adapter") != _DURABLE_SCRIPT_ADAPTER
            or type(result.get("v")) is not int
            or result.get("v") != 1
            or result.get("operation") != "studio_list_tree"
            or result.get("operation") != pending.remote_tool
            or result.get("studio_id") != self.studio_id
            or result.get("client_instance_id")
            != self.client_instance_id
            or result.get("document_epoch") != self.document_epoch
            or type(result.get("generation")) is not int
            or result.get("generation") != pending.generation
            or result.get("generation") != self.generation
            or result.get("request_id") != pending.request_id
            or result.get("sort_version") != "name-class-v1"
            or result.get("output_limit_bytes")
            != _DURABLE_TREE_OUTPUT_LIMIT_BYTES
            or type(result.get("output_limit_bytes")) is not int
        ):
            return False
        root_path = self._normalized_script_path(
            result.get("root_path"), allow_empty=True
        )
        if (
            root_path is None
            or root_path != expected["root_path"]
            or result.get("max_depth") != expected["max_depth"]
            or type(result.get("max_depth")) is not int
            or result.get("scan_limit") != expected["scan_limit"]
            or type(result.get("scan_limit")) is not int
            or result.get("page_size") != expected["page_size"]
            or type(result.get("page_size")) is not int
            or result.get("max_results") != expected["page_size"]
            or type(result.get("max_results")) is not int
            or result.get("name_filter") != expected["name_filter"]
            or result.get("class_filter") != expected["class_filter"]
            or result.get("class_is_a") is not expected["class_is_a"]
        ):
            return False
        scanned = result.get("scanned")
        returned = result.get("returned")
        items = result.get("items")
        output_bytes = result.get("output_bytes")
        if (
            not self._bounded_integer(
                scanned, 0, expected["scan_limit"]
            )
            or not self._bounded_integer(
                returned, 0, expected["page_size"]
            )
            or type(items) is not list
            or len(items) != returned
            or not self._bounded_integer(
                output_bytes, 0, _DURABLE_TREE_OUTPUT_LIMIT_BYTES
            )
        ):
            return False
        truncated = result.get("truncated")
        has_more = result.get("has_more")
        cursor = result.get("continuation_cursor")
        reason = result.get("truncation_reason")
        if (
            type(truncated) is not bool
            or has_more is not truncated
            or not self._valid_tree_cursor(cursor, allow_empty=True)
            or type(reason) is not str
            or reason
            not in {
                "complete",
                "page_size",
                "scan_limit",
                "output_bytes",
            }
            or truncated is not bool(cursor)
            or (not truncated and reason != "complete")
            or (truncated and reason == "complete")
        ):
            return False
        if reason == "page_size" and returned != expected["page_size"]:
            return False
        if reason == "scan_limit" and scanned != expected["scan_limit"]:
            return False

        previous_path: Optional[tuple[str, ...]] = None
        calculated_output_bytes = 0
        folded_filter = expected["name_filter"].lower()
        for item in items:
            if (
                type(item) is not dict
                or frozenset(item) != _DURABLE_TREE_ITEM_KEYS
            ):
                return False
            path = self._normalized_script_path(
                item.get("path"), allow_empty=True
            )
            name = item.get("name")
            class_name = item.get("class_name")
            if (
                path is None
                or path[: len(root_path)] != root_path
                or len(path) - len(root_path) > expected["max_depth"]
                or (previous_path is not None and path <= previous_path)
                or not self._bounded_text(name, 100)
                or (path and name != path[-1])
                or not self._inspection_identifier(class_name)
                or not self._bounded_integer(
                    item.get("child_count"), 0, 10_000
                )
                or (
                    folded_filter
                    and folded_filter not in name.lower()
                )
                or (
                    expected["class_filter"]
                    and not expected["class_is_a"]
                    and class_name != expected["class_filter"]
                )
            ):
                return False
            previous_path = path
            try:
                calculated_output_bytes += len(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ) + 1
            except (
                TypeError,
                ValueError,
                UnicodeEncodeError,
                RecursionError,
            ):
                return False
        if output_bytes != calculated_output_bytes:
            return False
        try:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (
            TypeError,
            ValueError,
            UnicodeEncodeError,
            RecursionError,
        ):
            return False
        return len(encoded) <= _DURABLE_TREE_OUTPUT_LIMIT_BYTES

    @staticmethod
    def _finite_inspection_number(value: Any) -> bool:
        if type(value) is float:
            return math.isfinite(value)
        if type(value) is not int:
            return False
        try:
            float_value = float(value)
            return (
                math.isfinite(float_value)
                and int(float_value) == value
            )
        except (OverflowError, TypeError, ValueError):
            return False

    @staticmethod
    def _inspection_identifier(value: Any) -> bool:
        return (
            type(value) is str
            and _DURABLE_CLASS_IDENTIFIER_RE.fullmatch(value) is not None
        )

    @staticmethod
    def _inspection_attribute_name(value: Any) -> bool:
        return (
            type(value) is str
            and value.isascii()
            and _DURABLE_ATTRIBUTE_NAME_RE.fullmatch(value)
            is not None
        )

    @staticmethod
    def _inspection_name(value: Any, maximum: int = 100) -> bool:
        if type(value) is not str:
            return False
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return (
            1 <= len(encoded) <= maximum
            and not any(
                unicodedata.category(character) == "Cc"
                for character in value
            )
        )

    @staticmethod
    def _inspection_uri(value: Any) -> bool:
        if type(value) is not str:
            return False
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        if (
            not 1 <= len(encoded) <= 1_024
            or not value.isascii()
            or any(not 33 <= byte <= 126 for byte in encoded)
        ):
            return False
        return re.fullmatch(
            r"(?:rbxasset|rbxassetid|https?)://"
            r"[^\s\x00-\x1f\x7f]+",
            value,
        ) is not None

    @staticmethod
    def _inspection_inactive_fields(
        value: Dict[str, Any], active: frozenset[str]
    ) -> bool:
        for field_name, sentinel in _DURABLE_INSPECT_SENTINELS.items():
            if field_name in active:
                continue
            field_value = value[field_name]
            if field_name == "number_value":
                if type(field_value) is not int or field_value != 0:
                    return False
            elif field_value != sentinel:
                return False
        return True

    def _valid_inspection_value(
        self, value: Any, *, property_value: bool
    ) -> bool:
        if (
            type(value) is not dict
            or frozenset(value) != _DURABLE_INSPECT_VALUE_KEYS
        ):
            return False
        value_type = value.get("type")
        if (
            type(value_type) is not str
            or value_type not in _DURABLE_INSPECT_VALUE_TYPES
            or type(value.get("boolean_value")) is not bool
            or not self._finite_inspection_number(
                value.get("number_value")
            )
            or type(value.get("text")) is not str
            or type(value.get("numbers")) is not list
            or len(value["numbers"]) > 256
            or any(
                not self._finite_inspection_number(number)
                for number in value["numbers"]
            )
            or type(value.get("labels")) is not list
            or len(value["labels"]) > 3
            or any(
                type(label) is not str
                or not self._bounded_text(
                    label, 1_024, allow_empty=True
                )
                for label in value["labels"]
            )
            or not self._bounded_integer(
                value.get("byte_length"), 0, 262_144
            )
            or type(value.get("truncated")) is not bool
        ):
            return False
        try:
            text_length = len(value["text"].encode("utf-8"))
        except UnicodeEncodeError:
            return False
        if text_length > 1_024:
            return False

        inactive_only = {"nil", "unavailable", "unsupported"}
        if value_type in inactive_only:
            return (
                property_value
                and self._inspection_inactive_fields(
                    value, frozenset()
                )
            )
        if value_type == "boolean":
            return self._inspection_inactive_fields(
                value, frozenset({"boolean_value"})
            )
        if value_type == "number":
            return self._inspection_inactive_fields(
                value, frozenset({"number_value"})
            )
        if value_type == "string":
            if not self._inspection_inactive_fields(
                value,
                frozenset({"text", "byte_length", "truncated"}),
            ):
                return False
            if value["truncated"]:
                return (
                    value["byte_length"] > 1_024
                    and 1_021 <= text_length <= 1_024
                )
            return value["byte_length"] == text_length
        if value_type == "enum":
            return (
                property_value
                and self._inspection_inactive_fields(
                    value,
                    frozenset({"number_value", "labels"}),
                )
                and type(value["number_value"]) is int
                and len(value["labels"]) == 2
                and all(
                    self._inspection_identifier(label)
                    for label in value["labels"]
                )
            )

        dimensions = {
            "vector2": 2,
            "vector3": 3,
            "color3": 3,
            "cframe": 12,
            "udim": 2,
            "udim2": 4,
            "rect": 4,
            "number_range": 2,
        }
        if value_type in dimensions:
            numbers = value["numbers"]
            if (
                not self._inspection_inactive_fields(
                    value, frozenset({"numbers"})
                )
                or len(numbers) != dimensions[value_type]
            ):
                return False
            if value_type == "color3" and any(
                not 0 <= number <= 1 for number in numbers
            ):
                return False
            if value_type == "udim" and type(numbers[1]) is not int:
                return False
            if value_type == "udim2" and (
                type(numbers[1]) is not int
                or type(numbers[3]) is not int
            ):
                return False
            if value_type == "rect" and (
                numbers[0] > numbers[2]
                or numbers[1] > numbers[3]
            ):
                return False
            if (
                value_type == "number_range"
                and numbers[0] > numbers[1]
            ):
                return False
            return True

        if value_type == "brick_color":
            return (
                self._inspection_inactive_fields(
                    value,
                    frozenset(
                        {"number_value", "numbers", "labels"}
                    ),
                )
                and type(value["number_value"]) is int
                and len(value["numbers"]) == 3
                and all(
                    0 <= number <= 1
                    for number in value["numbers"]
                )
                and len(value["labels"]) == 1
                and self._inspection_name(value["labels"][0])
            )

        if value_type in {"number_sequence", "color_sequence"}:
            width = 3 if value_type == "number_sequence" else 4
            numbers = value["numbers"]
            keypoint_count, remainder = divmod(len(numbers), width)
            if (
                not self._inspection_inactive_fields(
                    value, frozenset({"numbers"})
                )
                or remainder != 0
                or not 2 <= keypoint_count <= 64
            ):
                return False
            previous_time: Optional[float] = None
            for index in range(keypoint_count):
                offset = index * width
                keypoint_time = numbers[offset]
                if (
                    not 0 <= keypoint_time <= 1
                    or (
                        previous_time is not None
                        and keypoint_time <= previous_time
                    )
                ):
                    return False
                previous_time = keypoint_time
                if value_type == "number_sequence":
                    if numbers[offset + 2] < 0:
                        return False
                elif any(
                    not 0 <= channel <= 1
                    for channel in numbers[offset + 1 : offset + 4]
                ):
                    return False
            return numbers[0] == 0 and numbers[-width] == 1

        if value_type == "font":
            return (
                self._inspection_inactive_fields(
                    value,
                    frozenset(
                        {
                            "boolean_value",
                            "number_value",
                            "labels",
                        }
                    ),
                )
                and type(value["number_value"]) is int
                and len(value["labels"]) == 3
                and self._inspection_uri(value["labels"][0])
                and self._inspection_identifier(
                    value["labels"][1]
                )
                and self._inspection_identifier(
                    value["labels"][2]
                )
            )
        return False

    def _normalized_inspection_request(
        self, arguments: Any
    ) -> Optional[Dict[str, Any]]:
        if (
            type(arguments) is not dict
            or not frozenset(arguments).issubset(
                _DURABLE_INSPECT_ARGUMENT_KEYS
            )
            or "path" not in arguments
        ):
            return None
        path = self._normalized_script_path(
            arguments.get("path"), allow_empty=False
        )
        if path is None:
            return None
        child_limit = arguments.get("child_limit", 50)
        descendant_max_depth = arguments.get(
            "descendant_max_depth", 64 - len(path)
        )
        descendant_scan_limit = arguments.get(
            "descendant_scan_limit", 2_000
        )
        time_limit_ms = arguments.get("time_limit_ms", 3_000)
        if (
            not self._bounded_integer(child_limit, 0, 200)
            or not self._bounded_integer(
                descendant_max_depth, 0, 64
            )
            or len(path) + descendant_max_depth > 64
            or not self._bounded_integer(
                descendant_scan_limit, 1, 5_000
            )
            or not self._bounded_integer(
                time_limit_ms, 100, 10_000
            )
        ):
            return None
        return {
            "path": path,
            "child_limit": child_limit,
            "descendant_max_depth": descendant_max_depth,
            "descendant_scan_limit": descendant_scan_limit,
            "time_limit_ms": time_limit_ms,
        }

    def _valid_durable_inspection_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        expected = self._normalized_inspection_request(
            pending.arguments
        )
        if expected is None:
            return False
        if (
            type(result) is not dict
            or frozenset(result) != _DURABLE_INSPECT_RESULT_KEYS
            or result.get("adapter") != _DURABLE_SCRIPT_ADAPTER
            or type(result.get("v")) is not int
            or result.get("v") != 1
            or result.get("operation") != "studio_inspect_instance"
            or result.get("operation") != pending.remote_tool
            or result.get("studio_id") != self.studio_id
            or result.get("client_instance_id")
            != self.client_instance_id
            or result.get("document_epoch") != self.document_epoch
            or type(result.get("generation")) is not int
            or result.get("generation") != pending.generation
            or result.get("generation") != self.generation
            or result.get("request_id") != pending.request_id
            or result.get("datamodel_type") != "Edit"
            or result.get("snapshot_contract")
            != "path-edit-generation-fenced-observational-v1"
            or result.get("property_allowlist_version")
            != "instance-property-allowlist-v1"
            or result.get("value_encoding_version")
            != "instance-value-v1"
            or result.get("sort_version")
            != "name-class-original-v1"
            or result.get("output_limit_bytes")
            != _DURABLE_INSPECT_OUTPUT_LIMIT_BYTES
            or type(result.get("output_limit_bytes")) is not int
        ):
            return False

        path = self._normalized_script_path(
            result.get("path"), allow_empty=False
        )
        if (
            path is None
            or path != expected["path"]
            or result.get("name") != path[-1]
            or not self._inspection_identifier(
                result.get("class_name")
            )
        ):
            return False
        for field_name in (
            "child_limit",
            "descendant_max_depth",
            "descendant_scan_limit",
            "time_limit_ms",
        ):
            if (
                type(result.get(field_name)) is not int
                or result[field_name] != expected[field_name]
            ):
                return False

        properties = result.get("properties")
        property_count = result.get("property_count")
        if (
            type(properties) is not list
            or not self._bounded_integer(
                property_count,
                0,
                len(_DURABLE_INSPECT_PROPERTY_SELECTORS),
            )
            or property_count != len(properties)
            or type(result.get("properties_complete")) is not bool
        ):
            return False
        previous_selector: Optional[str] = None
        incomplete_property = False
        saw_archivable = False
        observed_selectors: Set[str] = set()
        for entry in properties:
            if (
                type(entry) is not dict
                or frozenset(entry)
                != _DURABLE_INSPECT_PROPERTY_KEYS
            ):
                return False
            selector = entry.get("selector")
            inspection_value = entry.get("value")
            if (
                type(selector) is not str
                or selector
                not in _DURABLE_INSPECT_PROPERTY_SELECTORS
                or (
                    previous_selector is not None
                    and selector <= previous_selector
                )
                or not self._valid_inspection_value(
                    inspection_value, property_value=True
                )
                or inspection_value["type"]
                not in {
                    _DURABLE_INSPECT_PROPERTY_VALUE_TYPES[selector],
                    "unavailable",
                    "unsupported",
                }
            ):
                return False
            expected_enum_family = (
                _DURABLE_INSPECT_PROPERTY_ENUM_FAMILIES.get(
                    selector
                )
            )
            if (
                inspection_value["type"] == "enum"
                and inspection_value["labels"][0]
                != expected_enum_family
            ):
                return False
            previous_selector = selector
            observed_selectors.add(selector)
            if selector == "Instance.Archivable":
                saw_archivable = True
            if inspection_value["type"] in {
                "unavailable",
                "unsupported",
            }:
                incomplete_property = True
        if (
            not saw_archivable
            or result["properties_complete"] is incomplete_property
        ):
            return False
        present_group_count = 0
        for property_group in _DURABLE_INSPECT_PROPERTY_GROUPS:
            present = observed_selectors & property_group
            if present and present != property_group:
                return False
            if present:
                present_group_count += 1
        if present_group_count > 1:
            return False

        attributes = result.get("attributes")
        attributes_total = result.get("attributes_total")
        attributes_returned = result.get("attributes_returned")
        if (
            type(attributes) is not list
            or not self._bounded_integer(
                attributes_total, 0, 1_024
            )
            or attributes_returned != min(attributes_total, 64)
            or type(attributes_returned) is not int
            or len(attributes) != attributes_returned
            or result.get("attributes_truncated")
            is not (attributes_total > 64)
        ):
            return False
        previous_name_bytes: Optional[bytes] = None
        for entry in attributes:
            if (
                type(entry) is not dict
                or frozenset(entry)
                != _DURABLE_INSPECT_ATTRIBUTE_KEYS
                or not self._inspection_attribute_name(
                    entry.get("name")
                )
                or not self._valid_inspection_value(
                    entry.get("value"), property_value=False
                )
            ):
                return False
            name_bytes = entry["name"].encode("utf-8")
            if (
                previous_name_bytes is not None
                and name_bytes <= previous_name_bytes
            ):
                return False
            previous_name_bytes = name_bytes

        tags = result.get("tags")
        tags_total = result.get("tags_total")
        tags_returned = result.get("tags_returned")
        if (
            type(tags) is not list
            or not self._bounded_integer(tags_total, 0, 1_024)
            or tags_returned != min(tags_total, 128)
            or type(tags_returned) is not int
            or len(tags) != tags_returned
            or result.get("tags_truncated") is not (tags_total > 128)
        ):
            return False
        previous_tag_bytes: Optional[bytes] = None
        for tag in tags:
            if not self._inspection_name(tag):
                return False
            tag_bytes = tag.encode("utf-8")
            if (
                previous_tag_bytes is not None
                and tag_bytes <= previous_tag_bytes
            ):
                return False
            previous_tag_bytes = tag_bytes

        children = result.get("children")
        children_total = result.get("children_total")
        children_returned = result.get("children_returned")
        child_limit = expected["child_limit"]
        reason = result.get("children_truncation_reason")
        if (
            type(children) is not list
            or not self._bounded_integer(children_total, 0, 10_000)
            or not self._bounded_integer(
                children_returned, 0, child_limit
            )
            or len(children) != children_returned
            or type(result.get("children_truncated")) is not bool
            or result["children_truncated"]
            is not (children_returned < children_total)
            or type(reason) is not str
            or reason not in {
                "complete",
                "child_limit",
                "output_bytes",
            }
        ):
            return False
        if reason == "complete":
            if children_returned != children_total:
                return False
        elif reason == "child_limit":
            if not (
                children_total > child_limit
                and children_returned == child_limit
            ):
                return False
        elif not (
            1 <= children_returned < children_total
            and children_returned
            < min(children_total, child_limit)
        ):
            return False

        child_name_counts: Dict[str, int] = {}
        previous_child_key: Optional[tuple[bytes, bytes]] = None
        for child in children:
            if (
                type(child) is not dict
                or frozenset(child) != _DURABLE_INSPECT_CHILD_KEYS
                or not self._inspection_name(child.get("name"))
                or not self._inspection_identifier(
                    child.get("class_name")
                )
                or type(child.get("addressable")) is not bool
            ):
                return False
            child_key = (
                child["name"].encode("utf-8"),
                child["class_name"].encode("ascii"),
            )
            if (
                previous_child_key is not None
                and child_key < previous_child_key
            ):
                return False
            previous_child_key = child_key
            child_name_counts[child["name"]] = (
                child_name_counts.get(child["name"], 0) + 1
            )
            child_path = self._normalized_script_path(
                child.get("path"),
                allow_empty=not child["addressable"],
            )
            if child["addressable"]:
                if (
                    child_path is None
                    or len(path) >= 64
                    or child_path != path + (child["name"],)
                ):
                    return False
            elif child.get("path") != []:
                return False
        for index, child in enumerate(children):
            expected_addressable = (
                len(path) < 64
                and child_name_counts[child["name"]] == 1
                and not (
                    result["children_truncated"]
                    and index == len(children) - 1
                )
            )
            if child["addressable"] is not expected_addressable:
                return False

        descendant_count = result.get("descendant_count")
        descendant_complete = result.get(
            "descendant_count_complete"
        )
        descendant_reason = result.get(
            "descendant_truncation_reason"
        )
        if (
            not self._bounded_integer(
                descendant_count,
                0,
                expected["descendant_scan_limit"],
            )
            or type(descendant_complete) is not bool
            or type(descendant_reason) is not str
            or descendant_reason not in {
                "complete",
                "scan_limit",
                "time_limit",
                "depth_limit",
            }
            or descendant_complete
            is not (descendant_reason == "complete")
            or (
                descendant_reason == "scan_limit"
                and descendant_count
                != expected["descendant_scan_limit"]
            )
        ):
            return False

        class_counts = result.get("descendant_class_counts")
        if type(class_counts) is not list or len(class_counts) > 256:
            return False
        previous_class_name: Optional[str] = None
        counted_descendants = 0
        for entry in class_counts:
            if (
                type(entry) is not dict
                or frozenset(entry)
                != _DURABLE_INSPECT_CLASS_COUNT_KEYS
                or not self._inspection_identifier(
                    entry.get("class_name")
                )
                or (
                    previous_class_name is not None
                    and entry["class_name"] <= previous_class_name
                )
                or not self._bounded_integer(
                    entry.get("count"), 1, descendant_count
                )
            ):
                return False
            previous_class_name = entry["class_name"]
            counted_descendants += entry["count"]
        if counted_descendants != descendant_count:
            return False
        if children_total == 0:
            if (
                descendant_count != 0
                or descendant_complete is not True
                or descendant_reason != "complete"
                or class_counts
            ):
                return False
        elif expected["descendant_max_depth"] == 0:
            if (
                descendant_count != 0
                or descendant_complete is not False
                or descendant_reason
                not in {"depth_limit", "time_limit"}
                or class_counts
            ):
                return False
        elif expected["descendant_max_depth"] == 1:
            if (
                descendant_count > children_total
                or (
                    descendant_reason in {
                        "complete",
                        "depth_limit",
                    }
                    and descendant_count != children_total
                )
                or (
                    descendant_reason == "scan_limit"
                    and descendant_count >= children_total
                )
            ):
                return False
        elif (
            descendant_reason in {"complete", "depth_limit"}
            and descendant_count < children_total
        ):
            return False

        try:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (
            TypeError,
            ValueError,
            UnicodeEncodeError,
            RecursionError,
        ):
            return False
        return len(encoded) <= _DURABLE_INSPECT_OUTPUT_LIMIT_BYTES

    def _normalized_script_request(
        self, remote_tool: str, arguments: Any
    ) -> Optional[Dict[str, Any]]:
        if type(arguments) is not dict:
            return None
        if remote_tool == "studio_search_scripts":
            allowed_keys = _DURABLE_SCRIPT_SEARCH_ARGUMENT_KEYS
        elif remote_tool == "studio_grep_scripts":
            allowed_keys = _DURABLE_SCRIPT_GREP_ARGUMENT_KEYS
        else:
            return None
        if not frozenset(arguments).issubset(allowed_keys):
            return None

        root_path = self._normalized_script_path(
            arguments.get("root_path", []),
            allow_empty=True,
        )
        if root_path is None:
            return None
        max_depth = arguments.get("max_depth", 64 - len(root_path))
        scan_limit = arguments.get("scan_limit", 2_000)
        time_limit_ms = arguments.get(
            "time_limit_ms",
            3_000
            if remote_tool == "studio_search_scripts"
            else 5_000,
        )
        maximum_page_size = (
            10 if remote_tool == "studio_search_scripts" else 50
        )
        page_size = arguments.get("page_size", maximum_page_size)
        if (
            not self._bounded_integer(max_depth, 0, 64)
            or len(root_path) + max_depth > 64
            or not self._bounded_integer(scan_limit, 1, 5_000)
            or not self._bounded_integer(
                page_size, 1, maximum_page_size
            )
            or not self._bounded_integer(time_limit_ms, 100, 10_000)
        ):
            return None
        if "continuation_cursor" in arguments and not (
            self._valid_script_cursor(
                arguments["continuation_cursor"],
                allow_empty=False,
            )
        ):
            return None

        normalized: Dict[str, Any] = {
            "root_path": root_path,
            "max_depth": max_depth,
            "scan_limit": scan_limit,
            "page_size": page_size,
            "time_limit_ms": time_limit_ms,
        }
        if remote_tool == "studio_search_scripts":
            raw_keywords = arguments.get("keywords")
            if not self._printable_ascii_script_text(raw_keywords):
                return None
            keywords = []
            seen = set()
            for raw_token in raw_keywords.split(","):
                token = raw_token.strip(" ")
                if not 1 <= len(token.encode("ascii")) <= 64:
                    return None
                folded = token.lower()
                if folded in seen:
                    return None
                seen.add(folded)
                keywords.append(folded)
            if not 1 <= len(keywords) <= 8:
                return None
            normalized["keywords"] = keywords
            return normalized

        query = arguments.get("query")
        if not self._printable_ascii_script_text(query):
            return None
        case_sensitive = arguments.get("case_sensitive", True)
        source_byte_limit = arguments.get(
            "source_byte_limit", 1_048_576
        )
        if (
            type(case_sensitive) is not bool
            or not self._bounded_integer(
                source_byte_limit, 262_144, 4_194_304
            )
        ):
            return None
        normalized.update(
            {
                "query": query,
                "case_sensitive": case_sensitive,
                "source_byte_limit": source_byte_limit,
            }
        )
        return normalized

    def _valid_script_common_result(
        self,
        pending: PendingRequest,
        result: Any,
        expected: Dict[str, Any],
        *,
        exact_keys: frozenset[str],
        maximum_page_size: int,
        output_limit_bytes: int,
        reasons: frozenset[str],
    ) -> bool:
        if (
            type(result) is not dict
            or frozenset(result) != exact_keys
            or result.get("adapter") != _DURABLE_SCRIPT_ADAPTER
            or type(result.get("v")) is not int
            or result.get("v") != 1
            or result.get("operation") != pending.remote_tool
            or result.get("studio_id") != self.studio_id
            or result.get("client_instance_id")
            != self.client_instance_id
            or result.get("document_epoch") != self.document_epoch
            or type(result.get("generation")) is not int
            or result.get("generation") != pending.generation
            or result.get("generation") != self.generation
            or result.get("request_id") != pending.request_id
            or result.get("sort_version") != "name-class-v1"
            or result.get("output_limit_bytes")
            != output_limit_bytes
        ):
            return False

        root_path = self._normalized_script_path(
            result.get("root_path"), allow_empty=True
        )
        if (
            root_path is None
            or root_path != expected["root_path"]
            or result.get("max_depth") != expected["max_depth"]
            or type(result.get("max_depth")) is not int
            or result.get("scan_limit") != expected["scan_limit"]
            or type(result.get("scan_limit")) is not int
            or result.get("page_size") != expected["page_size"]
            or type(result.get("page_size")) is not int
            or not 1 <= result["page_size"] <= maximum_page_size
            or result.get("time_limit_ms") != expected["time_limit_ms"]
            or type(result.get("time_limit_ms")) is not int
        ):
            return False

        scanned_instances = result.get("scanned_instances")
        scanned_scripts = result.get("scanned_scripts")
        returned = result.get("returned")
        items = result.get("items")
        if (
            not self._bounded_integer(
                scanned_instances, 0, expected["scan_limit"]
            )
            or not self._bounded_integer(
                scanned_scripts, 0, scanned_instances
            )
            or not self._bounded_integer(
                returned, 0, expected["page_size"]
            )
            or type(items) is not list
            or len(items) != returned
        ):
            return False

        cursor = result.get("continuation_cursor")
        if not self._valid_script_cursor(cursor, allow_empty=True):
            return False
        reason = result.get("truncation_reason")
        if type(reason) is not str or reason not in reasons:
            return False
        has_cursor = cursor != ""
        if (
            (reason == "complete") == has_cursor
            or result.get("truncated") is not has_cursor
            or result.get("has_more") is not has_cursor
            or (reason == "page_size"
                and returned != expected["page_size"])
            or (reason == "scan_limit"
                and scanned_instances != expected["scan_limit"])
        ):
            return False

        try:
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (
            TypeError,
            ValueError,
            UnicodeEncodeError,
            RecursionError,
        ):
            return False
        return len(encoded) <= output_limit_bytes

    def _valid_script_item_identity(
        self,
        item: Any,
        *,
        exact_keys: frozenset[str],
        root_path: tuple[str, ...],
        max_depth: int,
    ) -> Optional[tuple[str, ...]]:
        if type(item) is not dict or frozenset(item) != exact_keys:
            return None
        path = self._normalized_script_path(
            item.get("path"), allow_empty=False
        )
        if (
            path is None
            or path[: len(root_path)] != root_path
            or len(path) > len(root_path) + max_depth
            or item.get("name") != path[-1]
            or item.get("class_name") not in _DURABLE_SCRIPT_CLASSES
        ):
            return None
        return path

    def _valid_script_search_result(
        self,
        pending: PendingRequest,
        result: Any,
        expected: Dict[str, Any],
    ) -> bool:
        if not self._valid_script_common_result(
            pending,
            result,
            expected,
            exact_keys=_DURABLE_SCRIPT_SEARCH_RESULT_KEYS,
            maximum_page_size=10,
            output_limit_bytes=200_000,
            reasons=frozenset(
                {
                    "complete",
                    "page_size",
                    "scan_limit",
                    "time_budget",
                    "output_bytes",
                }
            ),
        ):
            return False
        if (
            result.get("keywords") != expected["keywords"]
            or type(result.get("keywords")) is not list
            or result.get("match_semantics")
            != (
                "all_keywords_ascii_case_insensitive_"
                "literal_subsequence"
            )
            or result.get("query_version")
            != "script-name-query-v1"
            or result["returned"] > result["scanned_scripts"]
        ):
            return False

        paths = set()
        previous_path: Optional[tuple[str, ...]] = None
        keyword_bytes = [
            keyword.encode("ascii") for keyword in expected["keywords"]
        ]
        for item in result["items"]:
            path = self._valid_script_item_identity(
                item,
                exact_keys=_DURABLE_SCRIPT_SEARCH_ITEM_KEYS,
                root_path=expected["root_path"],
                max_depth=expected["max_depth"],
            )
            if (
                path is None
                or path in paths
                or (
                    previous_path is not None
                    and path <= previous_path
                )
            ):
                return False
            paths.add(path)
            previous_path = path
            try:
                folded_name = self._ascii_fold_bytes(
                    item["name"].encode("utf-8")
                )
            except UnicodeEncodeError:
                return False
            if any(
                not self._ordered_byte_subsequence(
                    keyword, folded_name
                )
                for keyword in keyword_bytes
            ):
                return False
        return True

    def _valid_script_grep_result(
        self,
        pending: PendingRequest,
        result: Any,
        expected: Dict[str, Any],
    ) -> bool:
        if not self._valid_script_common_result(
            pending,
            result,
            expected,
            exact_keys=_DURABLE_SCRIPT_GREP_RESULT_KEYS,
            maximum_page_size=50,
            output_limit_bytes=500_000,
            reasons=frozenset(
                {
                    "complete",
                    "page_size",
                    "scan_limit",
                    "source_bytes",
                    "time_budget",
                    "output_bytes",
                }
            ),
        ):
            return False
        source_bytes_scanned = result.get("source_bytes_scanned")
        if (
            result.get("query") != expected["query"]
            or result.get("match_mode") != "literal"
            or result.get("case_sensitive")
            is not expected["case_sensitive"]
            or result.get("query_version")
            != "script-grep-query-v1"
            or result.get("source_byte_limit")
            != expected["source_byte_limit"]
            or type(result.get("source_byte_limit")) is not int
            or not self._bounded_integer(
                source_bytes_scanned,
                0,
                expected["source_byte_limit"],
            )
        ):
            return False

        query_bytes = expected["query"].encode("ascii")
        folded_query = self._ascii_fold_bytes(query_bytes)
        seen_matches = set()
        closed_paths = set()
        current_path: Optional[tuple[str, ...]] = None
        current_revision: Optional[tuple[Any, ...]] = None
        previous_path: Optional[tuple[str, ...]] = None
        previous_end = 0
        previous_line_number = 0
        previous_column_byte = 0
        previous_line_start = 0
        matched_script_count = 0
        matched_source_bytes = 0

        for item in result["items"]:
            path = self._valid_script_item_identity(
                item,
                exact_keys=_DURABLE_SCRIPT_GREP_ITEM_KEYS,
                root_path=expected["root_path"],
                max_depth=expected["max_depth"],
            )
            if path is None:
                return False
            source_sha256 = item.get("source_sha256")
            source_length = item.get("source_length")
            match_start = item.get("match_start_byte")
            match_end = item.get("match_end_byte")
            line_number = item.get("line_number")
            column_byte = item.get("column_byte")
            preview_start = item.get("preview_start_byte")
            preview = item.get("preview")
            if (
                type(source_sha256) is not str
                or _DURABLE_SHA256_RE.fullmatch(source_sha256) is None
                or not self._bounded_integer(
                    source_length, 0, 262_144
                )
                or not self._bounded_integer(
                    match_start, 1, source_length
                )
                or not self._bounded_integer(
                    match_end, match_start, source_length
                )
                or match_end - match_start + 1 != len(query_bytes)
                or not self._bounded_integer(
                    line_number, 1, 20_000
                )
                or type(column_byte) is not int
                or column_byte < 1
                or column_byte > source_length
                or not self._bounded_integer(
                    preview_start, 1, source_length
                )
                or type(preview) is not str
                or type(item.get("preview_prefix_truncated"))
                is not bool
                or type(item.get("preview_suffix_truncated"))
                is not bool
            ):
                return False
            try:
                preview_bytes = preview.encode("utf-8")
            except UnicodeEncodeError:
                return False
            preview_end = preview_start + len(preview_bytes) - 1
            match_offset = match_start - preview_start
            line_start = match_start - column_byte + 1
            room_before = (512 - len(query_bytes)) // 2
            nominal_preview_start = max(
                line_start, match_start - room_before
            )
            if (
                not 1 <= len(preview_bytes) <= 512
                or b"\n" in preview_bytes
                or line_start < 1
                or preview_start > match_start
                or preview_start < line_start
                or preview_start > nominal_preview_start
                or nominal_preview_start - preview_start > 3
                or preview_end > source_length
                or match_offset + len(query_bytes)
                > len(preview_bytes)
                or item["preview_prefix_truncated"]
                is not (preview_start > line_start)
                or (
                    preview_end == source_length
                    and item["preview_suffix_truncated"]
                )
                or (
                    item["preview_suffix_truncated"]
                    and not 509 <= len(preview_bytes) <= 512
                )
                or (line_start == 1) is not (line_number == 1)
            ):
                return False
            observed_match = preview_bytes[
                match_offset : match_offset + len(query_bytes)
            ]
            if expected["case_sensitive"]:
                if observed_match != query_bytes:
                    return False
            elif self._ascii_fold_bytes(observed_match) != folded_query:
                return False

            revision = (
                source_sha256,
                source_length,
                item["name"],
                item["class_name"],
            )
            if path != current_path:
                if path in closed_paths:
                    return False
                if (
                    previous_path is not None
                    and path <= previous_path
                ):
                    return False
                if current_path is not None:
                    closed_paths.add(current_path)
                current_path = path
                previous_path = path
                current_revision = revision
                previous_end = 0
                previous_line_number = 0
                previous_column_byte = 0
                previous_line_start = 0
                matched_script_count += 1
                matched_source_bytes += source_length
            elif revision != current_revision:
                return False
            if (
                match_start <= previous_end
                or line_number < previous_line_number
                or (
                    line_number == previous_line_number
                    and (
                        column_byte <= previous_column_byte
                        or line_start != previous_line_start
                    )
                )
                or (
                    previous_line_number > 0
                    and line_number > previous_line_number
                    and line_start <= previous_end
                )
            ):
                return False
            previous_end = match_end
            previous_line_number = line_number
            previous_column_byte = column_byte
            previous_line_start = line_start
            match_identity = (path, source_sha256, match_start)
            if match_identity in seen_matches:
                return False
            seen_matches.add(match_identity)
        return (
            matched_script_count <= result["scanned_scripts"]
            and matched_source_bytes <= source_bytes_scanned
        )

    def _valid_durable_script_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        expected = self._normalized_script_request(
            pending.remote_tool, pending.arguments
        )
        if expected is None:
            return False
        if pending.remote_tool == "studio_search_scripts":
            return self._valid_script_search_result(
                pending, result, expected
            )
        if pending.remote_tool == "studio_grep_scripts":
            return self._valid_script_grep_result(
                pending, result, expected
            )
        return False

    def _valid_multi_edit_prepared_target(
        self,
        target: Any,
        requested: Dict[str, Any],
        index: int,
    ) -> bool:
        path = self._normalized_script_path(
            target.get("path") if isinstance(target, dict) else None,
            allow_empty=False,
        )
        expected_path = self._normalized_script_path(
            requested.get("path"), allow_empty=False
        )
        return (
            type(target) is dict
            and frozenset(target)
            == _DURABLE_MULTI_EDIT_PREPARED_TARGET_KEYS
            and target.get("index") == index
            and type(target.get("index")) is int
            and path is not None
            and path == expected_path
            and target.get("expected_sha256")
            == requested.get("expected_sha256")
            and target.get("prepared_sha256")
            == requested.get("expected_sha256")
            and type(target.get("planned_sha256")) is str
            and SHA256_RE.fullmatch(target["planned_sha256"]) is not None
            and self._bounded_integer(
                target.get("source_length"),
                0,
                MAX_MULTI_EDIT_SOURCE_BYTES,
            )
            and self._bounded_integer(
                target.get("planned_source_length"),
                0,
                MAX_MULTI_EDIT_SOURCE_BYTES,
            )
            and target.get("edit_count") == len(requested["edits"])
            and type(target.get("edit_count")) is int
            and 1
            <= target["edit_count"]
            <= MAX_MULTI_EDIT_EDITS_PER_TARGET
            and self._bounded_integer(
                target.get("replacement_count"),
                target["edit_count"],
                MAX_MULTI_EDIT_REPLACEMENT_SPANS,
            )
            and target.get("status") == "prepared"
        )

    def _valid_multi_edit_prepare_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        arguments = pending.arguments
        targets = arguments.get("targets")
        if (
            arguments.get("_phase") != "prepare"
            or arguments.get("datamodel_type") != "Edit"
            or not self._canonical_uuid(
                arguments.get("transaction_id")
            )
            or type(targets) is not list
        ):
            return False
        if (
            type(result) is not dict
            or frozenset(result) != _DURABLE_MULTI_EDIT_PREPARE_KEYS
            or result.get("adapter") != _DURABLE_SCRIPT_ADAPTER
            or type(result.get("v")) is not int
            or result.get("v") != 1
            or result.get("operation") != "studio_multi_edit"
            or result.get("phase") != "prepare"
            or result.get("studio_id") != self.studio_id
            or result.get("client_instance_id")
            != self.client_instance_id
            or result.get("document_epoch") != self.document_epoch
            or type(result.get("generation")) is not int
            or result.get("generation") != pending.generation
            or result.get("generation") != self.generation
            or result.get("request_id") != pending.request_id
            or result.get("transaction_id")
            != arguments["transaction_id"]
            or result.get("ordering_version")
            != MULTI_EDIT_ORDERING_VERSION
            or result.get("atomicity") != MULTI_EDIT_ATOMICITY
            or result.get("target_count") != len(targets)
            or type(result.get("target_count")) is not int
            or not 1
            <= result["target_count"]
            <= MAX_MULTI_EDIT_TARGETS
            or result.get("edit_count") != total_edit_count(targets)
            or type(result.get("edit_count")) is not int
            or not 1 <= result["edit_count"] <= MAX_MULTI_EDIT_EDITS
            or result.get("expires_in_ms") != 120_000
            or type(result.get("expires_in_ms")) is not int
        ):
            return False
        receipt_targets = result.get("targets")
        if (
            type(receipt_targets) is not list
            or len(receipt_targets) != len(targets)
        ):
            return False
        aggregate_source = 0
        aggregate_planned = 0
        replacement_total = 0
        for index, (receipt_target, requested) in enumerate(
            zip(receipt_targets, targets), start=1
        ):
            if not self._valid_multi_edit_prepared_target(
                receipt_target, requested, index
            ):
                return False
            aggregate_source += receipt_target["source_length"]
            aggregate_planned += receipt_target[
                "planned_source_length"
            ]
            replacement_total += receipt_target[
                "replacement_count"
            ]
        if (
            result.get("aggregate_source_bytes") != aggregate_source
            or type(result.get("aggregate_source_bytes")) is not int
            or not 0
            <= aggregate_source
            <= MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES
            or result.get("aggregate_planned_source_bytes")
            != aggregate_planned
            or type(result.get("aggregate_planned_source_bytes"))
            is not int
            or not 0
            <= aggregate_planned
            <= MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES
            or replacement_total > MAX_MULTI_EDIT_REPLACEMENT_SPANS
            or type(result.get("prepare_sha256")) is not str
            or SHA256_RE.fullmatch(result["prepare_sha256"]) is None
        ):
            return False
        try:
            return result["prepare_sha256"] == prepare_receipt_sha256(
                result
            )
        except (KeyError, TypeError, ValidationError):
            return False

    def _valid_multi_edit_mutation_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        phase = pending.arguments.get("_phase")
        is_recovery = pending.remote_tool == "studio_recover_multi_edit"
        expected_phase = "recover" if is_recovery else "apply"
        expected_operation = (
            "studio_recover_multi_edit"
            if is_recovery
            else "studio_multi_edit"
        )
        prepared = self.multi_edit_prepared_receipt
        if (
            (
                phase is not None
                if is_recovery
                else phase != "apply"
            )
            or prepared is None
            or type(result) is not dict
            or frozenset(result) != _DURABLE_MULTI_EDIT_MUTATION_KEYS
            or result.get("adapter") != _DURABLE_SCRIPT_ADAPTER
            or type(result.get("v")) is not int
            or result.get("v") != 1
            or result.get("operation") != expected_operation
            or result.get("phase") != expected_phase
            or result.get("studio_id") != self.studio_id
            or result.get("client_instance_id")
            != self.client_instance_id
            or result.get("document_epoch") != self.document_epoch
            or type(result.get("generation")) is not int
            or result.get("generation") != pending.generation
            or result.get("generation") != self.generation
            or prepared.get("generation") != pending.generation
            or result.get("request_id") != pending.request_id
            or result.get("transaction_id")
            != prepared.get("transaction_id")
            or result.get("transaction_id")
            != pending.arguments.get("transaction_id")
            or result.get("prepare_request_id")
            != prepared.get("request_id")
            or result.get("prepare_sha256")
            != prepared.get("prepare_sha256")
            or result.get("ordering_version")
            != MULTI_EDIT_ORDERING_VERSION
            or result.get("atomicity") != MULTI_EDIT_ATOMICITY
            or result.get("receipt_contract")
            != MULTI_EDIT_RECEIPT_CONTRACT
            or result.get("target_count")
            != prepared.get("target_count")
            or type(result.get("target_count")) is not int
            or result.get("edit_count") != prepared.get("edit_count")
            or type(result.get("edit_count")) is not int
            or type(result.get("safe_terminal")) is not bool
            or type(result.get("recovery_required")) is not bool
            or result["recovery_required"] is result["safe_terminal"]
        ):
            return False
        outcome = result.get("outcome")
        allowed_outcomes = (
            {"recovered", "recovery_required"}
            if is_recovery
            else {
                "applied",
                "aborted_preflight",
                "rolled_back",
                "recovery_required",
            }
        )
        if (
            type(outcome) is not str
            or outcome not in allowed_outcomes
            or result["safe_terminal"]
            is (outcome == "recovery_required")
        ):
            return False
        targets = result.get("targets")
        prepared_targets = prepared.get("targets")
        if (
            type(targets) is not list
            or type(prepared_targets) is not list
            or len(targets) != result["target_count"]
            or len(targets) != len(prepared_targets)
        ):
            return False
        saw_recovery = False
        saw_preflight_conflict = False
        for index, (target, expected) in enumerate(
            zip(targets, prepared_targets), start=1
        ):
            if (
                type(target) is not dict
                or frozenset(target)
                != _DURABLE_MULTI_EDIT_TARGET_OUTCOME_KEYS
                or target.get("index") != index
                or type(target.get("index")) is not int
                or self._normalized_script_path(
                    target.get("path"), allow_empty=False
                )
                != self._normalized_script_path(
                    expected.get("path"), allow_empty=False
                )
            ):
                return False
            for field_name in (
                "expected_sha256",
                "prepared_sha256",
                "planned_sha256",
                "source_length",
                "planned_source_length",
                "edit_count",
                "replacement_count",
            ):
                if target.get(field_name) != expected.get(field_name):
                    return False
            before = target.get("observed_before_sha256")
            after = target.get("observed_after_sha256")
            if (
                type(before) is not str
                or (before and SHA256_RE.fullmatch(before) is None)
                or type(after) is not str
                or (after and SHA256_RE.fullmatch(after) is None)
                or target.get("status")
                not in {
                    "applied",
                    "rolled_back",
                    "not_applied",
                    "recovery_required",
                }
            ):
                return False
            status = target["status"]
            saw_recovery = saw_recovery or status == "recovery_required"
            saw_preflight_conflict = saw_preflight_conflict or (
                not before
                or before != expected["prepared_sha256"]
            )

            prepared_sha = expected["prepared_sha256"]
            planned_sha = expected["planned_sha256"]
            if expected_phase == "apply":
                if outcome == "applied" and (
                    status != "applied"
                    or before != prepared_sha
                    or after != planned_sha
                ):
                    return False
                if outcome == "aborted_preflight" and (
                    status != "not_applied"
                    or before != after
                ):
                    return False
                if outcome == "rolled_back" and (
                    status not in {"rolled_back", "not_applied"}
                    or before != prepared_sha
                    or after != prepared_sha
                ):
                    return False
                if outcome == "recovery_required":
                    if status not in {
                        "rolled_back",
                        "not_applied",
                        "recovery_required",
                    }:
                        return False
                    if status in {"rolled_back", "not_applied"} and (
                        before != prepared_sha
                        or after != prepared_sha
                    ):
                        return False
            else:
                if outcome == "recovered" and (
                    status != "rolled_back"
                    or before not in {prepared_sha, planned_sha}
                    or after != prepared_sha
                ):
                    return False
                if outcome == "recovery_required":
                    if status not in {
                        "rolled_back",
                        "recovery_required",
                    }:
                        return False
                    if status == "rolled_back" and (
                        before not in {prepared_sha, planned_sha}
                        or after != prepared_sha
                    ):
                        return False
        if outcome == "aborted_preflight" and not saw_preflight_conflict:
            return False
        if (outcome == "recovery_required") is not saw_recovery:
            return False
        if (
            type(result.get("receipt_sha256")) is not str
            or SHA256_RE.fullmatch(result["receipt_sha256"]) is None
        ):
            return False
        try:
            if result["receipt_sha256"] != mutation_receipt_sha256(
                result
            ):
                return False
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeEncodeError,
            RecursionError,
            ValidationError,
        ):
            return False
        return len(encoded) <= MAX_MULTI_EDIT_RECEIPT_BYTES

    def _valid_multi_edit_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        if (
            pending.remote_tool == "studio_multi_edit"
            and pending.arguments.get("_phase") == "prepare"
        ):
            return self._valid_multi_edit_prepare_result(
                pending, result
            )
        if pending.remote_tool in {
            "studio_multi_edit",
            "studio_recover_multi_edit",
        }:
            return self._valid_multi_edit_mutation_result(
                pending, result
            )
        return False

    @staticmethod
    def _controller_mode_from_predicates(
        predicates: Dict[str, Dict[str, Any]]
    ) -> str:
        def observed(name: str, expected: bool) -> bool:
            predicate = predicates[name]
            return (
                predicate["read_ok"]
                and predicate["value"] is expected
            )

        if (
            observed("is_edit", True)
            and observed("is_running", False)
            and observed("edit_mode_active", True)
        ):
            return "edit"
        if observed("is_running", True) or observed(
            "edit_mode_active", False
        ):
            return "play"
        return "unknown"

    def _valid_durable_play_result(
        self,
        play: Any,
        mode: str,
        mode_source: str,
        predicates: Dict[str, Dict[str, Any]],
    ) -> bool:
        if not isinstance(play, dict):
            return False
        play_keys = frozenset(play)

        if mode_source == "controller_predicates":
            allowed_key_sets = {
                _DURABLE_CONTROLLER_PLAY_KEYS,
                _DURABLE_CONTROLLER_PLAY_KEYS
                | _DURABLE_CONTROLLER_LAST_PLAY_KEYS,
                _DURABLE_CONTROLLER_PLAY_KEYS
                | _DURABLE_CONTROLLER_LAST_PLAY_KEYS
                | {"last_failure_code"},
            }
            if play_keys not in allowed_key_sets:
                return False
            if play.get("active") is not False or play.get("state") != "edit":
                return False
            if mode != self._controller_mode_from_predicates(predicates):
                return False
            if "last_state" in play:
                outcome = play["last_outcome"]
                if (
                    not isinstance(outcome, str)
                    or play["last_state"] != outcome
                    or outcome not in _DURABLE_LAST_PLAY_OUTCOMES
                    or not self._canonical_uuid(
                        play["last_transition_nonce"]
                    )
                ):
                    return False
                if "last_failure_code" in play and (
                    outcome != "start_failed_edit_confirmed"
                    or not isinstance(play["last_failure_code"], str)
                    or play["last_failure_code"]
                    not in _DURABLE_PLAY_FAILURE_CODES
                ):
                    return False
            return True

        if (
            not _DURABLE_TRANSITION_PLAY_KEYS.issubset(play_keys)
            or not play_keys.issubset(
                _DURABLE_TRANSITION_PLAY_KEYS
                | _DURABLE_TRANSITION_OPTIONAL_PLAY_KEYS
            )
        ):
            return False
        state = play.get("state")
        if not isinstance(state, str):
            return False
        expected_mode = _DURABLE_TRANSITION_MODE_BY_STATE.get(state)
        if expected_mode is None or mode != expected_mode:
            return False
        expected_active = state in {"play", "stopping"}
        if (
            play.get("active") is not expected_active
            or play.get("accepted") is not True
            or type(play.get("server_ready")) is not bool
            or type(play.get("runner_finished")) is not bool
            or not self._canonical_uuid(play.get("transition_nonce"))
        ):
            return False
        if "stop_command_id" in play and not self._canonical_uuid(
            play["stop_command_id"]
        ):
            return False
        if "error" in play and not self._bounded_text(
            play["error"], 240, allow_empty=True
        ):
            return False

        has_stop = "stop_command_id" in play
        has_error = "error" in play
        server_ready = play["server_ready"]
        runner_finished = play["runner_finished"]
        if state == "starting":
            return (
                not server_ready
                and not runner_finished
                and not has_stop
                and not has_error
            )
        if state == "play":
            return (
                server_ready
                and not runner_finished
                and not has_stop
                and not has_error
            )
        if state == "stopping":
            return not runner_finished and has_stop
        if state == "settling":
            return runner_finished
        return (
            state == "recovery_required"
            and not server_ready
            and not runner_finished
            and not has_stop
            and has_error
        )

    def _valid_durable_state_result(self, result: Any) -> bool:
        if (
            not isinstance(result, dict)
            or frozenset(result) != _DURABLE_STATE_KEYS
        ):
            return False
        if (
            result.get("adapter") != "studio-mcp-v2-durable-plugin"
            or result.get("source") != "studio_controller"
            or result.get("connected") is not True
            or result.get("studio_id") != self.studio_id
            or result.get("client_instance_id") != self.client_instance_id
            or result.get("document_epoch") != self.document_epoch
            or type(result.get("generation")) is not int
            or result.get("generation") != self.generation
        ):
            return False
        if not self._canonical_uuid(result.get("broker_instance_id")):
            return False
        run_id = result.get("run_id")
        session_tag = result.get("session_tag")
        name = result.get("name")
        if (
            not isinstance(run_id, str)
            or not 16 <= len(run_id) <= 64
            or not run_id.isascii()
            or not run_id.isalnum()
            or run_id != self.metadata.get("run_id")
            or not isinstance(session_tag, str)
            or len(session_tag) != 12
            or any(
                character not in "0123456789abcdef"
                for character in session_tag
            )
            or session_tag != self.metadata.get("session_tag")
            or not self._bounded_text(name, 256)
            or name != self.metadata.get("name")
        ):
            return False
        for document_id in ("place_id", "game_id"):
            value = result.get(document_id)
            if (
                type(value) is not int
                or value < 0
                or value != self.metadata.get(document_id)
            ):
                return False

        mode = result.get("mode")
        mode_source = result.get("mode_source")
        if (
            type(mode) is not str
            or mode not in _DURABLE_STATE_MODES
            or type(result.get("is_edit")) is not bool
            or result["is_edit"] != (mode == "edit")
            or type(mode_source) is not str
            or mode_source not in _DURABLE_STATE_MODE_SOURCES
        ):
            return False

        if result.get("controller_context") != {
            "role": "edit_controller",
            "datamodel_type": "Edit",
            "request_channel_available": True,
        }:
            return False
        if result.get("available_datamodel_types") != ["Edit"]:
            return False

        predicates = result.get("raw_mode_predicates")
        if (
            not isinstance(predicates, dict)
            or frozenset(predicates) != _DURABLE_STATE_RAW_PREDICATES
        ):
            return False
        for predicate in predicates.values():
            if not isinstance(predicate, dict):
                return False
            read_ok = predicate.get("read_ok")
            if type(read_ok) is not bool:
                return False
            expected_keys = (
                frozenset({"read_ok", "value"})
                if read_ok
                else frozenset({"read_ok"})
            )
            if frozenset(predicate) != expected_keys:
                return False
            if read_ok and type(predicate["value"]) is not bool:
                return False
        return self._valid_durable_play_result(
            result.get("play"),
            mode,
            mode_source,
            predicates,
        )

    def _observe_multi_edit_receipt(
        self, pending: PendingRequest, result: Dict[str, Any]
    ) -> None:
        if result.get("phase") == "prepare":
            return
        transaction_id = result["transaction_id"]
        if result.get("recovery_required") is True:
            self.uncertain_requests[pending.request_id] = {
                "generation": pending.generation,
                "operation": pending.remote_tool,
                "reason": "validated_multi_edit_recovery_required",
                "transaction_id": transaction_id,
            }
            self.uncertain_pending[pending.request_id] = pending
            self._set_multi_edit_recovery(
                transaction_id,
                pending.request_id,
                pending.generation,
                "validated_recovery_required",
            )
            return
        if result.get("safe_terminal") is True:
            resolver_job_id = ""
            for candidate in self.jobs.values():
                if pending.request_id in candidate.dispatched_request_ids:
                    resolver_job_id = candidate.job_id
                    break
            encoded_resolution = canonical_json_bytes(result)
            resolution_receipt = {
                "format": "studio-mcp-v2-job-resolution",
                "schema_version": 1,
                "kind": "exact_multi_edit_recovery",
                "studio_id": self.studio_id,
                "client_instance_id": self.client_instance_id,
                "document_epoch": self.document_epoch,
                "generation": pending.generation,
                "request_id": pending.request_id,
                "transaction_id": transaction_id,
                "operation": "studio_recover_multi_edit",
                "phase": "recover",
                "source": (
                    "job" if resolver_job_id else "direct"
                ),
                "resolver_job_id": resolver_job_id,
                "success": True,
                "safe_terminal": True,
                "recovery_required": False,
                "outcome": "recovered",
                "receipt_sha256": result["receipt_sha256"],
                "result_sha256": canonical_json_sha256(result),
                "result_bytes": len(encoded_resolution),
                "result": copy.deepcopy(result),
            }
            for request_id, context in list(
                self.uncertain_pending.items()
            ):
                if (
                    context.remote_tool
                    in {
                        "studio_multi_edit",
                        "studio_recover_multi_edit",
                    }
                    and context.arguments.get("transaction_id")
                    == transaction_id
                ):
                    self.uncertain_pending.pop(request_id, None)
                    self.uncertain_requests.pop(request_id, None)
            if (
                self.multi_edit_recovery is not None
                and self.multi_edit_recovery.get("transaction_id")
                == transaction_id
            ):
                self.multi_edit_recovery = None
            if (
                self.multi_edit_prepared_receipt is not None
                and self.multi_edit_prepared_receipt.get("transaction_id")
                == transaction_id
            ):
                self.multi_edit_prepared_receipt = None
            self._refresh_uncertainty()
            for job in list(self.jobs.values()):
                if (
                    job.transaction_id == transaction_id
                    and job.status == "outcome_unknown"
                ):
                    if (
                        result.get("phase") != "recover"
                        or result.get("operation")
                        != "studio_recover_multi_edit"
                        or result.get("outcome") != "recovered"
                    ):
                        # A matching safe late apply receipt is recorded by
                        # _record_job_response using its own request
                        # correlation. Only an independently dispatched exact
                        # recovery may externally resolve another job.
                        continue
                    if any(
                        receipt.get("request_id")
                        == pending.request_id
                        for receipt in job.resolution_receipts
                    ):
                        continue
                    if (
                        len(job.resolution_receipts)
                        >= MAX_JOB_RESOLUTION_RECEIPTS
                    ):
                        # A terminal job can normally be resolved only once.
                        # Preserve outcome_unknown rather than discarding an
                        # audit link if corrupted state reaches this bound.
                        continue
                    job.status = "completed"
                    job.resolution_receipts.append(
                        copy.deepcopy(resolution_receipt)
                    )
                    job.terminal_outcome = (
                        "resolved_by_exact_recovery:recovered"
                    )
                    job.error = None
                    job.updated_at = time.time()
                    self._finalize_job_storage(job)

    def _record_job_response(
        self,
        pending: PendingRequest,
        *,
        success: bool,
        result: Any = None,
        error: Any = None,
        late: bool = False,
    ) -> None:
        phase = pending.arguments.get("_phase")
        if not isinstance(phase, str):
            phase = (
                "recover"
                if pending.remote_tool == "studio_recover_multi_edit"
                else "direct"
            )
        mutation_phase = phase in {"apply", "recover"}
        for job in self.jobs.values():
            if pending.request_id not in job.dispatched_request_ids:
                continue
            if any(
                receipt.get("request_id") == pending.request_id
                for receipt in job.phase_receipts
            ):
                return
            descriptor: Dict[str, Any] = {
                "request_id": pending.request_id,
                "generation": pending.generation,
                "phase": phase,
                "success": success,
                "safe_terminal": False,
                "recovery_required": False,
                "outcome": None,
                "result_sha256": None,
                "result_bytes": None,
            }
            if success:
                encoded = canonical_json_bytes(result)
                result_sha256 = canonical_json_sha256(result)
                result_outcome = (
                    result.get("outcome")
                    if isinstance(result, dict)
                    and isinstance(result.get("outcome"), str)
                    else "completed"
                )
                recovery_required = bool(
                    mutation_phase
                    and isinstance(result, dict)
                    and result.get("recovery_required") is True
                )
                descriptor.update(
                    {
                        "safe_terminal": not recovery_required,
                        "recovery_required": recovery_required,
                        "outcome": result_outcome,
                        "result_sha256": result_sha256,
                        "result_bytes": len(encoded),
                    }
                )
                if phase != "prepare":
                    job.result = copy.deepcopy(result)
                    job.result_sha256 = result_sha256
                    job.result_bytes = len(encoded)
                    job.terminal_outcome = (
                        result_outcome
                        if result_outcome != "completed"
                        else (
                            "completed_late_receipt"
                            if late
                            else "completed"
                        )
                    )
                    job.error = None
                    job.status = (
                        "outcome_unknown"
                        if recovery_required
                        else "completed"
                    )
            else:
                safe_error = not mutation_phase
                descriptor.update(
                    {
                        "safe_terminal": safe_error,
                        "recovery_required": not safe_error,
                        "outcome": (
                            "failed"
                            if safe_error
                            else "mutation_error_unproven"
                        ),
                    }
                )
                if safe_error:
                    message = (
                        error.get("message")
                        if isinstance(error, dict)
                        and isinstance(error.get("message"), str)
                        else "Studio tool failed"
                    )
                    job.status = "failed"
                    job.terminal_outcome = (
                        "prepare_failed_no_apply"
                        if phase == "prepare"
                        else (
                            "failed_late_receipt"
                            if late
                            else "failed"
                        )
                    )
                    job.error = {
                        "code": "studio_tool_error",
                        "message": message[:240],
                    }
                else:
                    job.status = "outcome_unknown"
                    job.terminal_outcome = (
                        "mutation_error_without_safe_receipt"
                    )
            job.phase_receipts.append(descriptor)
            job.updated_at = time.time()
            if job.status in self.TERMINAL_JOB_STATES:
                self._finalize_job_storage(job)
            return

    def _settle_safe_unapplied_job(
        self, request_id: str, terminal_outcome: str
    ) -> None:
        for job in self.jobs.values():
            if (
                request_id not in job.dispatched_request_ids
                or job.status != "outcome_unknown"
            ):
                continue
            job.status = "failed"
            job.terminal_outcome = terminal_outcome
            job.error = {
                "code": "multi_edit_not_applied",
                "message": (
                    "Multi-edit prepare settled, but apply was not "
                    "dispatched"
                ),
            }
            job.updated_at = time.time()
            self._finalize_job_storage(job)
            return

    def _settle_safe_missing_receipt_job(self, request_id: str) -> None:
        for job in self.jobs.values():
            if (
                request_id not in job.dispatched_request_ids
                or job.status != "outcome_unknown"
            ):
                continue
            job.status = "failed"
            job.terminal_outcome = (
                "settled_terminal_receipt_unavailable"
            )
            job.error = {
                "code": "terminal_receipt_unavailable",
                "message": (
                    "Studio proved the request terminal during reconnect, "
                    "but no validated result receipt was retained"
                ),
            }
            job.updated_at = time.time()
            self._finalize_job_storage(job)
            return

    def _settle_late_job(
        self,
        request_id: str,
        *,
        success: bool,
        result: Any = None,
        error: Any = None,
    ) -> None:
        for job in self.jobs.values():
            if (
                request_id not in job.dispatched_request_ids
                or job.status != "outcome_unknown"
            ):
                continue
            if success:
                if (
                    job.remote_tool == "studio_multi_edit"
                    and isinstance(result, dict)
                    and result.get("phase") == "prepare"
                ):
                    self._settle_safe_unapplied_job(
                        request_id,
                        "prepare_completed_after_deadline_no_apply",
                    )
                    return
                job.status = "completed"
                job.result = copy.deepcopy(result)
                encoded = canonical_json_bytes(result)
                job.result_bytes = len(encoded)
                job.result_sha256 = canonical_json_sha256(result)
                job.terminal_outcome = (
                    result.get("outcome")
                    if isinstance(result, dict)
                    and isinstance(result.get("outcome"), str)
                    else "completed_late_receipt"
                )
                job.error = None
            else:
                job.status = "failed"
                job.terminal_outcome = "failed_late_receipt"
                job.error = (
                    copy.deepcopy(error)
                    if isinstance(error, dict)
                    else {
                        "code": "studio_tool_error",
                        "message": "Studio tool failed",
                    }
                )
            job.updated_at = time.time()
            self._finalize_job_storage(job)
            return

    def _valid_success_result(
        self, pending: PendingRequest, result: Any
    ) -> bool:
        if pending.remote_tool == "studio_get_state":
            return self._valid_durable_state_result(result)
        if pending.remote_tool == "studio_list_tree":
            return self._valid_durable_tree_result(pending, result)
        if pending.remote_tool in {
            "studio_search_scripts",
            "studio_grep_scripts",
        }:
            return self._valid_durable_script_result(pending, result)
        if pending.remote_tool == "studio_inspect_instance":
            return self._valid_durable_inspection_result(
                pending, result
            )
        if pending.remote_tool in {
            "studio_multi_edit",
            "studio_recover_multi_edit",
        }:
            return self._valid_multi_edit_result(pending, result)
        return True

    def receive_response(
        self,
        generation: int,
        request_id: str,
        *,
        success: bool,
        result: Any = None,
        error: Any = None,
    ) -> bool:
        if generation != self.generation:
            return False
        pending = self.pending.get(request_id)
        if pending is None:
            uncertain = self.uncertain_requests.get(request_id)
            if uncertain is not None and uncertain.get("generation") == generation:
                context = self.uncertain_pending.get(request_id)
                if context is None:
                    return False
                if success:
                    if not self._valid_success_result(context, result):
                        return False
                    self._record_job_response(
                        context,
                        success=True,
                        result=result,
                        late=True,
                    )
                    if context.remote_tool in {
                        "studio_multi_edit",
                        "studio_recover_multi_edit",
                    }:
                        self._observe_multi_edit_receipt(
                            context, result
                        )
                        if result.get("recovery_required") is True:
                            return True
                    self._settle_late_job(
                        request_id,
                        success=True,
                        result=result,
                    )
                    self.uncertain_requests.pop(request_id, None)
                    self.uncertain_pending.pop(request_id, None)
                    self._refresh_uncertainty()
                    return True
                self._record_job_response(
                    context,
                    success=False,
                    error=error,
                    late=True,
                )
                if (
                    context.remote_tool
                    in {
                        "studio_multi_edit",
                        "studio_recover_multi_edit",
                    }
                    and context.arguments.get("_phase") != "prepare"
                ):
                    # A generic mutation error does not prove no source
                    # changed. Exact recovery remains required.
                    return False
                self.uncertain_requests.pop(request_id, None)
                self.uncertain_pending.pop(request_id, None)
                self._settle_late_job(
                    request_id,
                    success=False,
                    error=error,
                )
                self._refresh_uncertainty()
                return True
            return False
        if pending.generation != generation or pending.future.done():
            return False
        # Receipt itself proves the remote operation reached a terminal state.
        # Remove correlation immediately so a reconnect before the waiter
        # resumes cannot misclassify it as outcome-unknown.
        self.pending.pop(request_id, None)
        if success:
            if (
                pending.remote_tool
                in {
                    "studio_multi_edit",
                    "studio_recover_multi_edit",
                }
                and not self._valid_multi_edit_result(
                    pending, result
                )
            ):
                if pending.arguments.get("_phase") == "prepare":
                    pending.future.set_exception(
                        RemoteToolError(
                            "Targeted Studio returned an invalid "
                            "multi-edit prepare receipt"
                        )
                    )
                    return True
                self.uncertain_requests[pending.request_id] = {
                    "generation": pending.generation,
                    "operation": pending.remote_tool,
                    "reason": "invalid_mutation_receipt",
                    "transaction_id": pending.arguments.get(
                        "transaction_id"
                    ),
                }
                self.uncertain_pending[pending.request_id] = pending
                self._set_multi_edit_recovery(
                    pending.arguments.get("transaction_id", ""),
                    pending.request_id,
                    pending.generation,
                    "invalid_mutation_receipt",
                )
                pending.future.set_exception(
                    RemoteToolError(
                        "Targeted Studio returned an invalid mutation "
                        "receipt; the session remains quarantined"
                    )
                )
                return True
            if pending.remote_tool in {
                "studio_multi_edit",
                "studio_recover_multi_edit",
            }:
                self._observe_multi_edit_receipt(pending, result)
            if (
                pending.remote_tool == "studio_get_state"
                and not self._valid_durable_state_result(result)
            ):
                pending.future.set_exception(
                    RemoteToolError(
                        "Targeted Studio returned an invalid state response"
                    )
                )
                return True
            if (
                pending.remote_tool == "studio_list_tree"
                and not self._valid_durable_tree_result(
                    pending, result
                )
            ):
                pending.future.set_exception(
                    RemoteToolError(
                        "Targeted Studio returned an invalid tree response"
                    )
                )
                return True
            if (
                pending.remote_tool
                in {"studio_search_scripts", "studio_grep_scripts"}
                and not self._valid_durable_script_result(
                    pending, result
                )
            ):
                pending.future.set_exception(
                    RemoteToolError(
                        "Targeted Studio returned an invalid script "
                        "query response"
                    )
                )
                return True
            if (
                pending.remote_tool == "studio_inspect_instance"
                and not self._valid_durable_inspection_result(
                    pending, result
                )
            ):
                pending.future.set_exception(
                    RemoteToolError(
                        "Targeted Studio returned an invalid instance "
                        "inspection response"
                    )
                )
                return True
            self._record_job_response(
                pending,
                success=True,
                result=result,
            )
            self._observe_result(
                pending.remote_tool, pending.arguments, result
            )
            pending.future.set_result(copy.deepcopy(result))
        else:
            self._record_job_response(
                pending,
                success=False,
                error=error,
            )
            if (
                pending.remote_tool
                in {
                    "studio_multi_edit",
                    "studio_recover_multi_edit",
                }
                and pending.arguments.get("_phase") != "prepare"
            ):
                self.uncertain_requests[pending.request_id] = {
                    "generation": pending.generation,
                    "operation": pending.remote_tool,
                    "reason": "mutation_error_without_safe_receipt",
                    "transaction_id": pending.arguments.get(
                        "transaction_id"
                    ),
                }
                self.uncertain_pending[pending.request_id] = pending
                self._set_multi_edit_recovery(
                    pending.arguments.get("transaction_id", ""),
                    pending.request_id,
                    pending.generation,
                    "mutation_error_without_safe_receipt",
                )
            message = (
                error.get("message")
                if isinstance(error, dict) and isinstance(error.get("message"), str)
                else str(error or "Studio tool failed")
            )
            pending.future.set_exception(RemoteToolError(message))
        return True

    def receive_event(
        self, generation: int, event_type: str, payload: Dict[str, Any]
    ) -> bool:
        if generation != self.generation or not self.connected:
            return False
        if event_type == "console":
            self.console_sequence += 1
            self.console.append(
                {
                    "sequence": self.console_sequence,
                    "generation": generation,
                    "payload": copy.deepcopy(payload),
                }
            )
            return True
        if event_type == "mode":
            mode = payload.get("mode")
            if isinstance(mode, str):
                self.mode = mode
                self.last_confirmed_mode = mode.lower()
                self._refresh_uncertainty()
                return True
            return False
        if event_type == "job":
            # Jobs are broker-owned wrappers around ordinary correlated
            # requests. Studio does not receive job_id and may not forge job
            # status, cancellation, or terminal results through events.
            return False
        return False

    def _observe_result(
        self, remote_tool: str, arguments: Dict[str, Any], result: Any
    ) -> None:
        normalized = remote_tool.lower()
        if normalized in {
            "start_stop_play",
            "startstopplay",
            "studio_start_stop_play",
        }:
            action = arguments.get("mode")
            if action is None:
                action = "start_play" if arguments.get("is_start") else "stop"
            result_mode = (
                result.get("mode")
                if isinstance(result, dict)
                else None
            )
            if isinstance(result_mode, str):
                self.mode = result_mode
                self.last_confirmed_mode = result_mode.lower()
            else:
                self.mode = {
                    "start_play": "starting",
                    "run_server": "starting",
                    "stop": "stopping",
                }.get(str(action), self.mode)
                self.last_confirmed_mode = self.mode.lower()
            self.uncertainty_state = None
        elif normalized in {
            "get_studio_state",
            "getstudiomode",
            "studio_get_state",
        }:
            if isinstance(result, dict) and isinstance(result.get("mode"), str):
                self.mode = result["mode"]
                self.last_confirmed_mode = self.mode.lower()
                self.uncertainty_state = None
            elif isinstance(result, str):
                self.mode = result
                self.last_confirmed_mode = self.mode.lower()
                self.uncertainty_state = None

    def start_job(
        self,
        public_tool: str,
        remote_tool: str,
        arguments: Dict[str, Any],
        timeout_ms: int,
        before_dispatch: Optional[Callable[[], None]] = None,
        *,
        input_schema_sha256: str = "",
        output_schema_sha256: str = "",
        handler_contract_sha256: str = "",
    ) -> JobRecord:
        admitted_arguments = copy.deepcopy(arguments)
        active_jobs = sum(
            job.status not in self.TERMINAL_JOB_STATES
            for job in self.jobs.values()
        )
        if active_jobs >= MAX_ACTIVE_SESSION_JOBS:
            raise SessionConflictError(
                "This Studio already has the maximum number of active jobs"
            )
        admitted_contract = _job_admitted_contract(
            remote_tool, admitted_arguments
        )
        required_bytes = len(
            canonical_json_bytes(
                {
                    "arguments": admitted_arguments,
                    "admitted_contract": admitted_contract,
                }
            )
        )
        self._compact_terminal_jobs(required_bytes=required_bytes)
        retained_bytes = sum(
            self._job_retained_bytes(job)
            for job in self.jobs.values()
        )
        if (
            len(self.jobs) >= MAX_SESSION_JOBS
            or retained_bytes + required_bytes
            > MAX_RETAINED_JOB_BYTES
        ):
            raise SessionConflictError(
                "Session job retention is full of active or uncertain "
                "records; no history was discarded"
            )
        admission = self.reserve_operation()
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            studio_id=self.studio_id,
            generation=self.generation,
            public_tool=public_tool,
            remote_tool=remote_tool,
            arguments=copy.deepcopy(admitted_arguments),
            timeout_ms=timeout_ms,
            client_instance_id=self.client_instance_id,
            document_epoch=self.document_epoch,
            input_schema_sha256=input_schema_sha256,
            output_schema_sha256=output_schema_sha256,
            handler_contract_sha256=handler_contract_sha256,
            arguments_sha256=canonical_json_sha256(
                admitted_arguments
            ),
            admitted_contract=admitted_contract,
            admission_sequence=admission.sequence,
            admission=admission,
        )
        self.jobs[job.job_id] = job

        def mark_dispatched(
            operation_request_id: str,
            phase: str,
            transaction_id: Optional[str],
        ) -> None:
            if job.status in self.TERMINAL_JOB_STATES:
                raise StaleGenerationError(
                    "Job became terminal before Studio dispatch"
                )
            job.dispatched = True
            job.status = "running"
            job.dispatched_request_ids.append(operation_request_id)
            job.dispatched_phases.append(phase)
            if transaction_id is not None:
                if (
                    job.transaction_id is not None
                    and job.transaction_id != transaction_id
                ):
                    raise StaleGenerationError(
                        "Job transaction identity changed between phases"
                    )
                job.transaction_id = transaction_id
            job.updated_at = time.time()

        async def run() -> None:
            try:
                if job.status in self.TERMINAL_JOB_STATES:
                    return
                result = await self.invoke(
                    remote_tool,
                    copy.deepcopy(admitted_arguments),
                    timeout_ms,
                    expected_generation=job.generation,
                    before_dispatch=before_dispatch,
                    on_dispatched=mark_dispatched,
                    admission=admission,
                )
                job.result = result
                encoded_result = canonical_json_bytes(result)
                job.result_bytes = len(encoded_result)
                job.result_sha256 = canonical_json_sha256(result)
                if (
                    remote_tool
                    in {
                        "studio_multi_edit",
                        "studio_recover_multi_edit",
                    }
                    and isinstance(result, dict)
                    and result.get("recovery_required") is True
                ):
                    job.status = "outcome_unknown"
                    job.terminal_outcome = "recovery_required"
                else:
                    job.status = "completed"
                    job.terminal_outcome = (
                        result.get("outcome")
                        if isinstance(result, dict)
                        and isinstance(result.get("outcome"), str)
                        else "completed"
                    )
            except asyncio.CancelledError:
                if job.status in self.TERMINAL_JOB_STATES:
                    return
                if job.dispatched:
                    job.status = "outcome_unknown"
                    job.cancellation_state = (
                        "requested_after_dispatch_not_acknowledged"
                    )
                    job.terminal_outcome = (
                        "local_wait_cancelled_after_dispatch"
                    )
                else:
                    job.status = "cancelled"
                    job.cancellation_state = (
                        "acknowledged_before_dispatch"
                    )
                    job.terminal_outcome = "cancelled_before_dispatch"
            except (SessionDisconnectedError, StaleGenerationError) as exc:
                if job.status in self.TERMINAL_JOB_STATES:
                    return
                job.error = exc.as_dict()
                if job.dispatched:
                    job.status = "outcome_unknown"
                    job.terminal_outcome = (
                        "connection_or_generation_lost_after_dispatch"
                    )
                else:
                    job.status = "failed"
                    job.terminal_outcome = (
                        "not_dispatched_connection_or_generation_lost"
                    )
            except Exception as exc:
                if job.status in self.TERMINAL_JOB_STATES:
                    return
                if hasattr(exc, "as_dict"):
                    job.error = exc.as_dict()
                else:
                    job.error = {
                        "code": "internal_error",
                        "message": "Job execution failed internally",
                    }
                if any(
                    request_id in self.uncertain_requests
                    for request_id in job.dispatched_request_ids
                ):
                    job.status = "outcome_unknown"
                    job.terminal_outcome = (
                        "receipt_or_outcome_unproven"
                    )
                else:
                    job.status = "failed"
                    job.terminal_outcome = "failed"
            finally:
                job.updated_at = time.time()
                self._finalize_job_storage(job)
                self._recompute_terminal_disconnect_candidate()

        job.task = asyncio.create_task(run())
        return job

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise JobNotFoundError(
                "No such job exists in the explicitly targeted Studio session"
            )

    def cancel_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job.status in self.TERMINAL_JOB_STATES:
            return job
        if job.dispatched:
            job.cancellation_state = (
                "requested_after_dispatch_refused"
            )
            raise UnsafeCancellationError(
                "The job was already sent to Studio; v2 will not claim it was cancelled"
            )
        if job.task is not None:
            if job.admission is not None:
                job.admission.retire_after_predecessor()
            job.task.cancel()
        job.status = "cancelled"
        job.cancellation_state = "acknowledged_before_dispatch"
        job.terminal_outcome = "cancelled_before_dispatch"
        job.updated_at = time.time()
        self._finalize_job_storage(job)
        return job
