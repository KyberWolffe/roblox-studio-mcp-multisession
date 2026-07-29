"""Render the isolated Studio MCP v2 plugin without installing or running it.

The pure ``render_fresh_bundle`` API is the preferred orchestration boundary:
it creates a new hub Studio token and run ID together and returns both beside
the package source, so the same token can be supplied to the side-by-side hub.
The CLI writes only to stdout and never touches a plugin directory or Studio.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent.parent
PLUGIN_TEMPLATE = ROOT / "scripts" / "studio_plugin_template.luau"
SERVER_BRIDGE_TEMPLATE = ROOT / "scripts" / "play_server_bridge.luau"
DURABLE_HANDLERS_TEMPLATE = (
    ROOT / "scripts" / "durable_operation_handlers.luau"
)
STUDIO_TOKEN_ENV = "STUDIO_MCP_V2_STUDIO_TOKEN"
SAFE_SECRET = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9]+$")
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")
DEFAULT_BASE_URL = "http://127.0.0.1:44756"


@dataclass(frozen=True)
class RenderedPluginBundle:
    studio_token: str
    run_id: str
    plugin_source: str
    plugin_package_rbxmx: str


def validate_studio_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 256
        or SAFE_SECRET.fullmatch(value) is None
    ):
        raise ValueError("Studio token must be 32-256 routing-safe characters")
    return value


def validate_run_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 64
        or SAFE_RUN_ID.fullmatch(value) is None
    ):
        raise ValueError("run_id must be 16-64 alphanumeric characters")
    return value


def validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("base_url must be text")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "base_url must be an explicit http://127.0.0.1:<port> origin"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url port is invalid") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("base_url requires a port from 1 to 65535")
    return f"http://127.0.0.1:{port}"


def _replace_once(source: str, placeholder: str, value: str) -> str:
    if source.count(placeholder) != 1:
        raise ValueError("template placeholder count is invalid: " + placeholder)
    return source.replace(placeholder, value)


def _replace_exact(source: str, old: str, new: str, label: str) -> str:
    if source.count(old) != 1:
        raise ValueError(
            "durable source transform drifted at "
            + label
            + "; refusing to render"
        )
    return source.replace(old, new)


def _replace_region(
    source: str,
    start_marker: str,
    end_marker: str,
    replacement: str,
    label: str,
) -> str:
    if source.count(start_marker) != 1 or source.count(end_marker) != 1:
        raise ValueError(
            "durable source transform drifted at "
            + label
            + "; refusing to render"
        )
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[:start] + replacement + "\n\n" + source[end:]


def _luau_long_string(value: str) -> str:
    """Return a Luau long-bracket literal that cannot close inside ``value``."""

    for equals_count in range(0, 32):
        equals = "=" * equals_count
        opener = "[" + equals + "["
        closer = "]" + equals + "]"
        if closer not in value:
            return opener + "\n" + value + "\n" + closer
    raise ValueError("unable to quote fixed server bridge source")


def _durable_server_template(source: str, base_url: str) -> str:
    source = _replace_exact(
        source,
        """--[[
Fixed Studio MCP v2 PlayServer plugin bridge.

This source is embedded twice in the rendered Studio plugin: as executable
PlayServer plugin code and as the inert temporary Script marker's auditable
source. It accepts only the closed ExecutePlayModeAsync bootstrap schema below.
The loopback origin, endpoint paths, retry limits, watchdog duration, and
EndTest payload are not caller controlled.
]]""",
        """--[[
Durable Studio MCP v2 PlayServer plugin bridge.

This fixed source is embedded as executable plugin code and as the inert
temporary Script marker's auditable source at install time. It accepts one
exact authenticated Play bootstrap for the current DataModel. The loopback
origin, endpoint paths, retry limits, watchdog duration, and EndTest payload
are not caller controlled.
]]""",
        "server header",
    )
    source = _replace_exact(
        source,
        'local BASE_URL = "http://127.0.0.1:44756"',
        'local BASE_URL = "' + base_url + '"',
        "server loopback origin",
    )
    source = source.replace(
        "studio_mcp_v2_validation_abort",
        "studio_mcp_v2_watchdog_abort",
    ).replace(
        "hard_watchdog_validation_abort",
        "hard_watchdog_bootstrap_abort",
    )
    return source


def _durable_plugin_template(
    source: str,
    handlers: str,
    base_url: str,
) -> str:
    source = _replace_region(
        source,
        "--[[\nStandalone, side-by-side Studio MCP v2 R&D plugin template.",
        "local InitialRunService = game:GetService(\"RunService\")",
        """--[[
Durable, side-by-side Studio MCP v2 plugin template.

Every Studio plugin runtime generates a distinct client UUID, registration
secret, document epoch, and derived session tag. The broker assigns studio_id.
All operational requests carry that exact studio_id; there is no active or
default Studio pointer and no hard-coded place/session count.
]]""",
        "plugin header",
    )
    source = _replace_exact(
        source,
        'local BASE_URL = "http://127.0.0.1:44756"',
        'local BASE_URL = "' + base_url + '"',
        "plugin loopback origin",
    )
    source = _replace_exact(
        source,
        'local SINGLETON_KEY = "StudioMCPv2StandaloneRndPluginSingleton"',
        'local SINGLETON_KEY = "StudioMCPv2DurableSideBySidePluginSingleton"',
        "plugin singleton",
    )
    source = _replace_exact(
        source,
        """local MAX_HTTP_BODY_BYTES = 65_536
