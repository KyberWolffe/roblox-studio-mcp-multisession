from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from .errors import ProxyError


class HubClientError(ProxyError):
    code = "hub_client_error"
    http_status = 502


class HubTransportError(HubClientError):
    """The loopback exchange failed before a broker response was available."""

    code = "hub_transport_error"


class HubClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float = 130.0):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("The v2 frontend only connects to an explicit loopback HTTP hub")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid v2 hub URL")
        if len(token) < 32:
            raise ValueError("STUDIO_MCP_V2_CLIENT_TOKEN must be at least 32 chars")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "HubClient":
        port = int(os.environ.get("STUDIO_MCP_V2_PORT", "44756"))
        url = os.environ.get(
            "STUDIO_MCP_V2_HUB_URL", f"http://127.0.0.1:{port}"
        )
        token = os.environ.get("STUDIO_MCP_V2_CLIENT_TOKEN", "")
        return cls(url, token)

    def post(self, path: str, payload: Dict[str, Any]) -> Any:
        if not path.startswith("/v2/client/"):
            raise ValueError("Frontend may only call authenticated v2 client endpoints")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Content-Length": str(len(encoded)),
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
                error = result.get("error", {})
                raise HubClientError(
                    str(error.get("message", "v2 hub rejected request")),
                    details={"remote_code": error.get("code")},
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HubClientError("v2 hub returned an HTTP error")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubTransportError(
                "Could not reach the isolated v2 hub: " + str(exc)
            )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise HubClientError("Malformed response from isolated v2 hub")
        return result.get("result")

    def tools(self) -> Dict[str, Any]:
        return self.post("/v2/client/tools", {})

    def lifecycle_status(self) -> Dict[str, Any]:
        return self.post("/v2/client/lifecycle/status", {})

    def lifecycle_stop(self, broker_instance_id: str) -> Dict[str, Any]:
        return self.post(
            "/v2/client/lifecycle/stop",
            {"broker_instance_id": broker_instance_id},
        )

    def list_studios(self) -> Dict[str, Any]:
        return self.post("/v2/client/list", {})

    def call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        client_request_id: str,
    ) -> Any:
        return self.post(
            "/v2/client/call",
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "client_request_id": client_request_id,
            },
        )

    def start_job(self, arguments: Dict[str, Any]) -> Any:
        return self.post(
            "/v2/client/jobs/start",
            {
                "studio_id": arguments.get("studio_id"),
                "tool_name": arguments.get("tool_name"),
                "tool_arguments": arguments.get("tool_arguments"),
                "timeout_ms": arguments.get("timeout_ms"),
            },
        )

    def get_job(self, arguments: Dict[str, Any]) -> Any:
        return self.post(
            "/v2/client/jobs/get",
            {
                "studio_id": arguments.get("studio_id"),
                "job_id": arguments.get("job_id"),
            },
        )

    def cancel_job(self, arguments: Dict[str, Any]) -> Any:
        return self.post(
            "/v2/client/jobs/cancel",
            {
                "studio_id": arguments.get("studio_id"),
                "job_id": arguments.get("job_id"),
            },
        )
