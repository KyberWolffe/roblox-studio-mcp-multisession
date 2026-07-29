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


class LongPollTransport:
    """One Studio connection generation's outbound request queue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def send(self, envelope: Dict[str, Any]) -> None:
        if self._closed:
            raise SessionDisconnectedError("Studio transport is closed")
        await self._queue.put(copy.deepcopy(envelope))

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

    def snapshot(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "job_id": self.job_id,
            "studio_id": self.studio_id,
            "generation": self.generation,
            "tool_name": self.public_tool,
            "status": self.status,
            "dispatched": self.dispatched,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.result is not None:
            payload["result"] = copy.deepcopy(self.result)
        if self.error is not None:
            payload["error"] = copy.deepcopy(self.error)
        return payload


class StudioSession:
    """All operational state and serialization for one explicit Studio ID."""

    TERMINAL_JOB_STATES = frozenset(
        {"completed", "failed", "cancelled", "disconnected"}
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
        self.pending: Dict[str, PendingRequest] = {}
        self.used_request_ids: Set[str] = set()
        # Dispatched calls whose terminal outcome has not been proven. New
        # operations are quarantined until the same-generation late response
        # arrives or a reconnecting Studio supplies its settlement ledger.
        self.uncertain_requests: Dict[str, Dict[str, Any]] = {}
        # Conservative v2 baseline: every Studio-bound operation is exclusive.
        self.operation_lock = asyncio.Lock()
        self.last_seen_monotonic = time.monotonic()
        self.has_polled = False

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
            "uncertain_request_count": len(self.uncertain_requests),
            "pending_count": len(self.pending),
            "job_counts": self._job_counts(),
            "console_sequence": self.console_sequence,
        }

    def _job_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for job in self.jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        return counts

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
                self.uncertain_requests.pop(request_id, None)
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
        terminal_candidate = (
            disconnect_mode == "edit"
            and self.uncertainty_state is None
            and self.play_bridge_uncertain is None
            and not self.operation_lock.locked()
            and not self.pending
            and not self.uncertain_requests
            and all(
                job.status in self.TERMINAL_JOB_STATES
                for job in self.jobs.values()
            )
        )
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.connected = False
        self.last_confirmed_mode = disconnect_mode
        self.mode = "unknown"
        self.disconnected_at_monotonic = time.monotonic()
        self.terminal_disconnect_candidate = terminal_candidate
        self.terminal_disconnect_reason = str(reason)[:160]
        error = SessionDisconnectedError(reason)
        for pending in list(self.pending.values()):
            self.uncertain_requests[pending.request_id] = {
                "generation": pending.generation,
                "operation": pending.remote_tool,
                "reason": "connection_lost_after_dispatch",
            }
            if not pending.future.done():
                pending.future.set_exception(error)
        self.pending.clear()
        self._refresh_uncertainty(
            fallback=None if terminal_candidate else reason
        )
        for job in self.jobs.values():
            if job.status not in self.TERMINAL_JOB_STATES:
                job.status = "disconnected"
                job.error = error.as_dict()
                job.updated_at = time.time()
                if not job.dispatched and job.task is not None:
                    job.task.cancel()

    def assert_generation_online(self, admitted_generation: int) -> None:
        if admitted_generation != self.generation:
            raise StaleGenerationError(
                "The operation was admitted before this Studio reconnected"
            )
        if not self.connected or self.transport is None:
            raise SessionDisconnectedError("The explicitly targeted Studio is offline")
        if self.uncertain_requests:
            raise SessionConflictError(
                "This Studio is quarantined until prior dispatched request "
                "outcomes are reconciled"
            )

    def assert_operation_admissible(
        self, admitted_generation: int, remote_tool: str
    ) -> None:
        self.assert_generation_online(admitted_generation)
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
        on_dispatched: Optional[Callable[[], None]] = None,
    ) -> Any:
        admitted_generation = (
            self.generation
            if expected_generation is None
            else expected_generation
        )
        async with self.operation_lock:
            # Critical no-replay fence for calls that waited through reconnect.
            self.assert_operation_admissible(
                admitted_generation, remote_tool
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
                    on_dispatched()
                result = await asyncio.wait_for(
                    asyncio.shield(future), timeout_ms / 1000
                )
                # A response can wake this task just before a reconnect. Fence
                # the result and its state observations against that race.
                self.assert_generation_online(admitted_generation)
                return result
            except asyncio.TimeoutError:
                self.uncertain_requests[operation_request_id] = {
                    "generation": admitted_generation,
                    "operation": remote_tool,
                    "reason": "response_timeout_after_dispatch",
                }
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
                # A late response is not delivered to the timed-out caller, but
                # it proves the operation terminated and releases quarantine.
                self.uncertain_requests.pop(request_id, None)
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
            self._observe_result(
                pending.remote_tool, pending.arguments, result
            )
            pending.future.set_result(copy.deepcopy(result))
        else:
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
            job_id = payload.get("job_id")
            if not isinstance(job_id, str):
                return False
            job = self.jobs.get(job_id)
            if job is None or job.generation != generation:
                return False
            # Only known fields are updated; the Studio cannot retarget the job.
            status = payload.get("status")
            if isinstance(status, str):
                job.status = status
            job.updated_at = time.time()
            return True
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
    ) -> JobRecord:
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            studio_id=self.studio_id,
            generation=self.generation,
            public_tool=public_tool,
            remote_tool=remote_tool,
            arguments=copy.deepcopy(arguments),
            timeout_ms=timeout_ms,
        )
        self.jobs[job.job_id] = job

        def mark_dispatched() -> None:
            if job.status in self.TERMINAL_JOB_STATES:
                raise StaleGenerationError(
                    "Job became terminal before Studio dispatch"
                )
            job.dispatched = True
            job.status = "running"
            job.updated_at = time.time()

        async def run() -> None:
            try:
                if job.status in self.TERMINAL_JOB_STATES:
                    return
                result = await self.invoke(
                    remote_tool,
                    arguments,
                    timeout_ms,
                    expected_generation=job.generation,
                    before_dispatch=before_dispatch,
                    on_dispatched=mark_dispatched,
                )
                if job.status != "disconnected":
                    job.status = "completed"
                    job.result = result
            except asyncio.CancelledError:
                if job.status != "disconnected":
                    job.status = "cancelled"
            except (SessionDisconnectedError, StaleGenerationError) as exc:
                job.status = "disconnected"
                job.error = exc.as_dict()
            except Exception as exc:
                job.status = "failed"
                if hasattr(exc, "as_dict"):
                    job.error = exc.as_dict()
                else:
                    job.error = {
                        "code": "internal_error",
                        "message": str(exc),
                    }
            finally:
                job.updated_at = time.time()

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
            raise UnsafeCancellationError(
                "The job was already sent to Studio; v2 will not claim it was cancelled"
            )
        if job.task is not None:
            job.task.cancel()
        job.status = "cancelled"
        job.updated_at = time.time()
        return job
