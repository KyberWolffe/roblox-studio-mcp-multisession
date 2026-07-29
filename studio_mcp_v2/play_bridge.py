from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from .errors import AuthenticationError, SessionConflictError, ValidationError
from .validation import (
    validate_client_instance_id,
    validate_document_epoch,
    validate_generation,
    validate_request_id,
    validate_studio_id,
)


class PlayBridgeState(str, Enum):
    PREPARED = "prepared"
    ATTACHED = "attached"
    STOP_REQUESTED = "stop_requested"
    STOP_ACKED = "stop_acked"
    COMPLETED = "completed"


COMPLETION_OUTCOMES = frozenset(
    {
        "stopped_edit_confirmed",
        "natural_stop_edit_confirmed",
        "recovery_natural_stop_edit_confirmed",
        "start_failed_edit_confirmed",
    }
)


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _validate_nonce(value: Any, label: str) -> str:
    try:
        return validate_studio_id(value)
    except ValidationError:
        raise ValidationError(label + " must be a canonical lowercase UUID")


def _validate_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 32 <= len(value) <= 256:
        raise AuthenticationError(label + " is invalid")
    return value


def _validate_document_id(value: Any, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValidationError(label + " must be a nonnegative integer")
    return value


def _validate_ttl(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 5
        or value > maximum
    ):
        raise ValidationError(
            "ttl_seconds must be an integer between 5 and "
            + str(maximum)
        )
    return value


@dataclass
class PlayTransition:
    studio_id: str
    client_instance_id: str
    document_epoch: str
    transition_generation: int
    play_request_id: str
    expected_place_id: int
    expected_game_id: int
    transition_nonce: str
    attach_token_hash: bytes
    state: PlayBridgeState
    created_at: float
    expires_at: float
    attach_id: Optional[str] = None
    server_instance_id: Optional[str] = None
    server_token_hash: Optional[bytes] = None
    attached_at: Optional[float] = None
    bootstrap_burned: bool = False
    watchdog_ack_id: Optional[str] = None
    watchdog_armed_at: Optional[float] = None
    controller_stop_id: Optional[str] = None
    stop_command_id: Optional[str] = None
    stop_source: Optional[str] = None
    stop_requested_at: Optional[float] = None
    stop_ack_id: Optional[str] = None
    stop_acked_at: Optional[float] = None
    watchdog_deadline: Optional[float] = None
    watchdog_expired_reason: Optional[str] = None
    recovery_only: bool = False
    recovery_reason: Optional[str] = None
    completion_id: Optional[str] = None
    completion_outcome: Optional[str] = None
    completion_correlation: Optional[str] = None
    completion_proof: Optional[Tuple[Any, ...]] = None
    completed_at: Optional[float] = None


class PlayBridgeManager:
    """Per-Studio Play transitions with no active/default Studio pointer."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_ttl_seconds: int = 90,
        max_ttl_seconds: int = 300,
        active_ttl_seconds: int = 210,
        stop_watchdog_seconds: int = 12,
        token_key: Optional[bytes] = None,
    ) -> None:
        if not 5 <= default_ttl_seconds <= max_ttl_seconds:
            raise ValueError("Invalid default Play bridge TTL")
        if not 5 <= active_ttl_seconds <= max_ttl_seconds:
            raise ValueError("Invalid active Play bridge TTL")
        if not 1 <= stop_watchdog_seconds <= max_ttl_seconds:
            raise ValueError("Invalid Play bridge watchdog")
        self._clock = clock
        self.default_ttl_seconds = default_ttl_seconds
        self.max_ttl_seconds = max_ttl_seconds
        self.active_ttl_seconds = active_ttl_seconds
        self.stop_watchdog_seconds = stop_watchdog_seconds
        self._token_key = token_key or secrets.token_bytes(32)
        if len(self._token_key) < 32:
            raise ValueError("Play bridge token key must be at least 32 bytes")
        self._transitions: Dict[str, PlayTransition] = {}
        self._lock_map: Dict[str, threading.RLock] = {}
        self._lock_map_guard = threading.Lock()

    def _lock_for(self, studio_id: str) -> threading.RLock:
        with self._lock_map_guard:
            lock = self._lock_map.get(studio_id)
            if lock is None:
                lock = threading.RLock()
                self._lock_map[studio_id] = lock
            return lock

    def _derived_token(
        self, transition: PlayTransition, purpose: str
    ) -> str:
        material = "\0".join(
            (
                "studio-mcp-v2-play-bridge",
                purpose,
                transition.studio_id,
                transition.client_instance_id,
                transition.document_epoch,
                str(transition.transition_generation),
                transition.play_request_id,
                str(transition.expected_place_id),
                str(transition.expected_game_id),
                transition.transition_nonce,
            )
        ).encode("utf-8")
        digest = hmac.new(self._token_key, material, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    @staticmethod
    def _context(
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
    ) -> Tuple[str, str, str, int, str, int, int]:
        return (
            validate_studio_id(studio_id),
            validate_client_instance_id(client_instance_id),
            validate_document_epoch(document_epoch),
            validate_generation(transition_generation),
            validate_request_id(play_request_id),
            _validate_document_id(expected_place_id, "expected_place_id"),
            _validate_document_id(expected_game_id, "expected_game_id"),
        )

    @staticmethod
    def _record_context(
        transition: PlayTransition,
    ) -> Tuple[str, str, str, int, str, int, int]:
        return (
            transition.studio_id,
            transition.client_instance_id,
            transition.document_epoch,
            transition.transition_generation,
            transition.play_request_id,
            transition.expected_place_id,
            transition.expected_game_id,
        )

    def _snapshot(
        self,
        transition: PlayTransition,
        *,
        idempotent: bool = False,
    ) -> Dict[str, Any]:
        now = self._clock()
        payload: Dict[str, Any] = {
            "v": 2,
            "studio_id": transition.studio_id,
            "client_instance_id": transition.client_instance_id,
            "document_epoch": transition.document_epoch,
            "transition_generation": transition.transition_generation,
            "play_request_id": transition.play_request_id,
            "expected_place_id": transition.expected_place_id,
            "expected_game_id": transition.expected_game_id,
            "transition_nonce": transition.transition_nonce,
            "state": transition.state.value,
            "idempotent": idempotent,
            "expires_in_ms": max(
                0, int((transition.expires_at - now) * 1000)
            ),
            "attached": transition.attach_id is not None,
            "bootstrap_burned": transition.bootstrap_burned,
            "watchdog_armed": transition.watchdog_ack_id is not None,
            "recovery_only": transition.recovery_only,
            "stop_requested": transition.stop_command_id is not None,
            "stop_acked": transition.stop_ack_id is not None,
        }
        if transition.stop_command_id is not None:
            payload["stop_command_id"] = transition.stop_command_id
            payload["stop_source"] = transition.stop_source
        if transition.watchdog_deadline is not None:
            payload["watchdog_in_ms"] = max(
                0, int((transition.watchdog_deadline - now) * 1000)
            )
        if transition.watchdog_expired_reason is not None:
            payload["watchdog_expired_reason"] = (
                transition.watchdog_expired_reason
            )
        if transition.recovery_reason is not None:
            payload["recovery_reason"] = transition.recovery_reason
        if transition.completion_outcome is not None:
            payload["completion_outcome"] = transition.completion_outcome
        if transition.completion_id is not None:
            payload["completion_id"] = transition.completion_id
        if transition.completion_correlation is not None:
            payload["end_test_correlation"] = (
                transition.completion_correlation
            )
        return payload

    def _set_stop_requested(
        self,
        transition: PlayTransition,
        now: float,
        *,
        source: str,
        controller_stop_id: Optional[str],
    ) -> None:
        if transition.stop_command_id is None:
            transition.stop_command_id = str(uuid.uuid4())
            transition.stop_source = source
            transition.stop_requested_at = now
        if controller_stop_id is not None:
            transition.controller_stop_id = controller_stop_id
        # Recovery must never move an already acknowledged stop backwards.
        # Doing so would retain stop_ack_id while making the completion state
        # unreachable.
        if transition.state not in {
            PlayBridgeState.COMPLETED,
            PlayBridgeState.STOP_ACKED,
        }:
            transition.state = PlayBridgeState.STOP_REQUESTED
        next_deadline = now + self.stop_watchdog_seconds
        if (
            transition.watchdog_deadline is None
            or next_deadline < transition.watchdog_deadline
        ):
            # Recovery and retries may shorten this bound, never extend it.
            transition.watchdog_deadline = next_deadline

    def _advance_watchdog(
        self, transition: PlayTransition, now: Optional[float] = None
    ) -> None:
        current = self._clock() if now is None else now
        if transition.state == PlayBridgeState.COMPLETED:
            return
        if (
            transition.state
            in {PlayBridgeState.PREPARED, PlayBridgeState.ATTACHED}
            and current >= transition.expires_at
        ):
            source = (
                "attach_ttl_watchdog"
                if transition.state == PlayBridgeState.PREPARED
                else "play_ttl_watchdog"
            )
            self._set_stop_requested(
                transition,
                current,
                source=source,
                controller_stop_id=None,
            )
            return
        if (
            transition.state
            in {PlayBridgeState.STOP_REQUESTED, PlayBridgeState.STOP_ACKED}
            and transition.watchdog_deadline is not None
            and current >= transition.watchdog_deadline
            and transition.watchdog_expired_reason is None
        ):
            transition.watchdog_expired_reason = (
                "stop_ack_watchdog_expired"
                if transition.state == PlayBridgeState.STOP_REQUESTED
                else "edit_completion_watchdog_expired"
            )

    def _require_current(
        self,
        context: Tuple[str, str, str, int, str, int, int],
        transition_nonce: Any,
    ) -> PlayTransition:
        nonce = _validate_nonce(transition_nonce, "transition_nonce")
        transition = self._transitions.get(context[0])
        if (
            transition is None
            or self._record_context(transition) != context
            or transition.transition_nonce != nonce
        ):
            raise SessionConflictError(
                "Stale or mismatched Play bridge transition context"
            )
        self._advance_watchdog(transition)
        return transition

    @staticmethod
    def _check_token(
        supplied: Any, expected_hash: Optional[bytes], label: str
    ) -> str:
        token = _validate_token(supplied, label)
        if expected_hash is None or not hmac.compare_digest(
            _token_hash(token), expected_hash
        ):
            raise AuthenticationError(label + " is stale or invalid")
        return token

    @staticmethod
    def _check_server_instance(
        transition: PlayTransition, server_instance_id: Any
    ) -> str:
        target = validate_request_id(server_instance_id)
        if transition.server_instance_id != target:
            raise AuthenticationError(
                "Play bridge server identity is stale or invalid"
            )
        return target

    def _authenticate_server(
        self,
        transition: PlayTransition,
        server_instance_id: Any,
        server_token: Any,
    ) -> None:
        self._check_server_instance(transition, server_instance_id)
        self._check_token(
            server_token,
            transition.server_token_hash,
            "Play bridge server token",
        )
        if not transition.bootstrap_burned:
            transition.bootstrap_burned = True
            transition.attach_token_hash = b""

    def prepare(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        *,
        ttl_seconds: Any = None,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        ttl = _validate_ttl(
            ttl_seconds, self.default_ttl_seconds, self.max_ttl_seconds
        )
        with self._lock_for(context[0]):
            current = self._transitions.get(context[0])
            if current is not None:
                self._advance_watchdog(current)
                if (
                    self._record_context(current) == context
                    and current.state == PlayBridgeState.PREPARED
                    and not current.bootstrap_burned
                ):
                    result = self._snapshot(current, idempotent=True)
                    result["bridge_token"] = self._derived_token(
                        current, "attach"
                    )
                    return result
                if current.state != PlayBridgeState.COMPLETED:
                    raise SessionConflictError(
                        "This Studio has an unsafe active Play transition"
                    )

            now = self._clock()
            transition = PlayTransition(
                studio_id=context[0],
                client_instance_id=context[1],
                document_epoch=context[2],
                transition_generation=context[3],
                play_request_id=context[4],
                expected_place_id=context[5],
                expected_game_id=context[6],
                transition_nonce=str(uuid.uuid4()),
                attach_token_hash=b"",
                state=PlayBridgeState.PREPARED,
                created_at=now,
                expires_at=now + ttl,
            )
            bridge_token = self._derived_token(transition, "attach")
            transition.attach_token_hash = _token_hash(bridge_token)
            self._transitions[context[0]] = transition
            result = self._snapshot(transition)
            result["bridge_token"] = bridge_token
            return result

    def attach(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
        attach_id: Any,
        server_instance_id: Any,
        bridge_token: Any,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        attach_operation = validate_request_id(attach_id)
        server_instance = validate_request_id(server_instance_id)
        with self._lock_for(context[0]):
            transition = self._require_current(context, transition_nonce)
            self._check_token(
                bridge_token,
                transition.attach_token_hash,
                "Play bridge attach token",
            )
            if transition.state == PlayBridgeState.COMPLETED:
                raise SessionConflictError(
                    "The Play bridge transition is already complete"
                )
            if transition.attach_id is not None:
                if (
                    transition.attach_id != attach_operation
                    or transition.server_instance_id != server_instance
                ):
                    raise SessionConflictError(
                        "The one-time attach token was already bound"
                    )
                if transition.bootstrap_burned:
                    raise AuthenticationError(
                        "The one-time attach token has been consumed"
                    )
                result = self._snapshot(transition, idempotent=True)
                result["server_token"] = self._derived_token(
                    transition, "server"
                )
                return result
            if transition.state not in {
                PlayBridgeState.PREPARED,
                PlayBridgeState.STOP_REQUESTED,
            }:
                raise SessionConflictError(
                    "Play bridge cannot attach from its current state"
                )
            transition.attach_id = attach_operation
            transition.server_instance_id = server_instance
            transition.attached_at = self._clock()
            server_token = self._derived_token(transition, "server")
            transition.server_token_hash = _token_hash(server_token)
            if transition.state == PlayBridgeState.PREPARED:
                transition.state = PlayBridgeState.ATTACHED
            result = self._snapshot(transition)
            result["server_token"] = server_token
            result["required_ack"] = (
                "watchdog_armed"
                if transition.state == PlayBridgeState.ATTACHED
                else "server_poll_stop"
            )
            return result

    def status(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        with self._lock_for(context[0]):
            return self._snapshot(
                self._require_current(context, transition_nonce)
            )

    def abort_pre_attach(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
        abort_id: Any,
        runner_started: Any,
        script_cleaned: Any,
    ) -> Dict[str, Any]:
        """Retire only a prepared transition that provably never launched.

        This controller-only action exists for failures between prepare() and
        ExecutePlayModeAsync. It never stops a running test and cannot retire a
        transition after the one-time server attach path has begun.
        """

        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        operation_id = validate_request_id(abort_id)
        if runner_started is not False:
            raise ValidationError("runner_started must be false")
        if script_cleaned is not True:
            raise ValidationError("script_cleaned must be true")
        proof = ("pre_attach_abort", operation_id, False, True)
        with self._lock_for(context[0]):
            transition = self._require_current(context, transition_nonce)
            if transition.state == PlayBridgeState.COMPLETED:
                if (
                    transition.completion_outcome == "pre_attach_aborted"
                    and transition.completion_proof == proof
                ):
                    return self._snapshot(transition, idempotent=True)
                raise SessionConflictError(
                    "A conflicting terminal Play bridge proof already exists"
                )
            if (
                transition.state != PlayBridgeState.PREPARED
                or transition.attach_id is not None
                or transition.server_instance_id is not None
                or transition.server_token_hash is not None
                or transition.bootstrap_burned
                or transition.stop_command_id is not None
            ):
                raise SessionConflictError(
                    "Pre-attach abort is allowed only for an untouched "
                    "PREPARED transition"
                )
            transition.state = PlayBridgeState.COMPLETED
            transition.completion_id = operation_id
            transition.completion_outcome = "pre_attach_aborted"
            transition.completion_proof = proof
            transition.completed_at = self._clock()
            transition.watchdog_deadline = None
            transition.attach_token_hash = b""
            return self._snapshot(transition)

    def request_stop(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
        stop_id: Any,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        operation_id = validate_request_id(stop_id)
        with self._lock_for(context[0]):
            transition = self._require_current(context, transition_nonce)
            if transition.state == PlayBridgeState.COMPLETED:
                raise SessionConflictError(
                    "The Play bridge transition is already complete"
                )
            if transition.stop_command_id is not None:
                if transition.controller_stop_id == operation_id:
                    return self._snapshot(transition, idempotent=True)
                if (
                    transition.controller_stop_id is None
                    and transition.stop_source
                    in {
                        "attach_ttl_watchdog",
                        "play_ttl_watchdog",
                        "controller_disconnect",
                        "generation_replaced",
                    }
                ):
                    transition.controller_stop_id = operation_id
                    return self._snapshot(transition, idempotent=True)
                raise SessionConflictError(
                    "A different stop request owns this transition"
                )
            self._set_stop_requested(
                transition,
                self._clock(),
                source="controller",
                controller_stop_id=operation_id,
            )
            return self._snapshot(transition)

    def server_poll(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
        server_instance_id: Any,
        server_token: Any,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        with self._lock_for(context[0]):
            transition = self._require_current(context, transition_nonce)
            self._authenticate_server(
                transition, server_instance_id, server_token
            )
            if transition.state == PlayBridgeState.COMPLETED:
                raise SessionConflictError(
                    "The Play bridge transition is complete"
                )
            result = self._snapshot(transition)
            if transition.state == PlayBridgeState.STOP_REQUESTED:
                result["command"] = "stop"
            elif transition.state == PlayBridgeState.STOP_ACKED:
                result["command"] = "stop_acked"
            elif transition.watchdog_ack_id is None:
                result["command"] = "arm_watchdog"
            else:
                result["command"] = "wait"
            return result

    def server_ack(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
        server_instance_id: Any,
        ack_kind: Any,
        ack_id: Any,
        stop_command_id: Any,
        server_token: Any,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        if ack_kind not in {"watchdog_armed", "stop_received"}:
            raise ValidationError(
                "ack_kind must be watchdog_armed or stop_received"
            )
        operation_id = validate_request_id(ack_id)
        with self._lock_for(context[0]):
            transition = self._require_current(context, transition_nonce)
            self._authenticate_server(
                transition, server_instance_id, server_token
            )
            if ack_kind == "watchdog_armed":
                if stop_command_id is not None:
                    raise ValidationError(
                        "watchdog_armed must not include stop_command_id"
                    )
                if transition.watchdog_ack_id is not None:
                    if transition.watchdog_ack_id == operation_id:
                        return self._snapshot(
                            transition, idempotent=True
                        )
                    raise SessionConflictError(
                        "A different watchdog acknowledgement is bound"
                    )
                if transition.state not in {
                    PlayBridgeState.ATTACHED,
                    PlayBridgeState.STOP_REQUESTED,
                }:
                    raise SessionConflictError(
                        "Watchdog cannot be armed in the current state"
                    )
                transition.watchdog_ack_id = operation_id
                now = self._clock()
                transition.watchdog_armed_at = now
                # PREPARED expiry protects the one-time bootstrap token. Once
                # the exact server has attached and armed its own watchdog,
                # give the active transition a fresh bounded lifetime instead
                # of charging slow Studio startup against playable time.
                transition.expires_at = now + self.active_ttl_seconds
                return self._snapshot(transition)

            command_id = _validate_nonce(
                stop_command_id, "stop_command_id"
            )
            if transition.stop_command_id != command_id:
                raise SessionConflictError(
                    "Stale or mismatched Play bridge stop command"
                )
            if transition.stop_ack_id is not None:
                if transition.stop_ack_id == operation_id:
                    return self._snapshot(transition, idempotent=True)
                raise SessionConflictError(
                    "A different stop acknowledgement is bound"
                )
            if transition.state != PlayBridgeState.STOP_REQUESTED:
                raise SessionConflictError(
                    "Stop receipt requires a pending stop command"
                )
            now = self._clock()
            transition.state = PlayBridgeState.STOP_ACKED
            transition.stop_ack_id = operation_id
            transition.stop_acked_at = now
            transition.watchdog_deadline = (
                now + self.stop_watchdog_seconds
            )
            return self._snapshot(transition)

    def complete(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        play_request_id: Any,
        expected_place_id: Any,
        expected_game_id: Any,
        transition_nonce: Any,
        completion_id: Any,
        outcome: Any,
        end_test_correlation: Any,
        runner_returned: Any,
        edit_confirmations: Any,
        script_cleaned: Any,
    ) -> Dict[str, Any]:
        context = self._context(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
        )
        operation_id = validate_request_id(completion_id)
        if not isinstance(outcome, str) or outcome not in COMPLETION_OUTCOMES:
            raise ValidationError("Invalid Play bridge completion outcome")
        correlation = validate_request_id(end_test_correlation)
        if runner_returned is not True:
            raise ValidationError("runner_returned must be true")
        if (
            not isinstance(edit_confirmations, int)
            or isinstance(edit_confirmations, bool)
            or not 2 <= edit_confirmations <= 10
        ):
            raise ValidationError(
                "edit_confirmations must be an integer from 2 to 10"
            )
        if script_cleaned is not True:
            raise ValidationError("script_cleaned must be true")
        proof = (
            operation_id,
            outcome,
            correlation,
            True,
            edit_confirmations,
            True,
        )
        with self._lock_for(context[0]):
            transition = self._require_current(context, transition_nonce)
            if transition.state == PlayBridgeState.COMPLETED:
                if transition.completion_proof == proof:
                    return self._snapshot(transition, idempotent=True)
                raise SessionConflictError(
                    "Conflicting completion proof was replayed"
                )
            if outcome == "stopped_edit_confirmed":
                if transition.watchdog_ack_id is None:
                    raise SessionConflictError(
                        "Stop completion requires the server's watchdog_armed "
                        "acknowledgement"
                    )
                if (
                    transition.stop_command_id is None
                    or correlation != transition.stop_command_id
                ):
                    raise SessionConflictError(
                        "Stop completion is not correlated to this command"
                    )
                if (
                    transition.state != PlayBridgeState.STOP_ACKED
                    or transition.stop_ack_id is None
                ):
                    raise SessionConflictError(
                        "Stop completion requires the server's correlated "
                        "stop_received acknowledgement"
                    )
            elif outcome == "natural_stop_edit_confirmed":
                if (
                    transition.state != PlayBridgeState.ATTACHED
                    or transition.watchdog_ack_id is None
                    or transition.stop_command_id is not None
                ):
                    raise SessionConflictError(
                        "Natural completion requires an attached, "
                        "watchdog-acknowledged transition with no pending "
                        "stop command"
                    )
                if correlation != transition.transition_nonce:
                    raise SessionConflictError(
                        "Natural completion is not transition-correlated"
                    )
            elif outcome == "recovery_natural_stop_edit_confirmed":
                if (
                    transition.state != PlayBridgeState.STOP_REQUESTED
                    or not transition.recovery_only
                    or transition.watchdog_ack_id is None
                    or transition.stop_command_id is None
                    or transition.stop_source
                    not in {"controller_disconnect", "generation_replaced"}
                    or transition.stop_ack_id is not None
                ):
                    raise SessionConflictError(
                        "Recovery-natural completion requires an undelivered, "
                        "unacknowledged recovery Stop on a watchdog-armed "
                        "transition"
                    )
                if correlation != transition.transition_nonce:
                    raise SessionConflictError(
                        "Recovery-natural completion is not "
                        "transition-correlated"
                    )
            else:
                if (
                    transition.state != PlayBridgeState.PREPARED
                    or transition.attach_id is not None
                    or transition.stop_command_id is not None
                ):
                    raise SessionConflictError(
                        "Start failure completion is allowed only before "
                        "server attachment or a stop transition"
                    )
                if correlation != transition.play_request_id:
                    raise SessionConflictError(
                        "Start failure is not request-correlated"
                    )
            transition.state = PlayBridgeState.COMPLETED
            transition.completion_id = operation_id
            transition.completion_outcome = outcome
            transition.completion_correlation = correlation
            transition.completion_proof = proof
            transition.completed_at = self._clock()
            transition.watchdog_deadline = None
            transition.recovery_only = False
            transition.attach_token_hash = b""
            transition.server_token_hash = None
            return self._snapshot(transition)

    def enter_recovery(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
        transition_generation: Any,
        *,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        target_id = validate_studio_id(studio_id)
        instance_id = validate_client_instance_id(client_instance_id)
        epoch = validate_document_epoch(document_epoch)
        target_generation = validate_generation(transition_generation)
        with self._lock_for(target_id):
            transition = self._transitions.get(target_id)
            if transition is None or (
                transition.client_instance_id != instance_id
                or transition.document_epoch != epoch
                or transition.transition_generation != target_generation
            ):
                return None
            if transition.state != PlayBridgeState.COMPLETED:
                transition.recovery_only = True
                transition.recovery_reason = str(reason)[:160]
                self._set_stop_requested(
                    transition,
                    self._clock(),
                    source=(
                        "generation_replaced"
                        if "generation" in reason.lower()
                        else "controller_disconnect"
                    ),
                    controller_stop_id=None,
                )
            return self._snapshot(transition, idempotent=True)

    def assert_document_can_retire(
        self, studio_id: Any, client_instance_id: Any
    ) -> None:
        target_id = validate_studio_id(studio_id)
        instance_id = validate_client_instance_id(client_instance_id)
        with self._lock_for(target_id):
            transition = self._transitions.get(target_id)
            if (
                transition is not None
                and transition.client_instance_id == instance_id
                and transition.state != PlayBridgeState.COMPLETED
            ):
                raise SessionConflictError(
                    "The prior document has an unsafe Play transition"
                )

    def retire_completed(
        self,
        studio_id: Any,
        client_instance_id: Any,
        document_epoch: Any,
    ) -> bool:
        """Forget only an exact, positively completed transition record."""

        target_id = validate_studio_id(studio_id)
        instance_id = validate_client_instance_id(client_instance_id)
        epoch = validate_document_epoch(document_epoch)
        with self._lock_for(target_id):
            transition = self._transitions.get(target_id)
            if transition is None:
                return False
            if (
                transition.client_instance_id != instance_id
                or transition.document_epoch != epoch
                or transition.state != PlayBridgeState.COMPLETED
            ):
                raise SessionConflictError(
                    "Refusing to retire an uncertain or active Play transition"
                )
            self._transitions.pop(target_id, None)
            return True

    def public_summary(self, studio_id: Any) -> Optional[Dict[str, Any]]:
        """Return bounded non-secret state for observation and recovery."""

        target_id = validate_studio_id(studio_id)
        with self._lock_for(target_id):
            transition = self._transitions.get(target_id)
            if transition is None:
                return None
            self._advance_watchdog(transition)
            return {
                "state": transition.state.value,
                "transition_nonce": transition.transition_nonce,
                "play_request_id": transition.play_request_id,
                "attached": transition.attach_id is not None,
                "watchdog_armed": transition.watchdog_ack_id is not None,
                "recovery_only": transition.recovery_only,
                "stop_requested": transition.stop_command_id is not None,
                "stop_acked": transition.stop_ack_id is not None,
                "completion_outcome": transition.completion_outcome,
                "watchdog_expired": (
                    transition.watchdog_expired_reason is not None
                ),
            }

    def lifecycle_summaries(self) -> Dict[str, Dict[str, Any]]:
        """Return non-secret transition state for shutdown safety checks."""

        with self._lock_map_guard:
            studio_ids = tuple(self._transitions)
        result: Dict[str, Dict[str, Any]] = {}
        for studio_id in studio_ids:
            with self._lock_for(studio_id):
                transition = self._transitions.get(studio_id)
                if transition is None:
                    continue
                self._advance_watchdog(transition)
                result[studio_id] = {
                    "studio_id": studio_id,
                    "state": transition.state.value,
                    "completed": transition.state == PlayBridgeState.COMPLETED,
                    "recovery_only": transition.recovery_only,
                    "watchdog_expired": (
                        transition.watchdog_expired_reason is not None
                    ),
                }
        return result

    def sweep_watchdogs(self) -> Dict[str, Dict[str, Any]]:
        with self._lock_map_guard:
            studio_ids = tuple(self._lock_map)
        changed: Dict[str, Dict[str, Any]] = {}
        for studio_id in studio_ids:
            with self._lock_for(studio_id):
                transition = self._transitions.get(studio_id)
                if transition is None:
                    continue
                before = (
                    transition.state,
                    transition.watchdog_expired_reason,
                    transition.stop_command_id,
                )
                self._advance_watchdog(transition)
                after = (
                    transition.state,
                    transition.watchdog_expired_reason,
                    transition.stop_command_id,
                )
                if after != before:
                    changed[studio_id] = self._snapshot(transition)
        return changed
