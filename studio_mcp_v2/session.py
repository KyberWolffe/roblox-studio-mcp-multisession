from __future__ import annotations

import asyncio
import copy
import time
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
