from __future__ import annotations

import base64
import binascii
import copy
import json
import sys
import uuid
from typing import Any, Dict, Optional, TextIO

from . import __version__
from .errors import ProxyError
from .frontend import HubClient


JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_SCREENSHOT_BASE64_BYTES = 10_000_000
SCREENSHOT_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MCP_INSTRUCTIONS = (
    "Use Roblox_Studio_Multisession for every operation it supports. Users "
    "identify a target with an ordinary project/place name; never ask them to "
    "copy, remember, or type studio_id, start the broker manually, select a "
    "global active Studio, or coordinate locks between tasks. Before "
    "operating, call list_roblox_studios_v2 internally. Match the requested "
    "name against metadata.name and confirm metadata.place_id, "
    "metadata.game_id, and document_epoch when available. If one session is "
    "the clear match, pass its studio_id only inside each subsequent v2 tool "
    "call. Parallel tasks must each discover and explicitly target their own "
    "session; the broker handles per-session locks. Ask the user only when "
    "duplicate or unsaved names are genuinely ambiguous, and offer "
    "human-readable name/PlaceId/GameId distinctions rather than UUIDs. Never "
    "route through an active or default Studio. V1 is only a disclosed "
    "compatibility fallback for an operation v2 does not support; never fall "
    "back silently, because v1 cannot safely multiplex concurrent Studio "
    "work."
)


class MCPStdioServer:
    """Thin per-Codex frontend; all routing and locks remain in the shared hub."""

    def __init__(self, client: HubClient):
        self.client = client

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "roblox-studio-mcp-multisession",
                        "version": __version__,
                    },
                    "instructions": MCP_INSTRUCTIONS,
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        try:
            if method == "tools/list":
                return self._result(request_id, self.client.tools())
            if method == "tools/call":
                params = message.get("params")
                if not isinstance(params, dict):
                    return self._rpc_error(
                        request_id, -32602, "params must be an object"
                    )
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    return self._rpc_error(
                        request_id, -32602, "Invalid tool name or arguments"
                    )
                result = self._call_tool(name, arguments)
                return self._result(request_id, self._tool_result(result, False))
        except ProxyError as exc:
            return self._result(
                request_id,
                self._tool_result(
                    {"error": exc.as_dict()},
                    True,
                ),
            )
        if request_id is None:
            return None
        return self._rpc_error(request_id, -32601, "Method not found")

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if name == "list_roblox_studios_v2":
            return self.client.list_studios()
        if name == "start_studio_job_v2":
            return self.client.start_job(arguments)
        if name == "get_studio_job_v2":
            return self.client.get_job(arguments)
        if name == "cancel_studio_job_v2":
            return self.client.cancel_job(arguments)
        # Never reuse a raw external MCP id across frontends.
        correlation_id = str(uuid.uuid4())
        return self.client.call(name, arguments, correlation_id)

    @staticmethod
    def _tool_result(value: Any, is_error: bool) -> Dict[str, Any]:
        if (
            not is_error
            and isinstance(value, dict)
            and "image_base64" in value
        ):
            return MCPStdioServer._image_tool_result(value)
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        payload: Dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }
        if isinstance(value, dict):
            payload["structuredContent"] = value
        else:
            payload["structuredContent"] = {"result": value}
        return payload

    @staticmethod
    def _image_tool_result(value: Dict[str, Any]) -> Dict[str, Any]:
        encoded = value.get("image_base64")
        mime_type = value.get("mime_type")
        valid = (
            isinstance(encoded, str)
            and 0 < len(encoded) <= MAX_SCREENSHOT_BASE64_BYTES
            and isinstance(mime_type, str)
            and mime_type in SCREENSHOT_MIME_TYPES
        )
        if valid:
            try:
                base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                valid = False
        if not valid:
            error = {
                "error": {
                    "code": "invalid_image_payload",
                    "message": (
                        "Studio returned an invalid or oversized screenshot payload"
                    ),
                }
            }
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(error, separators=(",", ":")),
                    }
                ],
                "isError": True,
                "structuredContent": error,
            }

        metadata = copy.deepcopy(value)
        del metadata["image_base64"]
        metadata["mime_type"] = mime_type
        return {
            "content": [
                {
                    "type": "image",
                    "data": encoded,
                    "mimeType": mime_type,
                },
                {
                    "type": "text",
                    "text": json.dumps(
                        metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "isError": False,
            "structuredContent": metadata,
        }

    @staticmethod
    def _result(request_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}

    @staticmethod
    def _rpc_error(
        request_id: Any, code: int, message: str
    ) -> Dict[str, Any]:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def serve_stdio(
    client: HubClient,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> None:
    """Run the newline-delimited MCP transport with no non-protocol stdout."""

    server = MCPStdioServer(client)
    for raw_line in input_stream:
        try:
            payload = json.loads(raw_line)
            if not isinstance(payload, dict):
                raise ValueError("JSON-RPC message must be an object")
            response = server.handle(payload)
            if response is not None:
                output_stream.write(
                    json.dumps(response, separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                )
                output_stream.flush()
        except (json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": JSONRPC_VERSION,
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()


def main() -> None:
    try:
        client = HubClient.from_environment()
    except ValueError as exc:
        sys.stderr.write(
            "Studio MCP Multisession frontend refused to start: "
            + str(exc)
            + "\n"
        )
        raise SystemExit(2)
    serve_stdio(client)


if __name__ == "__main__":
    main()
