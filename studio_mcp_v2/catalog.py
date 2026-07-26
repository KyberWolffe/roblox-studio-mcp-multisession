from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .errors import CapabilityError, ValidationError


STUDIO_ID_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "format": "uuid",
    "description": (
        "Agent-internal explicit target returned by list_roblox_studios_v2 "
        "after resolving the user's ordinary place/project name. Never ask "
        "the user to copy, remember, or type this UUID. This is routing "
        "context, not authorization."
    ),
}


@dataclass(frozen=True)
class ToolDefinition:
    public_name: str
    remote_name: str
    mcp_definition: Dict[str, Any]


class ToolCatalog:
    """Operator-owned catalog. Studio handshakes may select, never inject, tools."""

    def __init__(self, tools: Iterable[Mapping[str, Any]]):
        definitions: Dict[str, ToolDefinition] = {}
        remote_to_public: Dict[str, str] = {}
        for raw in tools:
            remote_name = raw.get("name")
            if not isinstance(remote_name, str) or not remote_name:
                raise ValidationError("Catalog tool names must be non-empty strings")
            public_name = remote_name if remote_name.endswith("_v2") else remote_name + "_v2"
            if public_name in definitions or remote_name in remote_to_public:
                raise ValidationError("Duplicate tool in catalog: " + remote_name)
            definition = self._inject_target(raw, public_name)
            definitions[public_name] = ToolDefinition(
                public_name=public_name,
                remote_name=remote_name,
                mcp_definition=definition,
            )
            remote_to_public[remote_name] = public_name
        self._definitions = definitions
        self._remote_to_public = remote_to_public

    @staticmethod
    def _inject_target(
        raw: Mapping[str, Any], public_name: str
    ) -> Dict[str, Any]:
        result = copy.deepcopy(dict(raw))
        result["name"] = public_name
        result["description"] = (
            "[Roblox Studio MCP v2: resolve the user's named place with "
            "list_roblox_studios_v2, then pass its studio_id internally; "
            "never use a global/default Studio] "
            + str(result.get("description", ""))
        ).strip()
        schema = result.setdefault("inputSchema", {"type": "object"})
        if not isinstance(schema, dict):
            raise ValidationError("Tool inputSchema must be an object")
        schema["type"] = "object"
        properties = schema.setdefault("properties", {})
        if not isinstance(properties, dict):
            raise ValidationError("Tool schema properties must be an object")
        properties["studio_id"] = copy.deepcopy(STUDIO_ID_SCHEMA)
        required = schema.setdefault("required", [])
        if not isinstance(required, list):
            raise ValidationError("Tool schema required must be an array")
        if "studio_id" not in required:
            required.append("studio_id")
        return result

    @classmethod
    def from_file(cls, path: Path) -> "ToolCatalog":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        tools = payload.get("tools") if isinstance(payload, dict) else None
        if not isinstance(tools, list):
            raise ValidationError("Catalog file must contain a tools array")
        return cls(tools)

    def get(self, public_name: str) -> ToolDefinition:
        try:
            return self._definitions[public_name]
        except KeyError:
            raise CapabilityError("Unknown v2 tool: " + str(public_name))

    def public_for_remote(self, remote_name: str) -> str:
        try:
            return self._remote_to_public[remote_name]
        except KeyError:
            raise CapabilityError("Remote tool is not approved: " + remote_name)

    def tools_for_mcp(self) -> List[Dict[str, Any]]:
        return [
            copy.deepcopy(item.mcp_definition)
            for item in self._definitions.values()
        ]

    @property
    def remote_names(self) -> frozenset:
        return frozenset(self._remote_to_public)


DISCOVERY_TOOL: Dict[str, Any] = {
    "name": "list_roblox_studios_v2",
    "description": (
        "Internal first step for resolving the ordinary place/project name "
        "given by the user. List authenticated v2 Studio sessions, match "
        "metadata.name, and confirm metadata.place_id, metadata.game_id, and "
        "document_epoch when available. When exactly one session matches, "
        "pass its studio_id only in subsequent v2 tool calls; never ask the "
        "user to supply or manage that UUID. If duplicate or unsaved names "
        "remain genuinely ambiguous, ask the user to distinguish the windows "
        "using human-readable name/PlaceId/GameId details. This is the only "
        "v2 tool that does not take studio_id."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "annotations": {
        "title": "List Roblox Studios v2",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


JOB_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "start_studio_job_v2",
        "description": "Start an approved Studio tool as a session-scoped background job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "studio_id": copy.deepcopy(STUDIO_ID_SCHEMA),
                "tool_name": {
                    "type": "string",
                    "description": "An approved v2 MCP tool name.",
                },
                "tool_arguments": {"type": "object"},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120000,
                },
            },
            "required": ["studio_id", "tool_name", "tool_arguments"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Start Studio Job v2",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "get_studio_job_v2",
        "description": "Read one job from the explicitly targeted Studio session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "studio_id": copy.deepcopy(STUDIO_ID_SCHEMA),
                "job_id": {"type": "string"},
            },
            "required": ["studio_id", "job_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Get Studio Job v2",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "cancel_studio_job_v2",
        "description": (
            "Cancel a queued session job. Dispatched Studio mutations are not "
            "claimed cancellable without downstream acknowledgement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "studio_id": copy.deepcopy(STUDIO_ID_SCHEMA),
                "job_id": {"type": "string"},
            },
            "required": ["studio_id", "job_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Cancel Studio Job v2",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]
