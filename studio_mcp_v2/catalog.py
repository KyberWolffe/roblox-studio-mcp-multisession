from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .errors import CapabilityError, ValidationError
from .schema_validation import validate_input_schema_definition


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
    input_schema_sha256: str
    output_schema_sha256: str
    handler_contract_sha256: str
    _input_schema_json: str = field(repr=False)
    _output_schema_json: str = field(repr=False)

    @property
    def input_schema(self) -> Dict[str, Any]:
        """Return an isolated copy of the exact raw pre-injection schema."""

        value = json.loads(self._input_schema_json)
        if not isinstance(value, dict):
            raise ValidationError("Stored input schema is not an object")
        return value

    @property
    def output_schema(self) -> Any:
        """Return an isolated copy of the exact raw output contract."""

        return json.loads(self._output_schema_json)


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
            input_schema = copy.deepcopy(
                dict(raw).get(
                    "inputSchema", {"type": "object"}
                )
            )
            validate_input_schema_definition(input_schema)
            output_schema = copy.deepcopy(
                dict(raw).get("outputSchema")
            )

            def canonical(value: Any) -> str:
                return json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )

            def digest(value: Any) -> str:
                return hashlib.sha256(
                    canonical(value).encode("utf-8")
                ).hexdigest()

            input_digest = digest(input_schema)
            output_digest = digest(output_schema)
            definitions[public_name] = ToolDefinition(
                public_name=public_name,
                remote_name=remote_name,
                mcp_definition=definition,
                input_schema_sha256=input_digest,
                output_schema_sha256=output_digest,
                handler_contract_sha256=digest(
                    {
                        "name": remote_name,
                        "inputSchema": input_schema,
                        "outputSchema": output_schema,
                    }
                ),
                _input_schema_json=canonical(input_schema),
                _output_schema_json=canonical(output_schema),
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


JOB_RECEIPT_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "$defs": {
        "nullableSha256": {
            "anyOf": [
                {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                {"type": "null"},
            ]
        },
        "nullableUuid": {
            "anyOf": [
                {"type": "string", "format": "uuid"},
                {"type": "null"},
            ]
        },
        "nullableInteger": {
            "anyOf": [
                {"type": "integer", "minimum": 0},
                {"type": "null"},
            ]
        },
        "nullableString": {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        },
        "path": {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "pattern": r"^[^\u0000-\u001f\u007f]+$",
            },
            "minItems": 0,
            "maxItems": 64,
        },
        "multiEditTarget": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 16,
                },
                "path": {
                    "allOf": [
                        {"$ref": "#/$defs/path"},
                        {"minItems": 1},
                    ]
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "edit_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64,
                },
            },
            "required": [
                "index",
                "path",
                "expected_sha256",
                "edit_count",
            ],
            "additionalProperties": False,
        },
        "multiEditAdmission": {
            "type": "object",
            "properties": {
                "contract_version": {
                    "type": "string",
                    "const": "studio-job-admission-v1",
                },
                "operation": {
                    "type": "string",
                    "const": "studio_multi_edit",
                },
                "datamodel_type": {
                    "type": "string",
                    "const": "Edit",
                },
                "target_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 16,
                },
                "edit_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 128,
                },
                "ordering_version": {
                    "type": "string",
                    "const": "target-input-edit-input-v1",
                },
                "atomicity": {
                    "type": "string",
                    "const": (
                        "preflight-all-per-target-cas-compensating-no-"
                        "cross-script-atomicity-v1"
                    ),
                },
                "targets": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/multiEditTarget"},
                    "minItems": 1,
                    "maxItems": 16,
                },
            },
            "required": [
                "contract_version",
                "operation",
                "datamodel_type",
                "target_count",
                "edit_count",
                "ordering_version",
                "atomicity",
                "targets",
            ],
            "additionalProperties": False,
        },
        "treeAdmission": {
            "type": "object",
            "properties": {
                "contract_version": {
                    "type": "string",
                    "const": "studio-job-admission-v1",
                },
                "operation": {
                    "type": "string",
                    "const": "studio_list_tree",
                },
                "root_path": {"$ref": "#/$defs/path"},
                "max_depth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 6,
                },
                "scan_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                },
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": [
                "contract_version",
                "operation",
                "root_path",
                "max_depth",
                "scan_limit",
                "page_size",
            ],
            "additionalProperties": False,
        },
        "scriptQueryAdmission": {
            "type": "object",
            "properties": {
                "contract_version": {
                    "type": "string",
                    "const": "studio-job-admission-v1",
                },
                "operation": {
                    "type": "string",
                    "enum": [
                        "studio_search_scripts",
                        "studio_grep_scripts",
                    ],
                },
                "root_path": {"$ref": "#/$defs/path"},
                "max_depth": {
                    "anyOf": [
                        {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 64,
                        },
                        {"type": "null"},
                    ]
                },
                "scan_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                },
                "page_size": {
                    "anyOf": [
                        {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        {"type": "null"},
                    ]
                },
                "time_limit_ms": {
                    "anyOf": [
                        {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 10000,
                        },
                        {"type": "null"},
                    ]
                },
                "query_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": [
                "contract_version",
                "operation",
                "root_path",
                "max_depth",
                "scan_limit",
                "page_size",
                "time_limit_ms",
                "query_sha256",
            ],
            "additionalProperties": False,
        },
        "inspectionAdmission": {
            "type": "object",
            "properties": {
                "contract_version": {
                    "type": "string",
                    "const": "studio-job-admission-v1",
                },
                "operation": {
                    "type": "string",
                    "const": "studio_inspect_instance",
                },
                "path": {
                    "allOf": [
                        {"$ref": "#/$defs/path"},
                        {"minItems": 1},
                    ]
                },
                "child_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 200,
                },
                "descendant_max_depth": {
                    "anyOf": [
                        {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 64,
                        },
                        {"type": "null"},
                    ]
                },
                "descendant_scan_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                },
                "time_limit_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 10000,
                },
            },
            "required": [
                "contract_version",
                "operation",
                "path",
                "child_limit",
                "descendant_max_depth",
                "descendant_scan_limit",
                "time_limit_ms",
            ],
            "additionalProperties": False,
        },
        "hashedAdmission": {
            "type": "object",
            "properties": {
                "contract_version": {
                    "type": "string",
                    "const": "studio-job-admission-v1",
                },
                "operation": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "arguments_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": [
                "contract_version",
                "operation",
                "arguments_sha256",
            ],
            "additionalProperties": False,
        },
        "admittedContract": {
            "oneOf": [
                {"$ref": "#/$defs/multiEditAdmission"},
                {"$ref": "#/$defs/treeAdmission"},
                {"$ref": "#/$defs/scriptQueryAdmission"},
                {"$ref": "#/$defs/inspectionAdmission"},
                {"$ref": "#/$defs/hashedAdmission"},
            ]
        },
        "phaseReceipt": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "format": "uuid"},
                "generation": {"type": "integer", "minimum": 1},
                "phase": {
                    "type": "string",
                    "enum": ["direct", "prepare", "apply", "recover"],
                },
                "success": {"type": "boolean"},
                "safe_terminal": {"type": "boolean"},
                "recovery_required": {"type": "boolean"},
                "outcome": {"$ref": "#/$defs/nullableString"},
                "result_sha256": {
                    "$ref": "#/$defs/nullableSha256"
                },
                "result_bytes": {
                    "$ref": "#/$defs/nullableInteger"
                },
            },
            "required": [
                "request_id",
                "generation",
                "phase",
                "success",
                "safe_terminal",
                "recovery_required",
                "outcome",
                "result_sha256",
                "result_bytes",
            ],
            "allOf": [
                {
                    "oneOf": [
                        {
                            "properties": {
                                "safe_terminal": {"const": True},
                                "recovery_required": {"const": False},
                            }
                        },
                        {
                            "properties": {
                                "safe_terminal": {"const": False},
                                "recovery_required": {"const": True},
                            }
                        },
                    ]
                },
                {
                    "if": {
                        "properties": {"success": {"const": True}},
                        "required": ["success"],
                    },
                    "then": {
                        "properties": {
                            "outcome": {"type": "string"},
                            "result_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "result_bytes": {
                                "type": "integer",
                                "minimum": 0,
                            },
                        }
                    },
                    "else": {
                        "properties": {
                            "outcome": {"type": "string"},
                            "result_sha256": {"type": "null"},
                            "result_bytes": {"type": "null"},
                        }
                    },
                },
            ],
            "additionalProperties": False,
        },
        "resolutionReceipt": {
            "type": "object",
            "description": (
                "External exact-recovery proof linked to an original "
                "outcome-unknown multi-edit job. The broker additionally "
                "enforces request_id uniqueness within resolution_receipts; "
                "the nested result must match this receipt's Studio, client, "
                "document, generation, request, transaction, outcome, and "
                "receipt_sha256 identities, while result_sha256 and "
                "result_bytes bind its exact canonical JSON."
            ),
            "properties": {
                "format": {
                    "type": "string",
                    "const": "studio-mcp-v2-job-resolution",
                },
                "schema_version": {"type": "integer", "const": 1},
                "kind": {
                    "type": "string",
                    "const": "exact_multi_edit_recovery",
                },
                "studio_id": {"type": "string", "format": "uuid"},
                "client_instance_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "document_epoch": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": (
                        "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
                    ),
                },
                "generation": {"type": "integer", "minimum": 1},
                "request_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "transaction_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "operation": {
                    "type": "string",
                    "const": "studio_recover_multi_edit",
                },
                "phase": {"type": "string", "const": "recover"},
                "source": {
                    "type": "string",
                    "enum": ["direct", "job"],
                },
                "resolver_job_id": {
                    "type": "string",
                    "maxLength": 128,
                },
                "success": {"type": "boolean", "const": True},
                "safe_terminal": {
                    "type": "boolean",
                    "const": True,
                },
                "recovery_required": {
                    "type": "boolean",
                    "const": False,
                },
                "outcome": {"type": "string", "const": "recovered"},
                "receipt_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "result_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "result_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100000,
                },
                "result": {"$ref": "#/$defs/recoveryResult"},
            },
            "required": [
                "format",
                "schema_version",
                "kind",
                "studio_id",
                "client_instance_id",
                "document_epoch",
                "generation",
                "request_id",
                "transaction_id",
                "operation",
                "phase",
                "source",
                "resolver_job_id",
                "success",
                "safe_terminal",
                "recovery_required",
                "outcome",
                "receipt_sha256",
                "result_sha256",
                "result_bytes",
                "result",
            ],
            "allOf": [
                {
                    "if": {
                        "properties": {"source": {"const": "direct"}},
                        "required": ["source"],
                    },
                    "then": {
                        "properties": {
                            "resolver_job_id": {"const": ""}
                        }
                    },
                    "else": {
                        "properties": {
                            "resolver_job_id": {"minLength": 1}
                        }
                    },
                }
            ],
            "additionalProperties": False,
        },
        "recoveryTargetReceipt": {
            "type": "object",
            "description": (
                "A recovered target. The broker validates that "
                "observed_before_sha256 equals prepared_sha256 or "
                "planned_sha256 and observed_after_sha256 equals "
                "prepared_sha256."
            ),
            "properties": {
                "index": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 16,
                },
                "path": {
                    "allOf": [
                        {"$ref": "#/$defs/path"},
                        {"minItems": 1},
                    ]
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "prepared_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "planned_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "observed_before_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "observed_after_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "source_length": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 262144,
                },
                "planned_source_length": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 262144,
                },
                "edit_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64,
                },
                "replacement_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1024,
                },
                "status": {
                    "type": "string",
                    "const": "rolled_back",
                },
            },
            "required": [
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
            ],
            "additionalProperties": False,
        },
        "recoveryResult": {
            "type": "object",
            "properties": {
                "adapter": {
                    "type": "string",
                    "const": "studio-mcp-v2-durable-plugin",
                },
                "v": {"type": "integer", "const": 1},
                "operation": {
                    "type": "string",
                    "const": "studio_recover_multi_edit",
                },
                "phase": {"type": "string", "const": "recover"},
                "studio_id": {"type": "string", "format": "uuid"},
                "client_instance_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "document_epoch": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "pattern": (
                        "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
                    ),
                },
                "generation": {"type": "integer", "minimum": 1},
                "request_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "transaction_id": {
                    "type": "string",
                    "format": "uuid",
                },
                "prepare_request_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "prepare_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "ordering_version": {
                    "type": "string",
                    "const": "target-input-edit-input-v1",
                },
                "atomicity": {
                    "type": "string",
                    "const": (
                        "preflight-all-per-target-cas-compensating-no-"
                        "cross-script-atomicity-v1"
                    ),
                },
                "receipt_contract": {
                    "type": "string",
                    "const": "broker-validated-downstream-ack-v1",
                },
                "outcome": {"type": "string", "const": "recovered"},
                "safe_terminal": {
                    "type": "boolean",
                    "const": True,
                },
                "recovery_required": {
                    "type": "boolean",
                    "const": False,
                },
                "target_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 16,
                },
                "edit_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 128,
                },
                "targets": {
                    "type": "array",
                    "items": {
                        "$ref": "#/$defs/recoveryTargetReceipt"
                    },
                    "minItems": 1,
                    "maxItems": 16,
                },
                "receipt_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": [
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
            ],
            "additionalProperties": False,
        },
        "error": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "message": {"type": "string", "minLength": 1},
                "details": {
                    "type": "object",
                    "description": (
                        "Typed error-specific details; their shape belongs "
                        "to the named error code."
                    ),
                },
            },
            "required": ["code", "message"],
            "additionalProperties": False,
        },
    },
    "properties": {
        "format": {
            "type": "string",
            "const": "studio-mcp-v2-job-receipt",
        },
        "schema_version": {"type": "integer", "const": 1},
        "job_id": {"type": "string", "format": "uuid"},
        "studio_id": {"type": "string", "format": "uuid"},
        "client_instance_id": {"type": "string", "format": "uuid"},
        "document_epoch": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        },
        "generation": {"type": "integer", "minimum": 1},
        "tool_name": {
            "type": "string",
            "pattern": "^[A-Za-z][A-Za-z0-9_]*_v2$",
        },
        "remote_tool": {
            "type": "string",
            "pattern": "^[A-Za-z][A-Za-z0-9_]*$",
        },
        "input_schema_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "output_schema_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "handler_contract_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "arguments_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "transaction_id": {"$ref": "#/$defs/nullableUuid"},
        "admitted_contract": {
            "$ref": "#/$defs/admittedContract"
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 1,
            "maximum": 120000,
        },
        "admission_sequence": {"type": "integer", "minimum": 1},
        "status": {
            "type": "string",
            "enum": [
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
                "outcome_unknown",
            ],
        },
        "dispatched": {"type": "boolean"},
        "dispatched_request_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "maxItems": 2,
        },
        "dispatched_phases": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["direct", "prepare", "apply", "recover"],
            },
            "maxItems": 2,
        },
        "phase_receipts": {
            "type": "array",
            "items": {"$ref": "#/$defs/phaseReceipt"},
            "maxItems": 2,
        },
        "resolution_receipts": {
            "type": "array",
            "items": {"$ref": "#/$defs/resolutionReceipt"},
            "maxItems": 4,
            "uniqueItems": True,
        },
        "cancellation_state": {
            "type": "string",
            "enum": [
                "not_requested",
                "requested_after_dispatch_refused",
                "requested_after_dispatch_not_acknowledged",
                "acknowledged_before_dispatch",
            ],
        },
        "terminal": {"type": "boolean"},
        "terminal_outcome": {"$ref": "#/$defs/nullableString"},
        "result_present": {"type": "boolean"},
        "error_present": {"type": "boolean"},
        "result_sha256": {"$ref": "#/$defs/nullableSha256"},
        "result_bytes": {"$ref": "#/$defs/nullableInteger"},
        "created_at": {"type": "number", "minimum": 0},
        "updated_at": {"type": "number", "minimum": 0},
        "result": {},
        "error": {"$ref": "#/$defs/error"},
    },
    "required": [
        "format",
        "schema_version",
        "job_id",
        "studio_id",
        "client_instance_id",
        "document_epoch",
        "generation",
        "tool_name",
        "remote_tool",
        "input_schema_sha256",
        "output_schema_sha256",
        "handler_contract_sha256",
        "arguments_sha256",
        "transaction_id",
        "admitted_contract",
        "timeout_ms",
        "admission_sequence",
        "status",
        "dispatched",
        "dispatched_request_ids",
        "dispatched_phases",
        "phase_receipts",
        "resolution_receipts",
        "cancellation_state",
        "terminal",
        "terminal_outcome",
        "result_present",
        "error_present",
        "result_sha256",
        "result_bytes",
        "created_at",
        "updated_at",
    ],
    "allOf": [
        {
            "if": {
                "properties": {"result_present": {"const": True}},
                "required": ["result_present"],
            },
            "then": {
                "required": ["result"],
                "properties": {
                    "result_sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "result_bytes": {
                        "type": "integer",
                        "minimum": 0,
                    },
                },
            },
            "else": {
                "not": {"required": ["result"]},
                "properties": {
                    "result_sha256": {"type": "null"},
                    "result_bytes": {"type": "null"},
                },
            },
        },
        {
            "if": {
                "properties": {"error_present": {"const": True}},
                "required": ["error_present"],
            },
            "then": {"required": ["error"]},
            "else": {"not": {"required": ["error"]}},
        },
        {
            "if": {
                "properties": {"terminal": {"const": True}},
                "required": ["terminal"],
            },
            "then": {
                "properties": {
                    "status": {
                        "enum": ["completed", "failed", "cancelled"]
                    },
                    "terminal_outcome": {"type": "string"},
                }
            },
            "else": {
                "properties": {
                    "status": {
                        "enum": [
                            "queued",
                            "running",
                            "outcome_unknown",
                        ]
                    }
                }
            },
        },
        {
            "if": {
                "properties": {
                    "status": {"enum": ["queued", "running"]}
                },
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "terminal_outcome": {"type": "null"}
                }
            },
            "else": {
                "properties": {
                    "terminal_outcome": {"type": "string"}
                }
            },
        },
        {
            "if": {
                "properties": {"status": {"const": "completed"}},
                "required": ["status"],
            },
            "then": {
                "properties": {"error_present": {"const": False}},
                "anyOf": [
                    {
                        "properties": {
                            "result_present": {"const": True}
                        }
                    },
                    {
                        "properties": {
                            "result_present": {"const": False},
                            "resolution_receipts": {"minItems": 1},
                            "terminal_outcome": {
                                "const": (
                                    "resolved_by_exact_recovery:recovered"
                                )
                            },
                        }
                    },
                ],
            },
        },
        {
            "if": {
                "properties": {
                    "resolution_receipts": {"minItems": 1}
                },
                "required": ["resolution_receipts"],
            },
            "then": {
                "properties": {
                    "status": {"const": "completed"},
                    "terminal": {"const": True},
                    "terminal_outcome": {
                        "const": "resolved_by_exact_recovery:recovered"
                    },
                    "remote_tool": {"const": "studio_multi_edit"},
                }
            },
        },
        {
            "if": {
                "properties": {
                    "status": {"const": "failed"}
                },
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "result_present": {"const": False},
                    "error_present": {"const": True},
                }
            },
        },
        {
            "if": {
                "properties": {
                    "status": {"const": "cancelled"}
                },
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "result_present": {"const": False},
                    "error_present": {"const": False},
                }
            },
        },
        {
            "if": {
                "properties": {
                    "status": {"enum": ["queued", "running"]}
                },
                "required": ["status"],
            },
            "then": {
                "properties": {
                    "result_present": {"const": False},
                    "error_present": {"const": False},
                }
            },
        },
        {
            "if": {
                "properties": {"dispatched": {"const": False}},
                "required": ["dispatched"],
            },
            "then": {
                "properties": {
                    "dispatched_request_ids": {"maxItems": 0},
                    "dispatched_phases": {"maxItems": 0},
                    "phase_receipts": {"maxItems": 0},
                    "resolution_receipts": {"maxItems": 0},
                    "transaction_id": {"type": "null"},
                }
            },
            "else": {
                "properties": {
                    "dispatched_request_ids": {"minItems": 1},
                    "dispatched_phases": {"minItems": 1},
                }
            },
        },
    ],
    "additionalProperties": False,
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
        "outputSchema": copy.deepcopy(JOB_RECEIPT_OUTPUT_SCHEMA),
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
        "outputSchema": copy.deepcopy(JOB_RECEIPT_OUTPUT_SCHEMA),
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
        "outputSchema": copy.deepcopy(JOB_RECEIPT_OUTPUT_SCHEMA),
        "annotations": {
            "title": "Cancel Studio Job v2",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]