local MAX_HTTP_RESPONSE_BYTES = 65_536
local MAX_ARGS_BYTES = 4_096""",
        """local MAX_HTTP_BODY_BYTES = 1_000_000
local MAX_HTTP_RESPONSE_BYTES = 1_000_000
local MAX_ARGS_BYTES = 350_000""",
        "plugin durable bounds",
    )
    source = _replace_region(
        source,
        "local CAPABILITIES = table.freeze({",
        "local REQUEST_KEYS = table.freeze({",
        """local CAPABILITIES = table.freeze({
\t"studio_get_state",
\t"studio_list_tree",
\t"studio_read_script",
\t"studio_update_script",
\t"studio_set_attribute",
\t"studio_get_console",
\t"studio_capture_screenshot",
\t"studio_fire_input_binding",
\t"studio_start_stop_play",
})

local CAPABILITY_SET = table.freeze({
\tstudio_get_state = true,
\tstudio_list_tree = true,
\tstudio_read_script = true,
\tstudio_update_script = true,
\tstudio_set_attribute = true,
\tstudio_get_console = true,
\tstudio_capture_screenshot = true,
\tstudio_fire_input_binding = true,
\tstudio_start_stop_play = true,
})""",
        "plugin durable capabilities",
    )
    source = _replace_exact(
        source,
        """\tresume_token = nil,
\tconnected = false,""",
        """\tresume_token = nil,
\tbroker_instance_id = nil,
\tconnected = false,""",
        "plugin broker identity state",
    )
    source = _replace_exact(
        source,
        """\t\tadapter = "studio-mcp-v2-standalone-plugin",
\t\trun_id = CONFIG.run_id,
\t\tsession_tag = DOCUMENT_POLICY.session_tag,""",
        """\t\tadapter = "studio-mcp-v2-durable-plugin",
\t\trun_id = CONFIG.run_id,
\t\tsession_tag = DOCUMENT_POLICY.session_tag,""",
        "plugin connection metadata",
    )
    source = _replace_exact(
        source,
        """\t\tor result.v ~= 2
\t\tor not isCanonicalUuid(result.studio_id)
\t\tor result.document_epoch ~= DOCUMENT_EPOCH""",
        """\t\tor result.v ~= 2
\t\tor not isCanonicalUuid(result.studio_id)
\t\tor not isCanonicalUuid(result.broker_instance_id)
\t\tor result.document_epoch ~= DOCUMENT_EPOCH""",
        "plugin connect broker validation",
    )
    source = _replace_exact(
        source,
        """\tif reconnecting
\t\tand (result.studio_id ~= peer.studio_id
\t\t\tor result.generation <= peer.generation)
\tthen
\t\treturn false, "hub changed reconnect identity"
\tend""",
        """\tif reconnecting
\t\tand (result.studio_id ~= peer.studio_id
\t\t\tor result.generation <= peer.generation
\t\t\tor result.broker_instance_id ~= peer.broker_instance_id)
\tthen
\t\treturn false, "hub changed reconnect identity"
\tend""",
        "plugin reconnect broker fence",
    )
    source = _replace_exact(
        source,
        """\tpeer.generation = result.generation
\tpeer.resume_token = result.resume_token
\tpeer.connected = true""",
        """\tpeer.generation = result.generation
