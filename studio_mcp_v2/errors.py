from __future__ import annotations

from typing import Any, Dict, Optional


class ProxyError(Exception):
    """Typed error that is safe to serialize across the local v2 API."""

    code = "proxy_error"
    http_status = 400

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(ProxyError):
    code = "validation_error"
    http_status = 400


class AuthenticationError(ProxyError):
    code = "authentication_failed"
    http_status = 401


class AuthorizationError(ProxyError):
    code = "forbidden"
    http_status = 403


class SessionNotFoundError(ProxyError):
    code = "studio_not_found"
    http_status = 404


class SessionDisconnectedError(ProxyError):
    code = "studio_disconnected"
    http_status = 409


class SessionConflictError(ProxyError):
    code = "studio_conflict"
    http_status = 409


class StaleGenerationError(ProxyError):
    code = "stale_generation"
    http_status = 409


class CapabilityError(ProxyError):
    code = "unsupported_tool"
    http_status = 422


class RequestTimeoutError(ProxyError):
    code = "studio_timeout"
    http_status = 504


class RemoteToolError(ProxyError):
    code = "studio_tool_error"
    http_status = 502


class JobNotFoundError(ProxyError):
    code = "job_not_found"
    http_status = 404


class UnsafeCancellationError(ProxyError):
    code = "job_already_dispatched"
    http_status = 409
