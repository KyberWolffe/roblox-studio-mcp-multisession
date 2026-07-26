from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import hmac
import json
import os
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable, Dict, Optional

from . import __version__
from .auth import Principal
from .catalog import DISCOVERY_TOOL, JOB_TOOLS, ToolCatalog
from .errors import (
    AuthenticationError,
    ProxyError,
    SessionConflictError,
    ValidationError,
)
from .registry import SessionRegistry
from .service import ProxyService


MAX_HTTP_BODY_BYTES = 1_100_000
CLIENT_API_DEADLINE_SECONDS = 125.0


@dataclass(frozen=True)
class HubRuntimeInfo:
    """Non-secret identity for one broker process."""

    instance_id: str
    pid: int
    started_at: float
    catalog_sha256: str
    version: str = __version__

    @classmethod
    def create(cls, catalog: ToolCatalog) -> "HubRuntimeInfo":
        encoded = json.dumps(
            catalog.tools_for_mcp(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return cls(
            instance_id=str(uuid.uuid4()),
            pid=os.getpid(),
            started_at=time.time(),
            catalog_sha256=hashlib.sha256(encoded).hexdigest(),
        )

    def validate(self) -> None:
        try:
            parsed = uuid.UUID(self.instance_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Broker instance_id must be a UUID")
        if str(parsed) != self.instance_id:
            raise ValueError("Broker instance_id must be a canonical UUID")
        if not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("Broker pid must be a positive integer")
        if not isinstance(self.started_at, (int, float)) or self.started_at <= 0:
            raise ValueError("Broker started_at must be positive")
        if (
            not isinstance(self.catalog_sha256, str)
            or len(self.catalog_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.catalog_sha256)
        ):
            raise ValueError("Broker catalog_sha256 must be lowercase SHA-256")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "api_version": 2,
            "service": "studio-mcp-v2",
            "version": self.version,
            "broker_instance_id": self.instance_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "catalog_sha256": self.catalog_sha256,
        }


@dataclass(frozen=True)
class HubSecurityConfig:
    studio_token: str
    client_token: str
    client_principal: Principal
    poll_timeout_seconds: float = 20.0

    def validate(self) -> None:
        if len(self.studio_token) < 32 or len(self.client_token) < 32:
            raise ValueError("Both v2 bearer tokens must be at least 32 characters")
        if hmac.compare_digest(self.studio_token, self.client_token):
            raise ValueError("Studio and MCP client tokens must be different")


class V2HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Lifecycle restarts occur immediately after an authenticated, exact-instance
    # graceful stop. SO_REUSEADDR avoids a TIME_WAIT-only restart failure; it
    # does not permit a second concurrent listener on the same loopback tuple.
    allow_reuse_address = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind() performs socket.getfqdn(host), which is an
        # unnecessary reverse-DNS lookup for an already validated literal
        # loopback address. A slow or broken local resolver must never delay
        # authenticated broker readiness.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        if host not in {"127.0.0.1", "::1"}:
            raise ValueError("The v2 hub bound a non-loopback address")
        self.server_name = host
        self.server_port = port

    def __init__(
        self,
        server_address: tuple,
        *,
        loop: asyncio.AbstractEventLoop,
        registry: SessionRegistry,
        service: ProxyService,
        catalog: ToolCatalog,
        security: HubSecurityConfig,
        runtime_info: HubRuntimeInfo,
        shutdown_callback: Optional[Callable[[], None]],
    ) -> None:
        self.loop = loop
        self.registry = registry
        self.service = service
        self.catalog = catalog
        self.security = security
        self.runtime_info = runtime_info
        self.shutdown_callback = shutdown_callback
        self._lifecycle_guard = threading.Lock()
        self._active_client_operations = 0
        self._active_studio_mutations = 0
        self._lifecycle_stopping = False
        super().__init__(server_address, V2RequestHandler)

    def begin_client_operation(self) -> None:
        with self._lifecycle_guard:
            if self._lifecycle_stopping:
                raise SessionConflictError(
                    "Broker shutdown is fenced; no new operations are admitted"
                )
            self._active_client_operations += 1

    def assert_lifecycle_creation_open(self) -> None:
        with self._lifecycle_guard:
            if self._lifecycle_stopping:
                raise SessionConflictError(
                    "Broker shutdown is fenced; new sessions/transitions "
                    "are not admitted"
                )

    def begin_studio_mutation(self) -> None:
        with self._lifecycle_guard:
            if self._lifecycle_stopping:
                raise SessionConflictError(
                    "Broker shutdown is fenced; Studio state changes are "
                    "not admitted"
                )
            self._active_studio_mutations += 1

    def end_studio_mutation(self) -> None:
        with self._lifecycle_guard:
            if self._active_studio_mutations <= 0:
                raise RuntimeError("Lifecycle Studio-mutation counter underflow")
            self._active_studio_mutations -= 1

    def end_client_operation(self) -> None:
        with self._lifecycle_guard:
            if self._active_client_operations <= 0:
                raise RuntimeError("Lifecycle client-operation counter underflow")
            self._active_client_operations -= 1

    def begin_stop_fence(self) -> Dict[str, int]:
        with self._lifecycle_guard:
            if self._lifecycle_stopping:
                raise SessionConflictError("Broker shutdown is already in progress")
            active = {
                "active_client_operation_count": self._active_client_operations,
                "active_studio_mutation_count": self._active_studio_mutations,
            }
            if any(active.values()):
                return active
            self._lifecycle_stopping = True
            return active

    def cancel_stop_fence(self) -> None:
        with self._lifecycle_guard:
            self._lifecycle_stopping = False

    def lifecycle_counters(self) -> Dict[str, Any]:
        with self._lifecycle_guard:
            return {
                "lifecycle_stopping": self._lifecycle_stopping,
                "active_client_operation_count": self._active_client_operations,
                "active_studio_mutation_count": self._active_studio_mutations,
            }


async def _call_sync(function, *args, **kwargs):
    return function(*args, **kwargs)


def submit_to_loop(
    loop: asyncio.AbstractEventLoop,
    coroutine: Awaitable[Any],
    timeout: float,
) -> Any:
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=timeout)
    except (TimeoutError, concurrent.futures.TimeoutError):
        # Prevent queued work from running after the HTTP caller has already
        # received a timeout and may retry.
        future.cancel()
        raise


class V2RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StudioMCPv2"
    sys_version = ""

    @property
    def v2_server(self) -> V2HTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Deliberately omit headers and bodies so credentials/Luau never enter logs.
        message = "%s - %s\n" % (self.address_string(), fmt % args)
        import sys

        sys.stderr.write(message)

    def do_GET(self) -> None:
        if self.path == "/v2/health":
            self._write_json(
                HTTPStatus.OK,
                {
                    "v": 2,
                    "status": "ok",
                    "service": "studio-mcp-v2",
                },
            )
            return
        self._write_error(ValidationError("Unknown endpoint"), HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            self._reject_browser_origin()
            body = self._read_json_body()
            if self.path.startswith("/v2/play-bridge/"):
                mutating = self.path in {
                    "/v2/play-bridge/attach",
                    "/v2/play-bridge/server-ack",
                }
                if mutating:
                    self.v2_server.begin_studio_mutation()
                try:
                    self._handle_play_bridge(body, self._bearer_token())
                finally:
                    if mutating:
                        self.v2_server.end_studio_mutation()
                return
            if self.path.startswith("/v2/studios/"):
                self._authenticate(self.v2_server.security.studio_token)
                mutating = self.path in {
                    "/v2/studios/connect",
                    "/v2/studios/response",
                    "/v2/studios/event",
                    "/v2/studios/play-bridge/prepare",
                    "/v2/studios/play-bridge/abort-pre-attach",
                    "/v2/studios/play-bridge/request-stop",
                    "/v2/studios/play-bridge/complete",
                    "/v2/studios/disconnect",
                }
                if mutating:
                    self.v2_server.begin_studio_mutation()
                try:
                    self._handle_studio(body)
                finally:
                    if mutating:
                        self.v2_server.end_studio_mutation()
                return
            if self.path.startswith("/v2/client/"):
                self._authenticate(self.v2_server.security.client_token)
                operational = self.path in {
                    "/v2/client/call",
                    "/v2/client/jobs/start",
                    "/v2/client/jobs/get",
                    "/v2/client/jobs/cancel",
                }
                if operational:
                    self.v2_server.begin_client_operation()
                    try:
                        self._handle_client(body)
                    finally:
                        self.v2_server.end_client_operation()
                else:
                    self._handle_client(body)
                return
            raise ValidationError("Unknown endpoint")
        except ProxyError as exc:
            self._write_error(exc)
        except (TimeoutError, concurrent.futures.TimeoutError):
            self._write_error(
                ValidationError("Broker operation exceeded its local API deadline"),
                HTTPStatus.GATEWAY_TIMEOUT,
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_error(ValidationError("Request body must be valid UTF-8 JSON"))
        except Exception:
            # Do not leak local paths, tokens, or payloads through unexpected errors.
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": "Internal v2 broker error",
                    },
                },
            )

    def _reject_browser_origin(self) -> None:
        if self.headers.get("Origin") is not None:
            raise AuthenticationError("Browser Origin requests are not accepted")

    def _authenticate(self, expected_token: str) -> None:
        supplied = self._bearer_token()
        if not hmac.compare_digest(supplied, expected_token):
            raise AuthenticationError("Invalid local v2 bearer token")

    def _bearer_token(self) -> str:
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = value[len(prefix) :] if value.startswith(prefix) else ""
        if not supplied:
            raise AuthenticationError("Invalid local v2 bearer token")
        return supplied

    @staticmethod
    def _require_body_fields(
        body: Dict[str, Any],
        required: set[str],
        optional: set[str] = frozenset(),
    ) -> None:
        keys = set(body)
        missing = required - keys
        unexpected = keys - required - optional
        if missing:
            raise ValidationError(
                "Missing required fields: " + ",".join(sorted(missing))
            )
        if unexpected:
            raise ValidationError(
                "Unexpected fields: " + ",".join(sorted(unexpected))
            )

    def _read_json_body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValidationError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValidationError("Invalid Content-Length")
        if length < 0 or length > MAX_HTTP_BODY_BYTES:
            raise ValidationError("Request body exceeds the v2 size limit")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object")
        return payload

    def _submit(
        self,
        coroutine: Awaitable[Any],
        timeout: float = CLIENT_API_DEADLINE_SECONDS,
    ) -> Any:
        # If dispatch already occurred, invoke() records outcome uncertainty
        # when this cancellation reaches it.
        return submit_to_loop(self.v2_server.loop, coroutine, timeout)

    def _handle_studio(self, body: Dict[str, Any]) -> None:
        registry = self.v2_server.registry
        if self.path == "/v2/studios/connect":
            capabilities = body.get("capabilities", [])
            if not isinstance(capabilities, list):
                raise ValidationError("capabilities must be an array")
            session, registration = self._submit(
                registry.register(
                    client_instance_id=body.get("client_instance_id"),
                    registration_secret=body.get("registration_secret"),
                    document_epoch=body.get("document_epoch"),
                    metadata=body.get("metadata", {}),
                    capabilities=capabilities,
                    studio_id=body.get("studio_id"),
                    resume_token=body.get("resume_token"),
                    reconnect_id=body.get("reconnect_id"),
                    settled_request_ids=body.get("settled_request_ids", []),
                )
            )
            payload = registration.as_dict()
            payload["approved_capabilities"] = sorted(
                session.capabilities & self.v2_server.catalog.remote_names
            )
            payload["broker_instance_id"] = (
                self.v2_server.runtime_info.instance_id
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": payload})
            return
        if self.path == "/v2/studios/poll":
            result = self._submit(
                registry.poll(
                    body.get("studio_id"),
                    body.get("generation"),
                    body.get("resume_token"),
                    self.v2_server.security.poll_timeout_seconds,
                ),
                timeout=self.v2_server.security.poll_timeout_seconds + 5,
            )
            self._write_json(
                HTTPStatus.OK, {"ok": True, "result": result}
            )
            return
        if self.path == "/v2/studios/response":
            accepted = self._submit(
                _call_sync(
                    registry.receive_response,
                    body.get("studio_id"),
                    body.get("generation"),
                    body.get("resume_token"),
                    body.get("request_id"),
                    success=body.get("success") is True,
                    result=body.get("result"),
                    error=body.get("error"),
                )
            )
            self._write_json(
                HTTPStatus.OK, {"ok": True, "result": {"accepted": accepted}}
            )
            return
        if self.path == "/v2/studios/event":
            accepted = self._submit(
                _call_sync(
                    registry.receive_event,
                    body.get("studio_id"),
                    body.get("generation"),
                    body.get("resume_token"),
                    body.get("event_type"),
                    body.get("payload"),
                )
            )
            self._write_json(
                HTTPStatus.OK, {"ok": True, "result": {"accepted": accepted}}
            )
            return
        if self.path == "/v2/studios/play-bridge/prepare":
            self._require_body_fields(
                body,
                {
                    "studio_id",
                    "document_epoch",
                    "generation",
                    "resume_token",
                    "play_request_id",
                },
                {"ttl_seconds"},
            )
            result = self._submit(
                _call_sync(
                    registry.prepare_play_bridge,
                    body["studio_id"],
                    body["document_epoch"],
                    body["generation"],
                    body["resume_token"],
                    body["play_request_id"],
                    body.get("ttl_seconds"),
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/studios/play-bridge/abort-pre-attach":
            self._require_body_fields(
                body,
                {
                    "studio_id",
                    "document_epoch",
                    "generation",
                    "resume_token",
                    "transition_generation",
                    "play_request_id",
                    "transition_nonce",
                    "abort_id",
                    "runner_started",
                    "script_cleaned",
                },
            )
            result = self._submit(
                _call_sync(
                    registry.abort_play_bridge_pre_attach,
                    body["studio_id"],
                    body["document_epoch"],
                    body["generation"],
                    body["resume_token"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["transition_nonce"],
                    body["abort_id"],
                    body["runner_started"],
                    body["script_cleaned"],
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/studios/play-bridge/status":
            self._require_body_fields(
                body,
                {
                    "studio_id",
                    "document_epoch",
                    "generation",
                    "resume_token",
                    "transition_generation",
                    "play_request_id",
                    "transition_nonce",
                },
            )
            result = self._submit(
                _call_sync(
                    registry.play_bridge_status,
                    body["studio_id"],
                    body["document_epoch"],
                    body["generation"],
                    body["resume_token"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["transition_nonce"],
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/studios/play-bridge/request-stop":
            self._require_body_fields(
                body,
                {
                    "studio_id",
                    "document_epoch",
                    "generation",
                    "resume_token",
                    "transition_generation",
                    "play_request_id",
                    "transition_nonce",
                    "stop_id",
                },
            )
            result = self._submit(
                _call_sync(
                    registry.request_play_bridge_stop,
                    body["studio_id"],
                    body["document_epoch"],
                    body["generation"],
                    body["resume_token"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["transition_nonce"],
                    body["stop_id"],
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/studios/play-bridge/complete":
            self._require_body_fields(
                body,
                {
                    "studio_id",
                    "document_epoch",
                    "generation",
                    "resume_token",
                    "transition_generation",
                    "play_request_id",
                    "transition_nonce",
                    "completion_id",
                    "outcome",
                    "end_test_correlation",
                    "runner_returned",
                    "edit_confirmations",
                    "script_cleaned",
                },
            )
            result = self._submit(
                _call_sync(
                    registry.complete_play_bridge,
                    body["studio_id"],
                    body["document_epoch"],
                    body["generation"],
                    body["resume_token"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["transition_nonce"],
                    body["completion_id"],
                    body["outcome"],
                    body["end_test_correlation"],
                    body["runner_returned"],
                    body["edit_confirmations"],
                    body["script_cleaned"],
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/studios/disconnect":
            disconnected = self._submit(
                _call_sync(
                    registry.disconnect,
                    body.get("studio_id"),
                    body.get("generation"),
                    body.get("resume_token"),
                    "Studio requested disconnect",
                )
            )
            self._write_json(
                HTTPStatus.OK, {"ok": True, "result": {"disconnected": disconnected}}
            )
            return
        raise ValidationError("Unknown Studio endpoint")

    def _handle_play_bridge(
        self, body: Dict[str, Any], bearer_token: str
    ) -> None:
        registry = self.v2_server.registry
        common = {
            "studio_id",
            "client_instance_id",
            "document_epoch",
            "transition_generation",
            "play_request_id",
            "expected_place_id",
            "expected_game_id",
            "transition_nonce",
            "server_instance_id",
        }
        if self.path == "/v2/play-bridge/attach":
            self._require_body_fields(body, common | {"attach_id"})
            result = self._submit(
                _call_sync(
                    registry.attach_play_bridge,
                    body["studio_id"],
                    body["client_instance_id"],
                    body["document_epoch"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["expected_place_id"],
                    body["expected_game_id"],
                    body["transition_nonce"],
                    body["attach_id"],
                    body["server_instance_id"],
                    bearer_token,
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/play-bridge/server-poll":
            self._require_body_fields(body, common)
            result = self._submit(
                _call_sync(
                    registry.poll_play_bridge_server,
                    body["studio_id"],
                    body["client_instance_id"],
                    body["document_epoch"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["expected_place_id"],
                    body["expected_game_id"],
                    body["transition_nonce"],
                    body["server_instance_id"],
                    bearer_token,
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/play-bridge/server-ack":
            self._require_body_fields(
                body,
                common | {"ack_kind", "ack_id"},
                {"stop_command_id"},
            )
            result = self._submit(
                _call_sync(
                    registry.acknowledge_play_bridge_stop,
                    body["studio_id"],
                    body["client_instance_id"],
                    body["document_epoch"],
                    body["transition_generation"],
                    body["play_request_id"],
                    body["expected_place_id"],
                    body["expected_game_id"],
                    body["transition_nonce"],
                    body["server_instance_id"],
                    body["ack_kind"],
                    body["ack_id"],
                    body.get("stop_command_id"),
                    bearer_token,
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        raise ValidationError("Unknown Play bridge endpoint")

    def _handle_client(self, body: Dict[str, Any]) -> None:
        service = self.v2_server.service
        principal = self.v2_server.security.client_principal
        if self.path == "/v2/client/lifecycle/status":
            self._require_body_fields(body, set())
            summary = self._submit(
                _call_sync(self.v2_server.registry.lifecycle_summary)
            )
            result = self.v2_server.runtime_info.as_dict()
            result.update(summary)
            counters = self.v2_server.lifecycle_counters()
            result.update(counters)
            lifecycle_blockers = []
            if counters["active_client_operation_count"]:
                lifecycle_blockers.append("active_client_operations")
            if counters["active_studio_mutation_count"]:
                lifecycle_blockers.append("active_studio_mutations")
            if counters["lifecycle_stopping"]:
                lifecycle_blockers.append("shutdown_already_fenced")
            result["lifecycle_blockers"] = lifecycle_blockers
            result["stop_safe"] = (
                summary["stop_safe"] and not lifecycle_blockers
            )
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "result": result,
                },
            )
            return
        if self.path == "/v2/client/lifecycle/stop":
            self._require_body_fields(body, {"broker_instance_id"})
            expected = body["broker_instance_id"]
            if not hmac.compare_digest(
                str(expected), self.v2_server.runtime_info.instance_id
            ):
                raise ValidationError(
                    "Broker instance changed; refusing stale lifecycle stop"
                )
            callback = self.v2_server.shutdown_callback
            if callback is None:
                raise ValidationError(
                    "This broker was not started with lifecycle stop enabled"
                )
            active = self.v2_server.begin_stop_fence()
            if any(active.values()):
                raise SessionConflictError(
                    "Broker stop refused while lifecycle-sensitive operations "
                    "are active",
                    details=active,
                )
            stop_committed = False
            try:
                summary = self._submit(
                    _call_sync(self.v2_server.registry.lifecycle_summary)
                )
                if not summary["stop_safe"]:
                    rendered = "; ".join(
                        item["studio_id"]
                        + "("
                        + ",".join(item["reasons"])
                        + ")"
                        for item in summary["stop_blockers"]
                    )
                    if summary["stop_blockers_truncated"]:
                        rendered += (
                            "; +"
                            + str(
                                summary["stop_blocker_count"]
                                - len(summary["stop_blockers"])
                            )
                            + " additional blocked sessions"
                        )
                    raise SessionConflictError(
                        "Broker stop refused by lifecycle safety gate: " + rendered,
                        details={
                            "stop_blockers": summary["stop_blockers"],
                            "stop_blocker_count": summary[
                                "stop_blocker_count"
                            ],
                            "stop_blockers_truncated": summary[
                                "stop_blockers_truncated"
                            ],
                            "unsafe_transitions": summary["unsafe_transitions"],
                        },
                    )
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "result": {
                            "stopping": True,
                            "broker_instance_id": (
                                self.v2_server.runtime_info.instance_id
                            ),
                        },
                    },
                )
                callback()
                stop_committed = True
            finally:
                if not stop_committed:
                    self.v2_server.cancel_stop_fence()
            return
        if self.path == "/v2/client/tools":
            tools = [copy.deepcopy(DISCOVERY_TOOL)]
            tools.extend(
                tool
                for tool in self.v2_server.catalog.tools_for_mcp()
                if service.policy.can_use_tool(principal, tool["name"])
            )
            tools.extend(
                copy.deepcopy(tool)
                for tool in JOB_TOOLS
                if service.policy.can_use_tool(principal, tool["name"])
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": {"tools": tools}})
            return
        if self.path == "/v2/client/list":
            result = self._submit(_call_sync(service.list_studios, principal))
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/client/call":
            result = self._submit(
                service.call_tool(
                    principal,
                    body.get("tool_name"),
                    body.get("arguments"),
                    client_request_id=body.get("client_request_id"),
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/client/jobs/start":
            result = self._submit(
                _call_sync(
                    service.start_job,
                    principal,
                    body.get("studio_id"),
                    body.get("tool_name"),
                    body.get("tool_arguments"),
                    body.get("timeout_ms"),
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/client/jobs/get":
            result = self._submit(
                _call_sync(
                    service.get_job,
                    principal,
                    body.get("studio_id"),
                    body.get("job_id"),
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/v2/client/jobs/cancel":
            result = self._submit(
                _call_sync(
                    service.cancel_job,
                    principal,
                    body.get("studio_id"),
                    body.get("job_id"),
                )
            )
            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        raise ValidationError("Unknown client endpoint")

    def _write_error(
        self, error: ProxyError, status: Optional[HTTPStatus] = None
    ) -> None:
        response_status = status or HTTPStatus(error.http_status)
        self._write_json(
            response_status, {"ok": False, "error": error.as_dict()}
        )

    def _write_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        encoded = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)


def create_http_server(
    host: str,
    port: int,
    *,
    loop: asyncio.AbstractEventLoop,
    registry: SessionRegistry,
    service: ProxyService,
    catalog: ToolCatalog,
    security: HubSecurityConfig,
    runtime_info: Optional[HubRuntimeInfo] = None,
    shutdown_callback: Optional[Callable[[], None]] = None,
) -> V2HTTPServer:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("The v2 hub only binds to an explicit loopback address")
    security.validate()
    resolved_runtime_info = runtime_info or HubRuntimeInfo.create(catalog)
    resolved_runtime_info.validate()
    return V2HTTPServer(
        (host, port),
        loop=loop,
        registry=registry,
        service=service,
        catalog=catalog,
        security=security,
        runtime_info=resolved_runtime_info,
        shutdown_callback=shutdown_callback,
    )