\tpeer.resume_token = result.resume_token
\tpeer.broker_instance_id = result.broker_instance_id
\tpeer.connected = true""",
        "plugin accepted broker identity",
    )
    source = _replace_region(
        source,
        "local function validateArgs(operation, args, deadlineMs)",
        "local function cacheResponse(request, signature, success, result, requestError)",
        handlers.rstrip(),
        "plugin durable handlers",
    )
    source = _replace_region(
        source,
        "local function run()",
        "task.spawn(function()\n\tlocal ok, runError = pcall(run)",
        """local function waitBoundedBackoff(delaySeconds)
\ttask.wait(delaySeconds)
\treturn math.min(delaySeconds * 2, 8)
end

local function establishInitialConnection()
\tlocal delaySeconds = 0.5
\twhile peer.alive and not peer.shutdown_requested do
\t\tlocal connected, connectError = connect(false)
\t\tif connected then
\t\t\treturn true
\t\tend
\t\tpeer.last_error = sanitizeMessage(connectError)
\t\tdelaySeconds = waitBoundedBackoff(delaySeconds)
\tend
\treturn false
end

local function restoreConnection()
\tlocal delaySeconds = 0.5
\twhile peer.alive and not peer.shutdown_requested do
\t\tlocal reconnected, reconnectError = connect(true)
\t\tif reconnected then
\t\t\tlocal recoveryOk, recoveryError = pcall(function()
\t\t\t\treconcileRecoveryAfterReconnect()
\t\t\tend)
\t\t\tif not recoveryOk then
\t\t\t\tlocal normalized, fatal = normalizedError(
\t\t\t\t\trecoveryError,
\t\t\t\t\t"play_recovery_failed"
\t\t\t\t)
\t\t\t\tpeer.last_error = normalized.message
\t\t\t\tif fatal then
\t\t\t\t\tpeer.alive = false
\t\t\t\tend
\t\t\t\treturn false
\t\t\tend
\t\t\treturn true
\t\tend
\t\tpeer.last_error = sanitizeMessage(reconnectError)

\t\t-- An empty replacement broker cannot know the old Studio ID. A fresh
\t\t-- registration is allowed only when no Play transition is active or
\t\t-- uncertain. The same runtime identity/secret/epoch are retained.
\t\tif peer.active_play == nil then
\t\t\tlocal registered, registrationError = connect(false)
\t\t\tif registered then
\t\t\t\tpeer.response_cache = {}
\t\t\t\tpeer.response_order = {}
\t\t\t\treturn true
\t\t\tend
\t\t\tpeer.last_error = sanitizeMessage(registrationError)
\t\tend
\t\tdelaySeconds = waitBoundedBackoff(delaySeconds)
\tend
\treturn false
end

