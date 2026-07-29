from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from .errors import (
    AuthenticationError,
    SessionConflictError,
    SessionDisconnectedError,
    SessionNotFoundError,
    ValidationError,
)
from .play_bridge import PlayBridgeManager
from .session import LongPollTransport, StudioSession
from .validation import (
    validate_document_epoch,
    validate_generation,
    validate_client_instance_id,
    validate_registration_secret,
    validate_reconnect_id,
    validate_request_id,
    validate_request_id_list,
    validate_studio_id,
)


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


MAX_LIFECYCLE_STATUS_DETAILS = 256
MAX_RETIRED_SESSION_AUDIT = 256


@dataclass(frozen=True)
class Registration:
    studio_id: str
    document_epoch: str
    generation: int
    resume_token: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "v": 2,
            "studio_id": self.studio_id,
            "document_epoch": self.document_epoch,
            "generation": self.generation,
            "resume_token": self.resume_token,
        }


class SessionRegistry:
    """Shared broker-owned map. It contains no active/default Studio pointer."""

    def __init__(
        self,
        lease_timeout_seconds: float = 30.0,
        *,
        play_bridges: Optional[PlayBridgeManager] = None,
        terminal_retirement_grace_seconds: float = 60.0,
    ) -> None:
        if terminal_retirement_grace_seconds < 0:
            raise ValueError("terminal retirement grace must be nonnegative")
        self._sessions: Dict[str, StudioSession] = {}
        self._client_instances: Dict[str, str] = {}
        self.lease_timeout_seconds = lease_timeout_seconds
        self.terminal_retirement_grace_seconds = (
            terminal_retirement_grace_seconds
        )
        self.play_bridges = play_bridges or PlayBridgeManager()
        self._retired_session_count = 0
        self._retired_session_audit: Deque[Dict[str, Any]] = deque(
            maxlen=MAX_RETIRED_SESSION_AUDIT
        )

    async def register(
        self,
        *,
        client_instance_id: str,
        registration_secret: str,
        document_epoch: str,
        metadata: Dict[str, Any],
        capabilities: Iterable[str],
        studio_id: Optional[str] = None,
        resume_token: Optional[str] = None,
        reconnect_id: Optional[str] = None,
        settled_request_ids: Iterable[str] = (),
        transport: Optional[LongPollTransport] = None,
    ) -> Tuple[StudioSession, Registration]:
        instance_id = validate_client_instance_id(client_instance_id)
        registration_credential = validate_registration_secret(
            registration_secret
        )
        epoch = validate_document_epoch(document_epoch)
        settled_ids = validate_request_id_list(settled_request_ids)
        if not isinstance(metadata, dict):
            raise SessionConflictError("Studio metadata must be an object")
        if not isinstance(capabilities, (list, tuple, set, frozenset)):
            raise ValidationError("capabilities must be an array or set")
        if len(capabilities) > 256:
            raise ValidationError("capabilities exceeds the 256-tool limit")
        capability_set = set()
        for item in capabilities:
            if not isinstance(item, str) or not item or len(item) > 128:
                raise ValidationError("capability names must be non-empty strings")
            capability_set.add(item)
        next_transport = transport or LongPollTransport()
        rotated_token = secrets.token_urlsafe(32)
        rotated_hash = _token_hash(rotated_token)
        registration_hash = _token_hash(registration_credential)

        if studio_id is None:
            existing_id = self._client_instances.get(instance_id)
            if existing_id is not None:
                existing = self._sessions[existing_id]
                if not hmac.compare_digest(
                    existing.registration_secret_hash, registration_hash
                ):
                    raise AuthenticationError(
                        "Invalid adapter registration credential"
                    )
                if existing.document_epoch == epoch:
                    if (
                        existing.connected
                        and not existing.has_polled
                        and existing.bootstrap_resume_token is not None
                    ):
                        # Idempotent retry after a lost first connect response.
                        return existing, Registration(
                            existing.studio_id,
                            existing.document_epoch,
                            existing.generation,
                            existing.bootstrap_resume_token,
                        )
                    raise SessionConflictError(
                        "This adapter/document is already registered; "
                        "use its studio_id and resume credential"
                    )
                if existing.connected:
                    raise SessionConflictError(
                        "Disconnect the prior document session before "
                        "registering a new document epoch"
                    )
                unresolved = set(existing.uncertain_requests) - set(settled_ids)
                if unresolved:
                    raise SessionConflictError(
                        "The prior document has unsettled dispatched requests: "
                        + ",".join(sorted(unresolved))
                    )
                self.play_bridges.assert_document_can_retire(
                    existing.studio_id, existing.client_instance_id
                )
                for request_id in settled_ids:
                    existing.uncertain_requests.pop(request_id, None)
                existing._refresh_uncertainty(
                    fallback="Retired by a newer document epoch"
                )
            assigned_id = str(uuid.uuid4())
            session = StudioSession(
                assigned_id,
                instance_id,
                epoch,
                registration_hash,
                rotated_hash,
                rotated_token,
                next_transport,
                copy.deepcopy(metadata),
                capability_set,
            )
            self._sessions[assigned_id] = session
            self._client_instances[instance_id] = assigned_id
            return session, Registration(
                assigned_id, epoch, session.generation, rotated_token
            )

        target_id = validate_studio_id(studio_id)
        target_reconnect_id = (
            validate_reconnect_id(reconnect_id)
            if reconnect_id is not None
            else None
        )
        session = self._sessions.get(target_id)
        if session is None:
            # Studio IDs are server assigned. Unknown IDs cannot be claimed.
            raise SessionNotFoundError("Unknown Studio ID; register a new session")
        if session.client_instance_id != instance_id or not hmac.compare_digest(
            session.registration_secret_hash, registration_hash
        ):
            raise AuthenticationError("Studio adapter identity does not match")
        if self._client_instances.get(instance_id) != target_id:
            raise SessionConflictError(
                "This Studio session was retired by a newer document epoch"
            )
        if (
            isinstance(resume_token, str)
            and session.connected
            and not session.has_polled
            and session.bootstrap_resume_token is not None
            and session.connect_retry_resume_token_hash is not None
            and target_reconnect_id is not None
            and session.connect_retry_reconnect_id == target_reconnect_id
            and hmac.compare_digest(
                session.connect_retry_resume_token_hash,
                _token_hash(resume_token),
            )
        ):
            # Idempotent retry after the replacement generation was committed
            # but its connect response was lost. The prior token is accepted
            # only for this exact registration response—not for poll, response,
            # event, disconnect, or Play bridge routes—and is burned by the
            # replacement generation's first authenticated poll.
            if (
                session.metadata != metadata
                or session.capabilities != capability_set
                or session.connect_retry_settled_request_ids
                != frozenset(settled_ids)
            ):
                raise AuthenticationError(
                    "Invalid Studio reconnect receipt retry"
                )
            return session, Registration(
                target_id,
                epoch,
                session.generation,
                session.bootstrap_resume_token,
            )
        if not isinstance(resume_token, str) or not hmac.compare_digest(
            session.resume_token_hash, _token_hash(resume_token)
        ):
            raise AuthenticationError("Invalid Studio resume credential")
        if epoch != session.document_epoch:
            raise SessionConflictError(
                "document_epoch changed; register a new Studio session"
            )
        if target_reconnect_id is None:
            raise ValidationError(
                "reconnect_id is required when reconnecting a Studio session"
            )
        if target_reconnect_id in session.used_reconnect_ids:
            raise SessionConflictError("reconnect_id was already used")
        if session.connected and not session.lease_is_stale(
            self.lease_timeout_seconds
        ):
            raise SessionConflictError(
                "The prior Studio generation still holds a live lease"
            )
        prior_generation = session.generation
        prior_resume_token_hash = session.resume_token_hash
        generation = session.replace_connection(
            resume_token_hash=rotated_hash,
            bootstrap_resume_token=rotated_token,
            retry_resume_token_hash=prior_resume_token_hash,
            reconnect_id=target_reconnect_id,
            transport=next_transport,
            metadata=copy.deepcopy(metadata),
            capabilities=capability_set,
            settled_request_ids=settled_ids,
        )
        self.play_bridges.enter_recovery(
            session.studio_id,
            session.client_instance_id,
            session.document_epoch,
            prior_generation,
            reason="Studio connection generation was replaced",
        )
        self._client_instances[instance_id] = target_id
        return session, Registration(target_id, epoch, generation, rotated_token)

    def require(self, studio_id: Any, *, connected: bool = True) -> StudioSession:
        target_id = validate_studio_id(studio_id)
        session = self._sessions.get(target_id)
        if session is None:
            raise SessionNotFoundError("The explicitly targeted Studio does not exist")
        self._expire_stale_lease(session)
        if connected and not session.connected:
            raise SessionDisconnectedError(
                "The explicitly targeted Studio is disconnected"
            )
        return session

    def _expire_stale_lease(self, session: StudioSession) -> bool:
        if (
            not session.connected
            or not session.lease_is_stale(self.lease_timeout_seconds)
        ):
            return False
        generation = session.generation
        expired = session.disconnect(
            generation, "Studio connection lease expired"
        )
        if expired:
            self.play_bridges.enter_recovery(
                session.studio_id,
                session.client_instance_id,
                session.document_epoch,
                generation,
                reason="Studio connection lease expired",
            )
        return expired

    def _terminal_disconnected_is_safe(
        self, session: StudioSession
    ) -> bool:
        if (
            session.connected
            or not session.terminal_disconnect_candidate
            or session.last_confirmed_mode != "edit"
            or session.operation_lock.locked()
            or session.pending
            or session.uncertain_requests
            or session.uncertainty_state is not None
            or session.play_bridge_uncertain is not None
            or any(
                job.status not in session.TERMINAL_JOB_STATES
                for job in session.jobs.values()
            )
        ):
            return False
        transition = self.play_bridges.public_summary(session.studio_id)
        if transition is None:
            return True
        outcome = transition.get("completion_outcome")
        return (
            transition.get("state") == "completed"
            and isinstance(outcome, str)
            and (
                outcome.endswith("_edit_confirmed")
                or outcome == "pre_attach_aborted"
            )
        )

    def _retire_terminal_disconnected_sessions(self) -> int:
        """Compact only positively terminal, non-live Studio records.

        The bounded audit tombstone contains identity and the positive safety
        basis, never credentials, request arguments, results, console data, or
        Play nonces.
        """

        now = time.monotonic()
        retired = 0
        for studio_id, session in list(self._sessions.items()):
            disconnected_at = session.disconnected_at_monotonic
            if (
                disconnected_at is None
                or now - disconnected_at
                < self.terminal_retirement_grace_seconds
                or not self._terminal_disconnected_is_safe(session)
            ):
                continue
            transition = self.play_bridges.public_summary(studio_id)
            completion_outcome = (
                transition.get("completion_outcome")
                if transition is not None
                else None
            )
            if self._client_instances.get(session.client_instance_id) == studio_id:
                self._client_instances.pop(session.client_instance_id, None)
            self._sessions.pop(studio_id, None)
            self.play_bridges.retire_completed(
                studio_id,
                session.client_instance_id,
                session.document_epoch,
            )
            self._retired_session_count += 1
            retired += 1
            self._retired_session_audit.append(
                {
                    "studio_id": studio_id,
                    "client_instance_id": session.client_instance_id,
                    "document_epoch": session.document_epoch,
                    "generation": session.generation,
                    "last_confirmed_mode": "edit",
                    "completion_outcome": completion_outcome,
                    "basis": [
                        "disconnected",
                        "edit_confirmed",
                        "no_operations_or_uncertainty",
                        "play_terminal_or_absent",
                        "retirement_grace_elapsed",
                    ],
                }
            )
        return retired

    def authenticate_studio(
        self,
        studio_id: Any,
        generation: Any,
        resume_token: Any,
        *,
        polled: bool = False,
    ) -> StudioSession:
        session = self.require(studio_id, connected=True)
        target_generation = validate_generation(generation)
        if target_generation != session.generation:
            raise AuthenticationError("Stale Studio connection generation")
        if not isinstance(resume_token, str) or not hmac.compare_digest(
            session.resume_token_hash, _token_hash(resume_token)
        ):
            raise AuthenticationError("Invalid Studio resume credential")
        session.mark_seen(polled=polled)
        return session

    async def poll(
        self,
        studio_id: Any,
        generation: Any,
        resume_token: Any,
        timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        session = self.authenticate_studio(
            studio_id, generation, resume_token, polled=True
        )
        assert session.transport is not None
        return await session.transport.poll(timeout_seconds)

    def receive_response(
        self,
        studio_id: Any,
        generation: Any,
        resume_token: Any,
        request_id: Any,
        *,
        success: bool,
        result: Any = None,
        error: Any = None,
    ) -> bool:
        session = self.authenticate_studio(studio_id, generation, resume_token)
        return session.receive_response(
            session.generation,
            validate_request_id(request_id),
            success=bool(success),
            result=result,
            error=error,
        )

    def receive_event(
        self,
        studio_id: Any,
        generation: Any,
        resume_token: Any,
        event_type: Any,
        payload: Any,
    ) -> bool:
        session = self.authenticate_studio(studio_id, generation, resume_token)
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            return False
        return session.receive_event(session.generation, event_type, payload)

    def disconnect(
        self,
        studio_id: Any,
        generation: Any,
        resume_token: Any,
        reason: str = "Studio disconnected",
    ) -> bool:
        session = self.authenticate_studio(studio_id, generation, resume_token)
        disconnected = session.disconnect(session.generation, reason)
        if disconnected:
            self.play_bridges.enter_recovery(
                session.studio_id,
                session.client_instance_id,
                session.document_epoch,
                session.generation,
                reason=reason,
            )
        return disconnected

    @staticmethod
    def _play_identity(
        session: StudioSession,
    ) -> Tuple[str, int, int]:
        place_id = session.metadata.get("place_id")
        game_id = session.metadata.get("game_id")
        if (
            not isinstance(place_id, int)
            or isinstance(place_id, bool)
            or place_id < 0
            or not isinstance(game_id, int)
            or isinstance(game_id, bool)
            or game_id < 0
        ):
            raise SessionConflictError(
                "Play bridge requires nonnegative place_id and game_id metadata"
            )
        return session.client_instance_id, place_id, game_id

    @staticmethod
    def _require_pending_play_operation(
        session: StudioSession,
        request_id: Any,
        legacy_remote_tool: str,
        *,
        durable_is_start: bool,
    ) -> str:
        operation_id = validate_request_id(request_id)
        pending = session.pending.get(operation_id)
        legacy_match = (
            pending is not None
            and pending.remote_tool == legacy_remote_tool
            and pending.arguments == {}
        )
        durable_match = (
            pending is not None
            and pending.remote_tool == "studio_start_stop_play"
            and pending.arguments == {"is_start": durable_is_start}
        )
        if (
            pending is None
            or pending.generation != session.generation
            or not (legacy_match or durable_match)
        ):
            raise SessionConflictError(
                "Play bridge request is not bound to the authorized "
                + ("start" if durable_is_start else "stop")
                + " phase dispatch"
            )
        return operation_id

    def _authenticate_play_controller(
        self,
        studio_id: Any,
        document_epoch: Any,
        generation: Any,
        resume_token: Any,
    ) -> StudioSession:
        session = self.authenticate_studio(
            studio_id, generation, resume_token
        )
        epoch = validate_document_epoch(document_epoch)
        if epoch != session.document_epoch:
            raise AuthenticationError(
                "Play bridge document epoch does not match this Studio"
            )
        return session

    def prepare_play_bridge(
        self,
        studio_id: Any,
        document_epoch: Any,
        generation: Any,
        resume_token: Any,
        play_request_id: Any,
        ttl_seconds: Any = None,
    ) -> Dict[str, Any]:
        session = self._authenticate_play_controller(
            studio_id, document_epoch, generation, resume_token
        )
        operation_id = self._require_pending_play_operation(
            session,
            play_request_id,
            "rnd_play_start",
            durable_is_start=True,
        )
        instance_id, place_id, game_id = self._play_identity(session)
        result = self.play_bridges.prepare(
            session.studio_id,
            instance_id,
            session.document_epoch,
            session.generation,
            operation_id,
            place_id,
            game_id,
            ttl_seconds=ttl_seconds,
        )
        session.play_bridge_uncertain = result["transition_nonce"]
        return result

    def play_bridge_status(
        self,
        studio_id: Any,
        document_epoch: Any,
        generation: Any,
        resume_token: Any,
        transition_generation: Any,
        play_request_id: Any,
        transition_nonce: Any,
    ) -> Dict[str, Any]:
        session = self._authenticate_play_controller(
            studio_id, document_epoch, generation, resume_token
        )
        instance_id, place_id, game_id = self._play_identity(session)
        return self.play_bridges.status(
            session.studio_id,
            instance_id,
            session.document_epoch,
            transition_generation,
            play_request_id,
            place_id,
            game_id,
            transition_nonce,
        )

    def abort_play_bridge_pre_attach(
        self,
        studio_id: Any,
        document_epoch: Any,
        generation: Any,
        resume_token: Any,
        transition_generation: Any,
        play_request_id: Any,
        transition_nonce: Any,
        abort_id: Any,
        runner_started: Any,
        script_cleaned: Any,
    ) -> Dict[str, Any]:
        session = self._authenticate_play_controller(
            studio_id, document_epoch, generation, resume_token
        )
        # prepare_play_bridge already bound this exact transition context to an
        # authorized pending rnd_play_start. Cleanup must remain possible after
        # that caller times out and Session.pending releases its waiter.
        instance_id, place_id, game_id = self._play_identity(session)
        result = self.play_bridges.abort_pre_attach(
            session.studio_id,
            instance_id,
            session.document_epoch,
            transition_generation,
            play_request_id,
            place_id,
            game_id,
            transition_nonce,
            abort_id,
            runner_started,
            script_cleaned,
        )
        if session.play_bridge_uncertain == transition_nonce:
            session.play_bridge_uncertain = None
        return result

    def request_play_bridge_stop(
        self,
        studio_id: Any,
        document_epoch: Any,
        generation: Any,
        resume_token: Any,
        transition_generation: Any,
        play_request_id: Any,
        transition_nonce: Any,
        stop_id: Any,
    ) -> Dict[str, Any]:
        session = self._authenticate_play_controller(
            studio_id, document_epoch, generation, resume_token
        )
        operation_id = self._require_pending_play_operation(
            session,
            stop_id,
            "rnd_play_stop",
            durable_is_start=False,
        )
        instance_id, place_id, game_id = self._play_identity(session)
        return self.play_bridges.request_stop(
            session.studio_id,
            instance_id,
            session.document_epoch,
            transition_generation,
            play_request_id,
            place_id,
            game_id,
            transition_nonce,
            operation_id,
        )

    def complete_play_bridge(
        self,
        studio_id: Any,
        document_epoch: Any,
        generation: Any,
        resume_token: Any,
        transition_generation: Any,
        play_request_id: Any,
        transition_nonce: Any,
        completion_id: Any,
        outcome: Any,
        end_test_correlation: Any,
        runner_returned: Any,
        edit_confirmations: Any,
        script_cleaned: Any,
    ) -> Dict[str, Any]:
        session = self._authenticate_play_controller(
            studio_id, document_epoch, generation, resume_token
        )
        instance_id, place_id, game_id = self._play_identity(session)
        result = self.play_bridges.complete(
            session.studio_id,
            instance_id,
            session.document_epoch,
            transition_generation,
            play_request_id,
            place_id,
            game_id,
            transition_nonce,
            completion_id,
            outcome,
            end_test_correlation,
            runner_returned,
            edit_confirmations,
            script_cleaned,
        )
        if session.play_bridge_uncertain == transition_nonce:
            session.play_bridge_uncertain = None
        return result

    def attach_play_bridge(
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
        return self.play_bridges.attach(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
            transition_nonce,
            attach_id,
            server_instance_id,
            bridge_token,
        )

    def poll_play_bridge_server(
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
        return self.play_bridges.server_poll(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
            transition_nonce,
            server_instance_id,
            server_token,
        )

    def acknowledge_play_bridge_stop(
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
        return self.play_bridges.server_ack(
            studio_id,
            client_instance_id,
            document_epoch,
            transition_generation,
            play_request_id,
            expected_place_id,
            expected_game_id,
            transition_nonce,
            server_instance_id,
            ack_kind,
            ack_id,
            stop_command_id,
            server_token,
        )

    def snapshots(self) -> List[Dict[str, Any]]:
        for session in list(self._sessions.values()):
            self._expire_stale_lease(session)
        self._retire_terminal_disconnected_sessions()
        snapshots: List[Dict[str, Any]] = []
        for session in self._sessions.values():
            snapshot = session.snapshot()
            transition = self.play_bridges.public_summary(
                session.studio_id
            )
            if transition is None:
                snapshot["play"] = {
                    "state": "edit" if session.mode == "edit" else "unknown",
                    "active": False,
                }
            else:
                broker_state = self.broker_state_snapshot(
                    session.studio_id
                )
                snapshot["mode"] = broker_state["mode"]
                snapshot["play"] = broker_state["play"]
            snapshots.append(snapshot)
        return snapshots

    def broker_state_snapshot(self, studio_id: Any) -> Dict[str, Any]:
        """Return exact broker-owned Play state when Studio cannot answer."""

        session = self.require(studio_id, connected=False)
        transition = self.play_bridges.public_summary(session.studio_id)
        if transition is None:
            mode = session.mode if session.connected else "unknown"
            play: Dict[str, Any] = {
                "active": False,
                "state": "edit" if mode == "edit" else "unknown",
            }
        else:
            host_state = transition["state"]
            completion = transition.get("completion_outcome")
            if host_state == "completed":
                edit_confirmed = isinstance(completion, str) and (
                    completion.endswith("_edit_confirmed")
                    or completion == "pre_attach_aborted"
                )
                mode = "edit" if edit_confirmed else "unknown"
                state = completion or "completed"
                active = False
            elif transition["stop_requested"]:
                mode = "stopping"
                state = "stopping"
                active = bool(transition["watchdog_armed"])
            elif (
                transition["attached"]
                and transition["watchdog_armed"]
            ):
                mode = "play"
                state = "play"
                active = True
            else:
                mode = "starting"
                state = "starting"
                active = False
            play = copy.deepcopy(transition)
            play["active"] = active
            play["state"] = state
        metadata = session.metadata
        return {
            "adapter": "studio-mcp-v2-broker-recovery-view",
            "source": "broker",
            "connected": session.connected,
            "studio_id": session.studio_id,
            "client_instance_id": session.client_instance_id,
            "document_epoch": session.document_epoch,
            "generation": session.generation,
            "name": metadata.get("name"),
            "place_id": metadata.get("place_id"),
            "game_id": metadata.get("game_id"),
            "mode": mode,
            "is_edit": mode == "edit",
            "play": play,
        }

    def lifecycle_summary(self) -> Dict[str, Any]:
        """Summarize whether dropping this broker is positively safe.

        The summary intentionally excludes request arguments, results, console
        data, credentials, and Play nonces.
        """

        for session in list(self._sessions.values()):
            self._expire_stale_lease(session)
        self._retire_terminal_disconnected_sessions()
        transitions = self.play_bridges.lifecycle_summaries()
        session_summaries: List[Dict[str, Any]] = []
        stop_blockers: List[Dict[str, Any]] = []
        unsafe_transitions: List[Dict[str, Any]] = []
        stop_blocker_count = 0
        unsafe_transition_count = 0
        connected_count = 0
        for studio_id, transition in transitions.items():
            if not transition["completed"]:
                unsafe_transition_count += 1
                if len(unsafe_transitions) < MAX_LIFECYCLE_STATUS_DETAILS:
                    unsafe_transitions.append(copy.deepcopy(transition))
        for session in self._sessions.values():
            if session.connected:
                connected_count += 1
            reasons: List[str] = []
            retained_terminal = self._terminal_disconnected_is_safe(session)
            observed_mode = (
                "edit" if retained_terminal else session.mode.lower()
            )
            normalized_mode = (
                observed_mode
                if observed_mode
                in {"edit", "play", "run", "paused", "unknown"}
                else "unknown"
            )
            if not session.connected and not retained_terminal:
                reasons.append("not_connected")
            if normalized_mode != "edit":
                reasons.append("mode_not_edit")
            if session.operation_lock.locked():
                reasons.append("operation_in_flight")
            if session.pending:
                reasons.append("pending_requests")
            if session.uncertain_requests:
                reasons.append("uncertain_requests")
            if session.uncertainty_state is not None:
                reasons.append("session_uncertain")
            if session.play_bridge_uncertain is not None:
                reasons.append("play_bridge_uncertain")
            nonterminal_jobs: Dict[str, int] = {}
            dispatched_jobs = 0
            for job in session.jobs.values():
                if job.status not in session.TERMINAL_JOB_STATES:
                    status_name = (
                        job.status
                        if job.status in {"queued", "running"}
                        else "other_nonterminal"
                    )
                    nonterminal_jobs[status_name] = (
                        nonterminal_jobs.get(status_name, 0) + 1
                    )
                    if job.dispatched:
                        dispatched_jobs += 1
            if nonterminal_jobs:
                reasons.append("nonterminal_jobs")
            transition = transitions.get(session.studio_id)
            if transition is not None and not transition["completed"]:
                reasons.append("play_transition_active")
            summary = {
                "studio_id": session.studio_id,
                "connected": session.connected,
                "mode": normalized_mode,
                "pending_request_count": len(session.pending),
                "uncertain_request_count": len(session.uncertain_requests),
                "nonterminal_job_counts": nonterminal_jobs,
                "dispatched_nonterminal_job_count": dispatched_jobs,
                "retained_terminal_disconnected": retained_terminal,
                "blockers": reasons,
            }
            if len(session_summaries) < MAX_LIFECYCLE_STATUS_DETAILS:
                session_summaries.append(summary)
            if reasons:
                stop_blocker_count += 1
                if len(stop_blockers) < MAX_LIFECYCLE_STATUS_DETAILS:
                    stop_blockers.append(
                        {"studio_id": session.studio_id, "reasons": reasons}
                    )
        known_sessions = set(self._sessions)
        for transition in transitions.values():
            if (
                not transition["completed"]
                and transition["studio_id"] not in known_sessions
            ):
                stop_blocker_count += 1
                if len(stop_blockers) < MAX_LIFECYCLE_STATUS_DETAILS:
                    stop_blockers.append(
                        {
                            "studio_id": transition["studio_id"],
                            "reasons": ["orphaned_play_transition"],
                        }
                    )
        return {
            "session_count": len(self._sessions),
            "connected_session_count": connected_count,
            "sessions": session_summaries,
            "sessions_truncated": len(self._sessions) > len(session_summaries),
            "unsafe_transition_count": unsafe_transition_count,
            "unsafe_transitions": unsafe_transitions,
            "unsafe_transitions_truncated": (
                unsafe_transition_count > len(unsafe_transitions)
            ),
            "stop_safe": stop_blocker_count == 0,
            "stop_blocker_count": stop_blocker_count,
            "stop_blockers": stop_blockers,
            "stop_blockers_truncated": (
                stop_blocker_count > len(stop_blockers)
            ),
            "retired_session_count": self._retired_session_count,
            "retired_session_audit": list(self._retired_session_audit),
            "retired_session_audit_truncated": (
                self._retired_session_count
                > len(self._retired_session_audit)
            ),
        }

    def session_count(self) -> int:
        return len(self._sessions)