local function run()
\tif not establishInitialConnection() then
\t\tbestEffortFinalize()
\t\treturn
\tend
\twhile peer.alive and not peer.shutdown_requested do
\t\tassertExpectedDocument()
\t\treconcilePendingPreRunAbort()
\t\tif not peer.alive then
\t\t\tbreak
\t\tend
\t\treconcileNaturalReturn()
\t\tif not peer.alive then
\t\t\tbreak
\t\tend
\t\tlocal ok, result = requestWithRetry("/v2/studios/poll", {
\t\t\tstudio_id = peer.studio_id,
\t\t\tgeneration = peer.generation,
\t\t\tresume_token = peer.resume_token,
\t\t})
\t\tif ok then
\t\t\tif result ~= nil then
\t\t\t\thandleRequest(result)
\t\t\tend
\t\telse
\t\t\tlocal leaseDeadline = peer.last_hub_success_at
\t\t\t\t+ RECONNECT_LEASE_SECONDS
\t\t\twhile peer.alive
\t\t\t\tand not peer.shutdown_requested
\t\t\t\tand os.clock() < leaseDeadline
\t\t\tdo
\t\t\t\ttask.wait(math.max(
\t\t\t\t\t0.05,
\t\t\t\t\tmath.min(0.25, leaseDeadline - os.clock())
\t\t\t\t))
\t\t\tend
\t\t\tif not peer.alive or peer.shutdown_requested then
\t\t\t\tbreak
\t\t\tend
\t\t\tif not restoreConnection() then
\t\t\t\tbreak
\t\t\tend
\t\tend
\tend
\tbestEffortFinalize()
end""",
        "plugin durable connection lifecycle",
    )
    return source.replace(
        '"studio-mcp-v2-standalone-plugin"',
        '"studio-mcp-v2-durable-plugin"',
    )


def render(
    *,
    studio_token: str,
    run_id: str,
    plugin_template: Optional[str] = None,
    server_bridge_template: Optional[str] = None,
) -> str:
    token = validate_studio_token(studio_token)
    target_run_id = validate_run_id(run_id)
    plugin_source = (
        PLUGIN_TEMPLATE.read_text(encoding="utf-8")
        if plugin_template is None
        else plugin_template
    )
    server_source = (
        SERVER_BRIDGE_TEMPLATE.read_text(encoding="utf-8")
        if server_bridge_template is None
        else server_bridge_template
    )

    server_source = _replace_once(
        server_source, "__RUN_ID__", target_run_id
    )
    unresolved_server = sorted(set(PLACEHOLDER.findall(server_source)))
    if unresolved_server:
        raise ValueError(
            "unresolved server bridge placeholders: "
            + ",".join(unresolved_server)
        )

    plugin_source = _replace_once(
        plugin_source, "__STUDIO_BEARER_TOKEN__", token
    )
    plugin_source = _replace_once(
        plugin_source, "__RUN_ID__", target_run_id
    )
    plugin_source = _replace_once(
        plugin_source,
        "__PLAY_SERVER_PLUGIN_BRIDGE_BODY__",
        "\n".join("\t" + line for line in server_source.splitlines()),
    )
    plugin_source = _replace_once(
        plugin_source,
        "__PLAY_SERVER_BRIDGE_SOURCE_LITERAL__",
        _luau_long_string(server_source),
    )
    unresolved_plugin = sorted(set(PLACEHOLDER.findall(plugin_source)))
    if unresolved_plugin:
        raise ValueError(
            "unresolved plugin placeholders: "
            + ",".join(unresolved_plugin)
        )
    return plugin_source


def render_durable(
    studio_token: str,
    run_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Render the durable uncapped plugin without installing or running it."""

    target_base_url = validate_base_url(base_url)
    plugin_source = _durable_plugin_template(
        PLUGIN_TEMPLATE.read_text(encoding="utf-8"),
        DURABLE_HANDLERS_TEMPLATE.read_text(encoding="utf-8"),
        target_base_url,
    )
    server_source = _durable_server_template(
        SERVER_BRIDGE_TEMPLATE.read_text(encoding="utf-8"),
        target_base_url,
    )
    return render(
        studio_token=studio_token,
        run_id=run_id,
        plugin_template=plugin_source,
        server_bridge_template=server_source,
    )


def package_rbxmx(
    plugin_source: str,
    *,
    package_name: str = "StudioMCPv2SideBySidePlugin",
) -> str:
    """Package rendered source as a reversible local-plugin XML model."""

    if not isinstance(plugin_source, str) or not plugin_source:
        raise ValueError("plugin_source must be nonempty text")
    if "]]>" in plugin_source:
        raise ValueError("plugin_source cannot be represented in one CDATA node")
    if (
        not isinstance(package_name, str)
        or not 1 <= len(package_name) <= 64
        or re.fullmatch(r"[A-Za-z0-9_.-]+", package_name) is None
    ):
        raise ValueError("package_name must be bounded filename-safe text")

    folder_ref = "RBX" + uuid.uuid4().hex
    script_ref = "RBX" + uuid.uuid4().hex
    script_guid = "{" + str(uuid.uuid4()) + "}"
    safe_name = escape(package_name)
    return (
        '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" '
        'version="4">\n'
        "\t<External>null</External>\n"
        "\t<External>nil</External>\n"
        f'\t<Item class="Folder" referent="{folder_ref}">\n'
        "\t\t<Properties>\n"
        '\t\t\t<BinaryString name="AttributesSerialize"></BinaryString>\n'
        '\t\t\t<SecurityCapabilities name="Capabilities">0'
        "</SecurityCapabilities>\n"
        '\t\t\t<bool name="DefinesCapabilities">false</bool>\n'
        f'\t\t\t<string name="Name">{safe_name}</string>\n'
        '\t\t\t<int64 name="SourceAssetId">-1</int64>\n'
        '\t\t\t<BinaryString name="Tags"></BinaryString>\n'
        "\t\t</Properties>\n"
        f'\t\t<Item class="Script" referent="{script_ref}">\n'
        "\t\t\t<Properties>\n"
        '\t\t\t\t<ProtectedString name="Source"><![CDATA['
        + plugin_source
        + "]]></ProtectedString>\n"
        '\t\t\t\t<bool name="Disabled">false</bool>\n'
        '\t\t\t\t<Content name="LinkedSource"><null></null></Content>\n'
        '\t\t\t\t<token name="RunContext">0</token>\n'
        f'\t\t\t\t<string name="ScriptGuid">{script_guid}</string>\n'
        '\t\t\t\t<BinaryString name="AttributesSerialize"></BinaryString>\n'
        '\t\t\t\t<SecurityCapabilities name="Capabilities">0'
        "</SecurityCapabilities>\n"
        '\t\t\t\t<bool name="DefinesCapabilities">false</bool>\n'
        '\t\t\t\t<string name="Name">Main</string>\n'
        '\t\t\t\t<int64 name="SourceAssetId">-1</int64>\n'
        '\t\t\t\t<BinaryString name="Tags"></BinaryString>\n'
        "\t\t\t</Properties>\n"
        "\t\t</Item>\n"
        "\t</Item>\n"
        "</roblox>\n"
    )


def render_fresh_bundle() -> RenderedPluginBundle:
    """Create one fresh host/plugin credential set and rendered package source."""

    studio_token = secrets.token_urlsafe(48)
    run_id = secrets.token_hex(16)
    plugin_source = render(
        studio_token=studio_token,
        run_id=run_id,
    )
    return RenderedPluginBundle(
        studio_token=studio_token,
        run_id=run_id,
        plugin_source=plugin_source,
        plugin_package_rbxmx=package_rbxmx(plugin_source),
    )


def render_fresh_durable_bundle(
    *,
    studio_token: Optional[str] = None,
    run_id: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
) -> RenderedPluginBundle:
    """Render a durable package, optionally reusing installer-owned values."""

    target_studio_token = studio_token or secrets.token_urlsafe(48)
    target_run_id = run_id or secrets.token_hex(16)
    plugin_source = render_durable(
        target_studio_token,
        target_run_id,
        base_url=base_url,
    )
    return RenderedPluginBundle(
        studio_token=validate_studio_token(target_studio_token),
        run_id=validate_run_id(target_run_id),
        plugin_source=plugin_source,
        plugin_package_rbxmx=package_rbxmx(
            plugin_source,
            package_name="StudioMCPv2SideBySide",
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fresh-bundle-json",
        action="store_true",
        help=(
            "emit JSON containing a new Studio token, run ID, and plugin source; "
            "treat stdout as secret"
        ),
    )
    parser.add_argument(
        "--fresh-durable-bundle-json",
        action="store_true",
        help=(
            "emit JSON containing a new durable bundle; "
            "treat stdout as secret"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="fixed loopback broker origin for durable rendering",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.fresh_bundle_json and args.fresh_durable_bundle_json:
        raise ValueError("choose only one fresh bundle mode")
    if args.fresh_durable_bundle_json:
        bundle = render_fresh_durable_bundle(base_url=args.base_url)
        print(
            json.dumps(
                {
                    "studio_token": bundle.studio_token,
                    "run_id": bundle.run_id,
                    "plugin_source": bundle.plugin_source,
                    "plugin_package_rbxmx": bundle.plugin_package_rbxmx,
                },
                separators=(",", ":"),
            )
        )
        return
    if args.fresh_bundle_json:
        bundle = render_fresh_bundle()
        print(
            json.dumps(
                {
                    "studio_token": bundle.studio_token,
                    "run_id": bundle.run_id,
                    "plugin_source": bundle.plugin_source,
                    "plugin_package_rbxmx": bundle.plugin_package_rbxmx,
                },
                separators=(",", ":"),
            )
        )
        return

    # Source-only mode uses the already provisioned hub token from the
    # environment. The run ID remains renderer-generated and embedded.
    studio_token = os.environ.get(STUDIO_TOKEN_ENV, "")
    if not studio_token:
        raise ValueError(
            STUDIO_TOKEN_ENV
            + " is required for source-only output; use --fresh-bundle-json "
            "when the renderer should provision the host token"
        )
    print(
        render(
            studio_token=studio_token,
            run_id=secrets.token_hex(16),
        ),
        end="",
    )


if __name__ == "__main__":
    main()
