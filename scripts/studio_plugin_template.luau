--[[
Standalone, side-by-side Studio MCP v2 R&D plugin template.

The renderer replaces only the Studio bearer token, run ID, and fixed audited
Play-server bridge source. Runtime requests have the following closed schemas:

  rnd_get_state      {}
  rnd_marker_create  { delay_ms = integer 0..2000 } -- optional and bounded
  rnd_marker_inspect {}
  rnd_marker_remove  {}
  rnd_play_start     {}
  rnd_play_stop      {}
  rnd_shutdown       {}

There is no active/default Studio selector and no caller-controlled URL, code,
instance path, Play duration, transition identity, or cleanup target.
]]

local InitialRunService = game:GetService("RunService")
local initialEditOk, initialIsEdit = pcall(function()
	return InitialRunService:IsEdit()
end)
local initialRunningOk, initialIsRunning = pcall(function()
	return InitialRunService:IsRunning()
end)
if not InitialRunService:IsStudio()
	or not initialEditOk
	or initialIsEdit ~= true
	or not initialRunningOk
	or initialIsRunning ~= false
then
	-- A local plugin copy must never register from a Play server/client
	-- DataModel. Returning here also avoids touching the document.
	return
end

assert(plugin ~= nil, "Studio MCP v2 must be installed as a Studio plugin")

local CONFIG = table.freeze({
	studio_bearer_token = "__STUDIO_BEARER_TOKEN__",
	run_id = "__RUN_ID__",
})

local PLAY_SERVER_BRIDGE_SOURCE = __PLAY_SERVER_BRIDGE_SOURCE_LITERAL__

local BASE_URL = "http://127.0.0.1:44756"
local SINGLETON_KEY = "StudioMCPv2StandaloneRndPluginSingleton"
local TEMP_SCRIPT_PREFIX = "_StudioMCPv2_PlayServerBridge_v2_"
local TEMP_KIND_ATTRIBUTE = "StudioMCPv2TemporaryKind"
local OWNER_RUN_ATTRIBUTE = "StudioMCPv2RunId"
local OWNER_CLIENT_ATTRIBUTE = "StudioMCPv2ClientInstanceId"
local OWNER_EPOCH_ATTRIBUTE = "StudioMCPv2DocumentEpoch"
local OWNER_TRANSITION_ATTRIBUTE = "StudioMCPv2TransitionNonce"
local OWNER_SESSION_ATTRIBUTE = "StudioMCPv2SessionTag"
local OWNER_MARKER_ATTRIBUTE = "StudioMCPv2MarkerId"

local MAX_HTTP_BODY_BYTES = 65_536
local MAX_HTTP_RESPONSE_BYTES = 65_536
local MAX_ARGS_BYTES = 4_096
local MAX_REQUEST_ATTEMPTS = 4
local MAX_DELAY_MS = 2_000
local RESPONSE_CACHE_LIMIT = 64
local RECONNECT_LEASE_SECONDS = 32
local START_READY_SECONDS = 12
local RUNNER_RETURN_SECONDS = 36
local EDIT_SAMPLE_COUNT = 3
local EDIT_SAMPLE_INTERVAL_SECONDS = 0.1

local STUDIO_PATHS = table.freeze({
	["/v2/studios/connect"] = true,
	["/v2/studios/poll"] = true,
	["/v2/studios/response"] = true,
	["/v2/studios/disconnect"] = true,
	["/v2/studios/play-bridge/prepare"] = true,
	["/v2/studios/play-bridge/abort-pre-attach"] = true,
	["/v2/studios/play-bridge/status"] = true,
	["/v2/studios/play-bridge/request-stop"] = true,
	["/v2/studios/play-bridge/complete"] = true,
})

-- Durable routing has no place-count or place-ID allowlist.

local CAPABILITIES = table.freeze({
	"rnd_get_state",
	"rnd_marker_create",
	"rnd_marker_inspect",
	"rnd_marker_remove",
	"rnd_play_start",
	"rnd_play_stop",
	"rnd_shutdown",
})

local CAPABILITY_SET = table.freeze({
	rnd_get_state = true,
	rnd_marker_create = true,
	rnd_marker_inspect = true,
	rnd_marker_remove = true,
	rnd_play_start = true,
	rnd_play_stop = true,
	rnd_shutdown = true,
})

local REQUEST_KEYS = table.freeze({
	v = true,
	kind = true,
	studio_id = true,
	document_epoch = true,
	generation = true,
	request_id = true,
	operation = true,
	args = true,
	deadline_ms = true,
})

local BRIDGE_RESULT_KEYS = table.freeze({
	v = true,
	studio_id = true,
	client_instance_id = true,
	document_epoch = true,
	transition_generation = true,
	play_request_id = true,
	expected_place_id = true,
	expected_game_id = true,
	transition_nonce = true,
	state = true,
	idempotent = true,
	expires_in_ms = true,
	attached = true,
	bootstrap_burned = true,
	watchdog_armed = true,
	recovery_only = true,
	stop_requested = true,
	stop_acked = true,
	stop_command_id = true,
	stop_source = true,
	watchdog_in_ms = true,
	watchdog_expired_reason = true,
	recovery_reason = true,
	completion_outcome = true,
	completion_id = true,
	end_test_correlation = true,
	bridge_token = true,
})

local END_TEST_RESULT_KEYS = table.freeze({
	v = true,
	kind = true,
	run_id = true,
	studio_id = true,
	client_instance_id = true,
	document_epoch = true,
	transition_generation = true,
	play_request_id = true,
	transition_nonce = true,
	reason = true,
	end_test_correlation = true,
	server_instance_id = true,
})

local END_TEST_REASONS = table.freeze({
	attach_failed = true,
	attach_rejected = true,
	watchdog_ack_failed = true,
	watchdog_ack_rejected = true,
	hard_watchdog_start_failed = true,
	hard_watchdog_attached_unready = true,
	hard_watchdog_natural = true,
	hard_watchdog_stop = true,
	invalid_poll_result = true,
	invalid_stop_command = true,
	invalid_poll_command = true,
	server_stop = true,
})

local RECOVERY_STOP_SOURCES = table.freeze({
	attach_ttl_watchdog = true,
	play_ttl_watchdog = true,
	controller_disconnect = true,
	generation_replaced = true,
	controller = true,
})

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local ServerScriptService = game:GetService("ServerScriptService")
local StudioTestService = game:GetService("StudioTestService")
local Workspace = game:GetService("Workspace")

local EXPECTED_DATA_MODEL = game
local EXPECTED_PLACE_ID = game.PlaceId
local EXPECTED_GAME_ID = game.GameId
local DOCUMENT_POLICY = nil

local function adapterError(code, message, fatal)
	error({
		adapter_error = true,
		code = code,
		message = message,
		fatal = fatal == true,
	}, 0)
end

local function isInteger(value)
	return type(value) == "number"
		and value == value
		and value > -math.huge
		and value < math.huge
		and math.floor(value) == value
end

local function isCanonicalUuid(value)
	if type(value) ~= "string" or #value ~= 36 then
		return false
	end
	if string.sub(value, 9, 9) ~= "-"
		or string.sub(value, 14, 14) ~= "-"
		or string.sub(value, 19, 19) ~= "-"
		or string.sub(value, 24, 24) ~= "-"
	then
		return false
	end
	local compact = string.gsub(value, "-", "")
	return #compact == 32 and string.find(compact, "[^0-9a-f]") == nil
end

local function isRequestId(value)
	return type(value) == "string"
		and #value >= 1
		and #value <= 128
		and string.find(value, "[%c%s]") == nil
end

local function isSecret(value)
	return type(value) == "string"
		and #value >= 32
		and #value <= 256
		and string.find(value, "[%c%s\"'\\]") == nil
end

local function assertExactKeys(value, allowed, label)
	if type(value) ~= "table" then
		adapterError("invalid_" .. label, label .. " must be an object", true)
	end
	for key in pairs(value) do
		if type(key) ~= "string" or allowed[key] ~= true then
			adapterError(
				"invalid_" .. label,
				label .. " contains an unexpected field",
				true
			)
		end
	end
end

if not isInteger(EXPECTED_PLACE_ID)
	or EXPECTED_PLACE_ID < 0
	or not isInteger(EXPECTED_GAME_ID)
	or EXPECTED_GAME_ID < 0
then
	adapterError("invalid_document", "Document identifiers are invalid", true)
end
if not isSecret(CONFIG.studio_bearer_token) then
	adapterError("invalid_config", "The embedded Studio token is invalid", true)
end
if type(CONFIG.run_id) ~= "string"
	or #CONFIG.run_id < 16
	or #CONFIG.run_id > 64
	or string.find(CONFIG.run_id, "[^%w]") ~= nil
then
	adapterError("invalid_config", "The embedded run ID is invalid", true)
end
if type(PLAY_SERVER_BRIDGE_SOURCE) ~= "string"
	or #PLAY_SERVER_BRIDGE_SOURCE < 1_000
	or #PLAY_SERVER_BRIDGE_SOURCE > 65_536
then
	adapterError("invalid_config", "The fixed Play bridge source is invalid", true)
end

local function newUuid()
	local value = string.lower(HttpService:GenerateGUID(false))
	if not isCanonicalUuid(value) then
		adapterError("identity_generation_failed", "GenerateGUID returned invalid data", true)
	end
	return value
end

-- These values are deliberately generated in this plugin runtime and never
-- stored in global plugin settings, so side-by-side Studio windows cannot
-- overwrite or reuse another window's identity.
local CLIENT_INSTANCE_ID = newUuid()
local REGISTRATION_SECRET = string.gsub(newUuid(), "-", "")
	.. string.gsub(newUuid(), "-", "")
local DOCUMENT_EPOCH = newUuid()
local SESSION_TAG = string.sub(
	string.gsub(CLIENT_INSTANCE_ID, "-", ""),
	1,
	12
)
DOCUMENT_POLICY = table.freeze({ session_tag = SESSION_TAG })
-- Validation marker helpers remain unreachable in the durable registry. Their
-- runtime-local names keep the composed source internally consistent.
local MARKER_ID = "StudioMCPv2Marker_" .. SESSION_TAG .. "_" .. CONFIG.run_id
local OWN_CONTAINER = "StudioMCPv2Markers_" .. SESSION_TAG
	.. "_" .. CONFIG.run_id
local PEER_TAG = "none"
local PEER_CONTAINER = "StudioMCPv2Markers_none_" .. CONFIG.run_id

local function assertExpectedDocument()
	if game ~= EXPECTED_DATA_MODEL
		or game.PlaceId ~= EXPECTED_PLACE_ID
		or game.GameId ~= EXPECTED_GAME_ID
	then
		adapterError(
			"document_changed",
			"The plugin runtime is no longer bound to its original document",
			true
		)
	end

end

if type(shared) ~= "table" then
	adapterError("singleton_unavailable", "The shared singleton is unavailable", true)
end
if shared[SINGLETON_KEY] ~= nil then
	adapterError(
		"singleton_conflict",
		"A standalone Studio MCP v2 plugin already owns this DataModel",
		true
	)
end

local peer = {
	alive = true,
	shutdown_requested = false,
	unloading = false,
	studio_id = nil,
	generation = nil,
	resume_token = nil,
	connected = false,
	response_cache = {},
	response_order = {},
	active_play = nil,
	last_hub_success_at = os.clock(),
	last_error = nil,
}
shared[SINGLETON_KEY] = peer

local function sanitizeMessage(value)
	local message
	if type(value) == "table" and value.adapter_error == true then
		message = tostring(value.message or value.code or "adapter error")
	else
		message = tostring(value)
	end
	for _, secret in ipairs({
		CONFIG.studio_bearer_token,
		REGISTRATION_SECRET,
		peer.resume_token or "",
	}) do
		if secret ~= "" then
			message = string.gsub(message, secret, "[redacted]")
		end
	end
	message = string.gsub(message, "[%c]", " ")
	return string.sub(message, 1, 240)
end

local function normalizedError(value, fallbackCode)
	if type(value) == "table" and value.adapter_error == true then
		return {
			code = tostring(value.code or fallbackCode),
			message = sanitizeMessage(value),
		}, value.fatal == true
	end
	return {
		code = fallbackCode,
		message = sanitizeMessage(value),
	}, false
end

local function modeSnapshot()
	local editOk, isEdit = pcall(function()
		return RunService:IsEdit()
	end)
	local runningOk, isRunning = pcall(function()
		return RunService:IsRunning()
	end)
	local testEditOk, testEdit = pcall(function()
		return StudioTestService.EditModeActive
	end)
	local mode = "unknown"
	if editOk and isEdit == true
		and runningOk and isRunning == false
		and testEditOk and testEdit == true
	then
		mode = "edit"
	elseif (runningOk and isRunning == true)
		or (testEditOk and testEdit == false)
	then
		mode = "play"
	end
	return {
		mode = mode,
		is_edit = editOk and isEdit == true or false,
		is_running = runningOk and isRunning == true or false,
		edit_mode_active = testEditOk and testEdit == true or false,
	}
end

local function requireEditMode()
	local mode = modeSnapshot()
	if mode.mode ~= "edit" then
		adapterError(
			"edit_mode_required",
			"Operation requires three-state-confirmable Edit mode",
			false
		)
	end
	return mode
end

local function requestOnce(path, body)
	if STUDIO_PATHS[path] ~= true then
		return false, "blocked Studio endpoint"
	end
	local encodedOk, encoded = pcall(function()
		return HttpService:JSONEncode(body)
	end)
	if not encodedOk
		or type(encoded) ~= "string"
		or #encoded > MAX_HTTP_BODY_BYTES
	then
		return false, "invalid bounded Studio request"
	end
	local callOk, response = pcall(function()
		return HttpService:RequestAsync({
			Url = BASE_URL .. path,
			Method = "POST",
			Headers = {
				["Authorization"] = "Bearer " .. CONFIG.studio_bearer_token,
				["Content-Type"] = "application/json",
			},
			Body = encoded,
			Compress = Enum.HttpCompression.None,
			Timeout = path == "/v2/studios/poll" and 30 or 8,
		})
	end)
	if not callOk
		or type(response) ~= "table"
		or response.Success ~= true
		or type(response.Body) ~= "string"
		or #response.Body > MAX_HTTP_RESPONSE_BYTES
	then
		return false, "local Studio hub request failed"
	end
	local decodedOk, payload = pcall(function()
		return HttpService:JSONDecode(response.Body)
	end)
	if not decodedOk or type(payload) ~= "table" or payload.ok ~= true then
		return false, "local Studio hub rejected the request"
	end
	return true, payload.result
end

local function requestWithRetry(path, body)
	local lastError = "Studio request exhausted retries"
	local attemptLimit = path == "/v2/studios/poll"
		and 1
		or MAX_REQUEST_ATTEMPTS
	for attempt = 1, attemptLimit do
		if not peer.alive then
			return false, "plugin is shutting down"
		end
		local ok, result = requestOnce(path, body)
		if ok then
			if not peer.alive or peer.unloading then
				return false, "plugin is shutting down"
			end
			peer.last_hub_success_at = os.clock()
			return true, result
		end
		lastError = result
		if attempt < attemptLimit then
			task.wait(math.min(0.1 * (2 ^ (attempt - 1)), 0.8))
		end
	end
	return false, lastError
end

local function connectionMetadata()
	return {
		name = game.Name,
		place_id = EXPECTED_PLACE_ID,
		game_id = EXPECTED_GAME_ID,
		mode = modeSnapshot().mode,
		adapter = "studio-mcp-v2-standalone-plugin",
		run_id = CONFIG.run_id,
		session_tag = DOCUMENT_POLICY.session_tag,
		marker_id = MARKER_ID,
		own_container = OWN_CONTAINER,
		peer_container = PEER_CONTAINER,
	}
end

local function validateCapabilityApproval(value)
	if type(value) ~= "table" or #value ~= #CAPABILITIES then
		adapterError("capability_mismatch", "Hub capability approval is invalid", true)
	end
	local seen = {}
	for _, capability in ipairs(value) do
		if type(capability) ~= "string"
			or CAPABILITY_SET[capability] ~= true
			or seen[capability] == true
		then
			adapterError("capability_mismatch", "Hub capability approval is invalid", true)
		end
		seen[capability] = true
	end
end

local function connect(reconnecting)
	local body = {
		client_instance_id = CLIENT_INSTANCE_ID,
		registration_secret = REGISTRATION_SECRET,
		document_epoch = DOCUMENT_EPOCH,
		metadata = connectionMetadata(),
		capabilities = CAPABILITIES,
	}
	if reconnecting then
		body.studio_id = peer.studio_id
		body.resume_token = peer.resume_token
		-- One stable receipt identity is reused by all HTTP retries belonging to
		-- this logical reconnect attempt.
		body.reconnect_id = newUuid()
		local settled = {}
		for _, requestId in ipairs(peer.response_order) do
			table.insert(settled, requestId)
		end
		body.settled_request_ids = settled
	end
	local ok, result = requestWithRetry("/v2/studios/connect", body)
	if not ok then
		return false, result
	end
	if type(result) ~= "table"
		or result.v ~= 2
		or not isCanonicalUuid(result.studio_id)
		or result.document_epoch ~= DOCUMENT_EPOCH
		or not isInteger(result.generation)
		or result.generation < 1
		or not isSecret(result.resume_token)
	then
		return false, "hub returned invalid registration"
	end
	validateCapabilityApproval(result.approved_capabilities)
	if reconnecting
		and (result.studio_id ~= peer.studio_id
			or result.generation <= peer.generation)
	then
		return false, "hub changed reconnect identity"
	end
	peer.studio_id = result.studio_id
	peer.generation = result.generation
	peer.resume_token = result.resume_token
	peer.connected = true
	if reconnecting then
		peer.response_cache = {}
		peer.response_order = {}
	end
	return true
end

local function applyMarkerOwnership(instance)
	instance:SetAttribute(OWNER_RUN_ATTRIBUTE, CONFIG.run_id)
	instance:SetAttribute(OWNER_SESSION_ATTRIBUTE, DOCUMENT_POLICY.session_tag)
	instance:SetAttribute(OWNER_MARKER_ATTRIBUTE, MARKER_ID)
	instance:SetAttribute(OWNER_EPOCH_ATTRIBUTE, DOCUMENT_EPOCH)
end

local function markerOwned(instance)
	return instance:GetAttribute(OWNER_RUN_ATTRIBUTE) == CONFIG.run_id
		and instance:GetAttribute(OWNER_SESSION_ATTRIBUTE)
			== DOCUMENT_POLICY.session_tag
		and instance:GetAttribute(OWNER_MARKER_ATTRIBUTE) == MARKER_ID
		and instance:GetAttribute(OWNER_EPOCH_ATTRIBUTE) == DOCUMENT_EPOCH
end

local function inspectOwnMarker()
	local container = Workspace:FindFirstChild(OWN_CONTAINER)
	if container == nil then
		return { present = false, container_present = false, marker_id = MARKER_ID }
	end
	if not container:IsA("Folder") or not markerOwned(container) then
		adapterError("marker_ownership_conflict", "Marker container collision", false)
	end
	local children = container:GetChildren()
	if #children == 0 then
		return { present = false, container_present = true, marker_id = MARKER_ID }
	end
	if #children ~= 1 then
		adapterError("marker_shape_conflict", "Marker container shape changed", false)
	end
	local marker = children[1]
	if not marker:IsA("Part")
		or marker.Name ~= MARKER_ID
		or not markerOwned(marker)
	then
		adapterError("marker_ownership_conflict", "Marker identity changed", false)
	end
	return {
		present = true,
		container_present = true,
		marker_id = MARKER_ID,
		container_name = OWN_CONTAINER,
	}
end

local function createMarker()
	requireEditMode()
	local current = inspectOwnMarker()
	if current.present then
		current.created = false
		current.idempotent = true
		return current
	end
	local container = Workspace:FindFirstChild(OWN_CONTAINER)
	local newContainer = nil
	if container == nil then
		newContainer = Instance.new("Folder")
		newContainer.Name = OWN_CONTAINER
		applyMarkerOwnership(newContainer)
		container = newContainer
	end
	local marker = Instance.new("Part")
	marker.Name = MARKER_ID
	marker.Anchored = true
	marker.Locked = true
	marker.CanCollide = false
	marker.CanTouch = false
	marker.CanQuery = false
	marker.CastShadow = false
	marker.Transparency = 1
	marker.Size = Vector3.new(1, 1, 1)
	marker.CFrame = CFrame.new(0, -10_000, 0)
	applyMarkerOwnership(marker)
	marker.Parent = container
	if newContainer ~= nil then
		newContainer.Parent = Workspace
	end
	local result = inspectOwnMarker()
	result.created = true
	result.idempotent = false
	return result
end

local function removeMarker()
	requireEditMode()
	local container = Workspace:FindFirstChild(OWN_CONTAINER)
	if container == nil then
		return { removed = false, already_absent = true, marker_id = MARKER_ID }
	end
	if not container:IsA("Folder") or not markerOwned(container) then
		adapterError("marker_ownership_conflict", "Marker container collision", false)
	end
	local children = container:GetChildren()
	if #children > 1 then
		adapterError("marker_shape_conflict", "Marker container shape changed", false)
	end
	if #children == 1 then
		local marker = children[1]
		if not marker:IsA("Part")
			or marker.Name ~= MARKER_ID
			or not markerOwned(marker)
		then
			adapterError("marker_ownership_conflict", "Marker identity changed", false)
		end
		marker:Destroy()
	end
	if #container:GetChildren() ~= 0 then
		adapterError("marker_cleanup_conflict", "Marker cleanup was not exact", false)
	end
	container:Destroy()
	return {
		removed = #children == 1,
		already_absent = #children == 0,
		marker_id = MARKER_ID,
	}
end

local function inspectMarkers()
	requireEditMode()
	local own = inspectOwnMarker()
	local peerContainer = Workspace:FindFirstChild(PEER_CONTAINER)
	return {
		own_marker_present = own.present == true,
		peer_marker_present = peerContainer ~= nil,
		own = own,
		peer = {
			container_name = PEER_CONTAINER,
			container_present = peerContainer ~= nil,
		},
	}
end

local function transitionBody(transition)
	return {
		studio_id = peer.studio_id,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		resume_token = peer.resume_token,
		transition_generation = transition.transition_generation,
		play_request_id = transition.play_request_id,
		transition_nonce = transition.transition_nonce,
	}
end

local function validateBridgeSnapshot(result, playRequestId, transition)
	assertExactKeys(result, BRIDGE_RESULT_KEYS, "bridge_result")
	if result.v ~= 2
		or result.studio_id ~= peer.studio_id
		or result.client_instance_id ~= CLIENT_INSTANCE_ID
		or result.document_epoch ~= DOCUMENT_EPOCH
		or result.play_request_id ~= playRequestId
		or result.expected_place_id ~= EXPECTED_PLACE_ID
		or result.expected_game_id ~= EXPECTED_GAME_ID
		or not isInteger(result.transition_generation)
		or result.transition_generation < 1
		or not isCanonicalUuid(result.transition_nonce)
	then
		adapterError("bridge_context_mismatch", "Play bridge context is invalid", true)
	end
	if transition ~= nil
		and (result.transition_generation ~= transition.transition_generation
			or result.transition_nonce ~= transition.transition_nonce)
	then
		adapterError(
			"bridge_transition_mismatch",
			"Play bridge result belongs to a different transition",
			true
		)
	end
	return result
end

local function temporaryScriptOwned(target, transition)
	return target == transition.script
		and target.Name == transition.script_name
		and target:IsA("Script")
		and target:GetAttribute(TEMP_KIND_ATTRIBUTE) == "play_server_bridge_v2"
		and target:GetAttribute(OWNER_RUN_ATTRIBUTE) == CONFIG.run_id
		and target:GetAttribute(OWNER_CLIENT_ATTRIBUTE) == CLIENT_INSTANCE_ID
		and target:GetAttribute(OWNER_EPOCH_ATTRIBUTE) == DOCUMENT_EPOCH
		and target:GetAttribute(OWNER_TRANSITION_ATTRIBUTE)
			== transition.transition_nonce
		and target:GetAttribute(OWNER_SESSION_ATTRIBUTE)
			== DOCUMENT_POLICY.session_tag
end

local function createTemporaryScript(transition)
	if ServerScriptService:FindFirstChild(transition.script_name) ~= nil then
		adapterError(
			"play_script_collision",
			"The exact temporary Play bridge name is already occupied",
			false
		)
	end
	local bridge = Instance.new("Script")
	bridge.Name = transition.script_name
	bridge.Enabled = true
	bridge.RunContext = Enum.RunContext.Server
	bridge.Source = PLAY_SERVER_BRIDGE_SOURCE
	bridge:SetAttribute(TEMP_KIND_ATTRIBUTE, "play_server_bridge_v2")
	bridge:SetAttribute(OWNER_RUN_ATTRIBUTE, CONFIG.run_id)
	bridge:SetAttribute(OWNER_CLIENT_ATTRIBUTE, CLIENT_INSTANCE_ID)
	bridge:SetAttribute(OWNER_EPOCH_ATTRIBUTE, DOCUMENT_EPOCH)
	bridge:SetAttribute(OWNER_TRANSITION_ATTRIBUTE, transition.transition_nonce)
	bridge:SetAttribute(
		OWNER_SESSION_ATTRIBUTE,
		DOCUMENT_POLICY.session_tag
	)
	transition.script = bridge
	bridge.Parent = ServerScriptService
	if ServerScriptService:FindFirstChild(transition.script_name) ~= bridge
		or not temporaryScriptOwned(bridge, transition)
	then
		adapterError(
			"play_script_creation_failed",
			"Temporary Play bridge ownership could not be proven",
			false
		)
	end
end

local function cleanupTemporaryScript(transition)
	local exact = ServerScriptService:FindFirstChild(transition.script_name)
	if exact == nil then
		if transition.script ~= nil and transition.script.Parent ~= nil then
			adapterError(
				"play_script_cleanup_conflict",
				"Owned temporary Script moved to an unexpected parent",
				true
			)
		end
		transition.script = nil
		return true
	end
	if transition.script == nil or not temporaryScriptOwned(exact, transition) then
		adapterError(
			"play_script_cleanup_conflict",
			"Refusing to destroy an unowned temporary Script collision",
			true
		)
	end
	exact:Destroy()
	transition.script = nil
	if ServerScriptService:FindFirstChild(transition.script_name) ~= nil then
		adapterError(
			"play_script_cleanup_conflict",
			"Exact temporary Script cleanup did not complete",
			true
		)
	end
	return true
end

local function cleanupOwnedTemporaryScriptForAbort(transition)
	if transition.script == nil then
		-- Creation can fail on an unowned nonce-name collision before this
		-- runtime allocates anything. The collision is never destroyed; the
		-- truthful proof here is only that this transition created no Script.
		return true
	end
	return cleanupTemporaryScript(transition)
end

local function abortPreparedTransition(transition)
	if transition.runner_started == true or transition.runner_finished == true then
		adapterError(
			"pre_attach_abort_forbidden",
			"Refusing to abort after the Play runner may have started",
			true
		)
	end
	local scriptCleaned = cleanupOwnedTemporaryScriptForAbort(transition)
	if transition.abort_id == nil then
		transition.abort_id = newUuid()
	end
	local body = transitionBody(transition)
	body.abort_id = transition.abort_id
	body.runner_started = false
	body.script_cleaned = scriptCleaned
	local ok, result = requestWithRetry(
		"/v2/studios/play-bridge/abort-pre-attach",
		body
	)
	if not ok then
		adapterError("pre_attach_abort_failed", tostring(result), false)
	end
	local snapshot = validateBridgeSnapshot(
		result,
		transition.play_request_id,
		transition
	)
	if snapshot.state ~= "completed"
		or snapshot.completion_outcome ~= "pre_attach_aborted"
		or snapshot.completion_id ~= transition.abort_id
	then
		adapterError(
			"pre_attach_abort_rejected",
			"Hub did not retire the untouched PREPARED transition",
			true
		)
	end
	transition.bridge_token = nil
	peer.active_play = nil
	return true
end

local function waitForRunner(transition, seconds)
	local deadline = os.clock() + seconds
	while not transition.runner_finished and os.clock() < deadline do
		task.wait(0.05)
	end
	return transition.runner_finished == true
end

local function waitForStableEdit()
	for _ = 1, EDIT_SAMPLE_COUNT do
		assertExpectedDocument()
		if modeSnapshot().mode ~= "edit" then
			adapterError(
				"edit_confirmation_failed",
				"Edit mode was not stable after ExecutePlayModeAsync returned",
				true
			)
		end
		task.wait(EDIT_SAMPLE_INTERVAL_SECONDS)
	end
	return EDIT_SAMPLE_COUNT
end

local function validateEndTestResult(transition)
	local result = transition.runner_result
	if transition.runner_ok ~= true
		or type(result) ~= "table"
	then
		adapterError(
			"uncorrelated_play_return",
			"ExecutePlayModeAsync did not return a bridge result",
			true
		)
	end
	assertExactKeys(result, END_TEST_RESULT_KEYS, "end_test_result")
	if result.v ~= 2
		or result.kind ~= "studio_mcp_v2_end_test"
		or result.run_id ~= CONFIG.run_id
		or result.studio_id ~= peer.studio_id
		or result.client_instance_id ~= CLIENT_INSTANCE_ID
		or result.document_epoch ~= DOCUMENT_EPOCH
		or result.transition_generation ~= transition.transition_generation
		or result.play_request_id ~= transition.play_request_id
		or result.transition_nonce ~= transition.transition_nonce
		or not isRequestId(result.end_test_correlation)
		or not isCanonicalUuid(result.server_instance_id)
		or END_TEST_REASONS[result.reason] ~= true
	then
		adapterError(
			"uncorrelated_play_return",
			"ExecutePlayModeAsync did not return the exact bridge proof",
			true
		)
	end
	return result
end

local function completeTransition(transition, outcome, correlation)
	if transition.runner_finished ~= true then
		adapterError("runner_still_active", "Play runner has not returned", true)
	end
	if transition.completion_outcome ~= nil
		and (transition.completion_outcome ~= outcome
			or transition.completion_correlation ~= correlation)
	then
		adapterError(
			"completion_proof_changed",
			"Refusing to change the bound Play completion proof",
			true
		)
	end
	local scriptCleaned = cleanupTemporaryScript(transition)
	local confirmations = waitForStableEdit()
	if transition.completion_edit_confirmations ~= nil
		and transition.completion_edit_confirmations ~= confirmations
	then
		adapterError(
			"completion_proof_changed",
			"Stable Edit confirmation count changed across completion retry",
			true
		)
	end
	local body = transitionBody(transition)
	if transition.completion_id == nil then
		transition.completion_id = newUuid()
	end
	transition.completion_outcome = outcome
	transition.completion_correlation = correlation
	transition.completion_edit_confirmations = confirmations
	body.completion_id = transition.completion_id
	body.outcome = transition.completion_outcome
	body.end_test_correlation = transition.completion_correlation
	body.runner_returned = true
	body.edit_confirmations = transition.completion_edit_confirmations
	body.script_cleaned = scriptCleaned
	local ok, result = requestWithRetry(
		"/v2/studios/play-bridge/complete",
		body
	)
	if not ok then
		-- The transition remains recovery-gated and the same completion_id is
		-- reused by later reconciliation, so a lost HTTP reply is retryable.
		adapterError("play_complete_failed", tostring(result), false)
	end
	validateBridgeSnapshot(result, transition.play_request_id, transition)
	if result.state ~= "completed" then
		adapterError("play_complete_rejected", "Hub did not complete Play", true)
	end
	if result.completion_id ~= transition.completion_id
		or result.completion_outcome ~= transition.completion_outcome
		or result.end_test_correlation ~= transition.completion_correlation
	then
		adapterError(
			"play_complete_receipt_mismatch",
			"Hub returned a different Play completion receipt",
			true
		)
	end
	peer.active_play = nil
end

local function finishReturnedTransition(transition)
	local returned = validateEndTestResult(transition)
	local correlation = returned.end_test_correlation
	local statusOk, statusResult = requestWithRetry(
		"/v2/studios/play-bridge/status",
		transitionBody(transition)
	)
	if not statusOk then
		adapterError(
			"play_status_unavailable",
			"Cannot classify Play completion without current host state",
			true
		)
	end
	local status = validateBridgeSnapshot(
		statusResult,
		transition.play_request_id,
		transition
	)
	if status.state == "completed" then
		if transition.completion_id == nil
			or transition.completion_outcome == nil
			or transition.completion_correlation == nil
			or status.completion_id ~= transition.completion_id
			or status.completion_outcome ~= transition.completion_outcome
			or status.end_test_correlation
				~= transition.completion_correlation
		then
			adapterError(
				"play_complete_receipt_mismatch",
				"Completed host state does not match the local proof",
				true
			)
		end
		completeTransition(
			transition,
			transition.completion_outcome,
			transition.completion_correlation
		)
		return transition.completion_outcome
	end
	if correlation == transition.play_request_id then
		if returned.reason ~= "attach_failed"
			and returned.reason ~= "attach_rejected"
			and returned.reason ~= "watchdog_ack_failed"
			and returned.reason ~= "watchdog_ack_rejected"
			and returned.reason ~= "hard_watchdog_start_failed"
		then
			adapterError(
				"start_failure_reason_mismatch",
				"EndTest reason is not a pre-attach start failure",
				true
			)
		end
		if status.state ~= "prepared"
			or status.attached ~= false
			or status.stop_requested ~= false
		then
			adapterError(
				"start_failure_state_mismatch",
				"Host is no longer PREPARED; refusing start_failed proof",
				true
			)
		end
		completeTransition(
			transition,
			"start_failed_edit_confirmed",
			correlation
		)
		return "start_failed_edit_confirmed"
	elseif correlation == transition.transition_nonce then
		if returned.reason ~= "hard_watchdog_natural" then
			adapterError(
				"natural_stop_reason_mismatch",
				"EndTest reason is not a natural hard-watchdog return",
				true
			)
		end
		if status.state == "attached"
			and status.attached == true
			and status.watchdog_armed == true
			and status.stop_requested == false
		then
			completeTransition(
				transition,
				"natural_stop_edit_confirmed",
				correlation
			)
			return "natural_stop_edit_confirmed"
		end
		if status.state == "stop_requested"
			and status.recovery_only == true
			and status.watchdog_armed == true
			and status.stop_requested == true
			and status.stop_acked == false
			and (status.stop_source == "controller_disconnect"
				or status.stop_source == "generation_replaced")
		then
			completeTransition(
				transition,
				"recovery_natural_stop_edit_confirmed",
				correlation
			)
			return "recovery_natural_stop_edit_confirmed"
		end
		adapterError(
			"natural_stop_state_mismatch",
			"Host state does not permit natural-stop completion",
			true
		)
	elseif transition.stop_command_id ~= nil
		and correlation == transition.stop_command_id
	then
		if returned.reason ~= "server_stop"
			and returned.reason ~= "hard_watchdog_stop"
		then
			adapterError(
				"stop_reason_mismatch",
				"EndTest reason is not correlated server Stop handling",
				true
			)
		end
		if status.state ~= "stop_acked"
			or status.watchdog_armed ~= true
			or status.stop_acked ~= true
			or status.stop_command_id ~= transition.stop_command_id
		then
			adapterError(
				"stop_state_mismatch",
				"Host lacks the correlated stop_received proof",
				true
			)
		end
		completeTransition(
			transition,
			"stopped_edit_confirmed",
			correlation
		)
		return "stopped_edit_confirmed"
	end
	adapterError(
		"uncorrelated_play_return",
		"EndTest correlation does not belong to this transition",
		true
	)
end

local function startPlay(requestId)
	requireEditMode()
	if peer.active_play ~= nil then
		adapterError("play_transition_conflict", "A Play transition is active", false)
	end
	local prepareBody = {
		studio_id = peer.studio_id,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		resume_token = peer.resume_token,
		play_request_id = requestId,
	}
	local preparedOk, preparedResult = requestWithRetry(
		"/v2/studios/play-bridge/prepare",
		prepareBody
	)
	if not preparedOk then
		adapterError("play_prepare_failed", tostring(preparedResult), false)
	end
	local prepared = validateBridgeSnapshot(preparedResult, requestId)
	if prepared.state ~= "prepared"
		or prepared.attached ~= false
		or not isSecret(prepared.bridge_token)
	then
		adapterError("play_prepare_rejected", "Hub returned unsafe prepare state", true)
	end
	local transition = {
		transition_generation = prepared.transition_generation,
		play_request_id = requestId,
		transition_nonce = prepared.transition_nonce,
		bridge_token = prepared.bridge_token,
		script = nil,
		runner_finished = false,
		runner_ok = nil,
		runner_result = nil,
		server_ready = false,
		stop_command_id = nil,
		stop_request_id = nil,
		completion_id = nil,
		completion_outcome = nil,
		completion_correlation = nil,
		completion_edit_confirmations = nil,
		abort_id = nil,
		abort_requested = false,
		runner_started = false,
	}
	transition.script_name = TEMP_SCRIPT_PREFIX
		.. string.gsub(transition.transition_nonce, "-", "")
	peer.active_play = transition
	local createdOk, createdError = pcall(function()
		createTemporaryScript(transition)
	end)
	if not createdOk then
		transition.abort_requested = true
		local abortedOk, abortError = pcall(function()
			abortPreparedTransition(transition)
		end)
		if not abortedOk then
			peer.last_error = sanitizeMessage(abortError)
		end
		error(createdError, 0)
	end

	local bootstrap = {
		v = 2,
		kind = "studio_mcp_v2_play_bootstrap",
		run_id = CONFIG.run_id,
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		transition_generation = transition.transition_generation,
		play_request_id = transition.play_request_id,
		expected_place_id = EXPECTED_PLACE_ID,
		expected_game_id = EXPECTED_GAME_ID,
		transition_nonce = transition.transition_nonce,
		bridge_token = transition.bridge_token,
		activation_deadline_unix = os.time() + 10,
		temporary_script_name = transition.script_name,
	}
	local spawnedOk, spawnedError = pcall(function()
		task.spawn(function()
			transition.runner_started = true
			local ok, result = pcall(function()
				return StudioTestService:ExecutePlayModeAsync(bootstrap)
			end)
			transition.runner_ok = ok
			transition.runner_result = result
			transition.runner_finished = true
		end)
	end)
	if not spawnedOk then
		transition.abort_requested = true
		local abortedOk, abortError = pcall(function()
			abortPreparedTransition(transition)
		end)
		if not abortedOk then
			peer.last_error = sanitizeMessage(abortError)
		end
		error(spawnedError, 0)
	end
	transition.bridge_token = nil

	local readyDeadline = os.clock() + START_READY_SECONDS
	while os.clock() < readyDeadline do
		if transition.runner_finished then
			local outcome = finishReturnedTransition(transition)
			adapterError(
				"play_ended_before_ready",
				"Play ended before server-ready acknowledgement: " .. outcome,
				false
			)
		end
		local statusBody = transitionBody(transition)
		local statusOk, statusResult = requestWithRetry(
			"/v2/studios/play-bridge/status",
			statusBody
		)
		if statusOk then
				local status = validateBridgeSnapshot(
					statusResult,
					transition.play_request_id,
					transition
				)
			if status.attached == true and status.watchdog_armed == true then
				transition.server_ready = true
				return {
					started = true,
					server_ready = true,
					transition_nonce = transition.transition_nonce,
					mode = modeSnapshot(),
				}
			end
		end
		task.wait(0.1)
	end
	if waitForRunner(transition, RUNNER_RETURN_SECONDS) then
		local outcome = finishReturnedTransition(transition)
		adapterError(
			"play_start_timeout",
			"Server-ready timed out and Play ended safely: " .. outcome,
			false
		)
	end
	adapterError(
		"play_runner_uncertain",
		"Play runner did not return after its hard server watchdog",
		true
	)
end

local function stopPlay(requestId)
	local transition = peer.active_play
	if transition == nil then
		local mode = modeSnapshot()
		if mode.mode == "edit" then
			return { stopped = false, already_stopped = true, mode = mode }
		end
		adapterError("play_not_owned", "No owned Play transition exists", false)
	end
	if transition.server_ready ~= true then
		adapterError("play_not_ready", "Play server is not ready for Stop", false)
	end
	if transition.runner_finished then
		local outcome = finishReturnedTransition(transition)
		return {
			stopped = outcome == "stopped_edit_confirmed",
			already_stopped = outcome ~= "stopped_edit_confirmed",
			outcome = outcome,
			mode = modeSnapshot(),
		}
	end
	local body = transitionBody(transition)
	transition.stop_request_id = requestId
	body.stop_id = requestId
	local requestedOk, requestedResult = requestWithRetry(
		"/v2/studios/play-bridge/request-stop",
		body
	)
	if not requestedOk then
		adapterError("play_stop_request_failed", tostring(requestedResult), false)
	end
	local requested = validateBridgeSnapshot(
		requestedResult,
		transition.play_request_id,
		transition
	)
	if requested.stop_requested ~= true
		or not isCanonicalUuid(requested.stop_command_id)
	then
		adapterError("play_stop_rejected", "Hub did not bind an exact Stop", true)
	end
	transition.stop_command_id = requested.stop_command_id
	if not waitForRunner(transition, RUNNER_RETURN_SECONDS) then
		adapterError(
			"play_stop_timeout",
			"Correlated server EndTest did not return to the plugin runner",
			true
		)
	end
	local returned = validateEndTestResult(transition)
	if returned.end_test_correlation ~= transition.stop_command_id then
		adapterError(
			"play_stop_uncorrelated",
			"ExecutePlayModeAsync returned without the Stop correlation",
			true
		)
	end
	completeTransition(
		transition,
		"stopped_edit_confirmed",
		transition.stop_command_id
	)
	return {
		stopped = true,
		already_stopped = false,
		stop_command_id = transition.stop_command_id,
		mode = modeSnapshot(),
	}
end

local function getState()
	local transition = peer.active_play
	return {
		adapter = "studio-mcp-v2-standalone-plugin",
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		run_id = CONFIG.run_id,
		session_tag = DOCUMENT_POLICY.session_tag,
		place_id = EXPECTED_PLACE_ID,
		game_id = EXPECTED_GAME_ID,
		mode = modeSnapshot().mode,
		is_edit = modeSnapshot().mode == "edit",
		play = transition == nil and {
			active = false,
		} or {
			active = true,
			server_ready = transition.server_ready,
			runner_finished = transition.runner_finished,
			transition_nonce = transition.transition_nonce,
		},
		marker = modeSnapshot().mode == "edit" and inspectOwnMarker() or {
			present = false,
			available = false,
		},
	}
end

local function validateArgs(operation, args, deadlineMs)
	if type(args) ~= "table" then
		adapterError("invalid_args", "args must be an object", false)
	end
	for key, value in pairs(args) do
		if operation ~= "rnd_marker_create" or key ~= "delay_ms" then
			adapterError("invalid_args", "Operation args are outside the closed schema", false)
		end
		if not isInteger(value) or value < 0 or value > MAX_DELAY_MS then
			adapterError("invalid_args", "delay_ms must be an integer from 0 to 2000", false)
		end
	end
	if operation ~= "rnd_marker_create" and next(args) ~= nil then
		adapterError("invalid_args", "Operation accepts no args", false)
	end
	if operation == "rnd_marker_create"
		and args.delay_ms ~= nil
		and args.delay_ms >= deadlineMs
	then
		adapterError("invalid_args", "delay_ms exceeds request deadline", false)
	end
	local encodedOk, encoded = pcall(function()
		return HttpService:JSONEncode(args)
	end)
	if not encodedOk or type(encoded) ~= "string" or #encoded > MAX_ARGS_BYTES then
		adapterError("invalid_args", "args JSON is invalid or too large", false)
	end
end

local function validateRequest(request)
	assertExactKeys(request, REQUEST_KEYS, "request")
	if request.v ~= 2
		or request.kind ~= "request"
		or request.studio_id ~= peer.studio_id
		or request.document_epoch ~= DOCUMENT_EPOCH
		or request.generation ~= peer.generation
		or not isRequestId(request.request_id)
		or CAPABILITY_SET[request.operation] ~= true
		or not isInteger(request.deadline_ms)
		or request.deadline_ms < 1
		or request.deadline_ms > 120_000
	then
		adapterError("invalid_request", "Request routing envelope is invalid", true)
	end
	validateArgs(request.operation, request.args, request.deadline_ms)
	local delay = request.args.delay_ms
	return table.concat({
		request.operation,
		tostring(request.deadline_ms),
		delay == nil and "delay:nil" or ("delay:number:" .. tostring(delay)),
	}, "\0")
end

local function dispatch(request)
	assertExpectedDocument()
	if request.operation == "rnd_get_state" then
		return getState()
	elseif request.operation == "rnd_marker_create" then
		local delayMs = request.args.delay_ms or 0
		requireEditMode()
		if delayMs > 0 then
			task.wait(delayMs / 1_000)
		end
		assertExpectedDocument()
		return createMarker()
	elseif request.operation == "rnd_marker_inspect" then
		return inspectMarkers()
	elseif request.operation == "rnd_marker_remove" then
		return removeMarker()
	elseif request.operation == "rnd_play_start" then
		return startPlay(request.request_id)
	elseif request.operation == "rnd_play_stop" then
		return stopPlay(request.request_id)
	elseif request.operation == "rnd_shutdown" then
		if peer.active_play ~= nil then
			adapterError(
				"play_transition_active",
				"Shutdown is rejected until owned Play has ended",
				false
			)
		end
		peer.shutdown_requested = true
		return { shutdown = true, run_id = CONFIG.run_id }
	end
	adapterError("unsupported_operation", "Operation is outside the allowlist", false)
end

local function cacheResponse(request, signature, success, result, requestError)
	local record = {
		request_id = request.request_id,
		operation = request.operation,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		signature = signature,
		success = success,
		result = result,
		error = requestError,
	}
	peer.response_cache[request.request_id] = record
	table.insert(peer.response_order, request.request_id)
	while #peer.response_order > RESPONSE_CACHE_LIMIT do
		local evicted = table.remove(peer.response_order, 1)
		peer.response_cache[evicted] = nil
	end
	return record
end

local function deliverResponse(record)
	local body = {
		studio_id = peer.studio_id,
		generation = peer.generation,
		resume_token = peer.resume_token,
		request_id = record.request_id,
		success = record.success,
	}
	if record.success then
		body.result = record.result
	else
		body.error = record.error
	end
	local ok, result = requestWithRetry("/v2/studios/response", body)
	return ok and type(result) == "table" and result.accepted == true
end

local function handleRequest(request)
	local validOk, signatureOrError = pcall(function()
		return validateRequest(request)
	end)
	if not validOk then
		local normalized, fatal = normalizedError(
			signatureOrError,
			"invalid_request"
		)
		if fatal then
			peer.alive = false
		end
		return false
	end
	local signature = signatureOrError
	local cached = peer.response_cache[request.request_id]
	if cached ~= nil then
		if cached.operation ~= request.operation
			or cached.document_epoch ~= DOCUMENT_EPOCH
			or cached.generation ~= peer.generation
			or cached.signature ~= signature
		then
			adapterError(
				"request_id_collision",
				"Duplicate request changed its validated envelope",
				true
			)
		end
		return deliverResponse(cached)
	end
	local dispatchOk, dispatchResult = pcall(function()
		return dispatch(request)
	end)
	local record
	if dispatchOk then
		record = cacheResponse(
			request,
			signature,
			true,
			dispatchResult,
			nil
		)
	else
		local normalized, fatal = normalizedError(
			dispatchResult,
			"operation_failed"
		)
		record = cacheResponse(
			request,
			signature,
			false,
			nil,
			normalized
		)
		if fatal then
			peer.alive = false
		end
	end
	return deliverResponse(record)
end

local function reconcilePendingPreRunAbort()
	local transition = peer.active_play
	if transition == nil
		or transition.abort_requested ~= true
		or transition.runner_started == true
	then
		return
	end
	local ok, result = pcall(function()
		return abortPreparedTransition(transition)
	end)
	if not ok then
		local normalized, fatal = normalizedError(
			result,
			"pre_attach_abort_reconcile_failed"
		)
		peer.last_error = normalized.message
		if fatal then
			peer.alive = false
		end
	end
end

local function reconcileNaturalReturn()
	local transition = peer.active_play
	if transition ~= nil and transition.runner_finished == true then
		local ok, result = pcall(function()
			return finishReturnedTransition(transition)
		end)
		if not ok then
			local normalized, fatal = normalizedError(
				result,
				"play_reconcile_failed"
			)
			peer.last_error = normalized.message
			if fatal then
				peer.alive = false
			end
		end
	end
end

local function reconcileRecoveryAfterReconnect()
	local transition = peer.active_play
	if transition == nil then
		return
	end
	local ok, result = requestWithRetry(
		"/v2/studios/play-bridge/status",
		transitionBody(transition)
	)
	if not ok then
		adapterError(
			"play_recovery_status_failed",
			"Cannot reconcile the Play transition after reconnect",
			true
		)
	end
	local status = validateBridgeSnapshot(
		result,
		transition.play_request_id,
		transition
	)
	if transition.abort_requested == true
		and transition.runner_started ~= true
	then
		if status.state == "completed"
			and status.completion_outcome == "pre_attach_aborted"
			and status.completion_id == transition.abort_id
		then
			-- Re-send the identical abort proof using the new controller
			-- generation. The host accepts it only as an exact idempotent replay.
			abortPreparedTransition(transition)
			return
		end
		-- An uncommitted PREPARED abort becomes recovery-fenced on reconnect
		-- because the host can no longer prove no runner started.
		adapterError(
			"pre_attach_abort_reconnect_fenced",
			"Pre-run cleanup lost its original authenticated generation",
			true
		)
	end
	if status.state == "completed" then
		if transition.runner_finished ~= true
			or transition.completion_id == nil
			or transition.completion_outcome == nil
			or transition.completion_correlation == nil
			or status.completion_id ~= transition.completion_id
			or status.completion_outcome ~= transition.completion_outcome
			or status.end_test_correlation
				~= transition.completion_correlation
		then
			adapterError(
				"play_complete_receipt_mismatch",
				"Reconnect completion receipt does not match local proof",
				true
			)
		end
		completeTransition(
			transition,
			transition.completion_outcome,
			transition.completion_correlation
		)
		return
	end
	if status.recovery_only ~= true
		or status.stop_requested ~= true
		or not isCanonicalUuid(status.stop_command_id)
		or RECOVERY_STOP_SOURCES[status.stop_source] ~= true
	then
		adapterError(
			"play_recovery_state_mismatch",
			"Reconnect did not return an exact recovery Stop context",
			true
		)
	end
	if status.stop_source == "controller"
		and transition.stop_command_id == nil
		and transition.stop_request_id == nil
	then
		adapterError(
			"play_recovery_controller_mismatch",
			"An unowned controller Stop appeared during reconnect",
			true
		)
	end
	if transition.stop_command_id ~= nil
		and transition.stop_command_id ~= status.stop_command_id
	then
		adapterError(
			"play_recovery_stop_mismatch",
			"Reconnect changed the bound Stop command",
			true
		)
	end
	transition.stop_command_id = status.stop_command_id
	transition.recovery_stop_source = status.stop_source
	if status.watchdog_armed == true then
		transition.server_ready = true
	end
	if transition.runner_finished == true then
		finishReturnedTransition(transition)
	end
end

local function bestEffortFinalize()
	local transition = peer.active_play
	if transition ~= nil
		and transition.runner_finished ~= true
		and peer.alive
	then
		waitForRunner(transition, RUNNER_RETURN_SECONDS)
	end
	if transition ~= nil and transition.runner_finished == true and peer.alive then
		local reconciled = pcall(function()
			finishReturnedTransition(transition)
		end)
		if not reconciled then
			peer.last_error = "Play transition could not be reconciled on shutdown"
		end
	end
	if peer.active_play ~= nil then
		-- Never claim disconnect/cleanup while an ExecutePlayModeAsync runner
		-- remains unsafe. The server watchdog retains ownership of EndTest.
		peer.alive = false
		return
	end
	if modeSnapshot().mode == "edit" then
		pcall(removeMarker)
	end
	if peer.connected
		and isCanonicalUuid(peer.studio_id)
		and isInteger(peer.generation)
		and isSecret(peer.resume_token)
	then
		requestOnce("/v2/studios/disconnect", {
			studio_id = peer.studio_id,
			generation = peer.generation,
			resume_token = peer.resume_token,
		})
	end
	peer.connected = false
	peer.resume_token = nil
	peer.alive = false
	if shared[SINGLETON_KEY] == peer then
		shared[SINGLETON_KEY] = nil
	end
end

local function synchronousUnload()
	if peer.unloading then
		return
	end
	peer.unloading = true
	peer.shutdown_requested = true
	peer.alive = false

	local transition = peer.active_play
	if transition ~= nil then
		-- Never start a prepared runner after unload and never make an HTTP
		-- completion claim from this non-yielding callback.
		transition.bridge_token = nil
		if transition.runner_finished == true
			and modeSnapshot().mode == "edit"
		then
			local cleaned = pcall(function()
				cleanupTemporaryScript(transition)
			end)
			if not cleaned then
				peer.last_error =
					"Unload could not prove exact temporary Script cleanup"
			end
		end
	elseif modeSnapshot().mode == "edit" then
		local cleaned = pcall(removeMarker)
		if not cleaned then
			peer.last_error = "Unload could not prove exact marker cleanup"
		end
	end

	-- RequestAsync yields and is not safe from Plugin.Unloading. Locally fence
	-- credentials and release the DataModel singleton; the bounded host lease
	-- and Play-server watchdog retain external recovery ownership.
	peer.connected = false
	peer.resume_token = nil
	peer.response_cache = {}
	peer.response_order = {}
	if shared[SINGLETON_KEY] == peer then
		shared[SINGLETON_KEY] = nil
	end
end

plugin.Unloading:Connect(synchronousUnload)

local function run()
	local connected, connectError = connect(false)
	if not connected then
		peer.last_error = sanitizeMessage(connectError)
		bestEffortFinalize()
		return
	end
	while peer.alive and not peer.shutdown_requested do
		assertExpectedDocument()
		reconcilePendingPreRunAbort()
		if not peer.alive then
			break
		end
		reconcileNaturalReturn()
		if not peer.alive then
			break
		end
		local ok, result = requestWithRetry("/v2/studios/poll", {
			studio_id = peer.studio_id,
			generation = peer.generation,
			resume_token = peer.resume_token,
		})
		if ok then
			if result ~= nil then
				handleRequest(result)
			end
		else
			local leaseDeadline = peer.last_hub_success_at
				+ RECONNECT_LEASE_SECONDS
			while peer.alive
				and not peer.shutdown_requested
				and os.clock() < leaseDeadline
			do
				task.wait(math.max(
					0.05,
					math.min(0.25, leaseDeadline - os.clock())
				))
			end
			if not peer.alive or peer.shutdown_requested then
				break
			end
			local reconnected, reconnectError = connect(true)
			if not reconnected then
				peer.last_error = sanitizeMessage(reconnectError)
				peer.alive = false
			else
				local recoveryOk, recoveryError = pcall(function()
					reconcileRecoveryAfterReconnect()
				end)
				if not recoveryOk then
					local normalized, fatal = normalizedError(
						recoveryError,
						"play_recovery_failed"
					)
					peer.last_error = normalized.message
					if fatal then
						peer.alive = false
					end
				end
			end
		end
	end
	bestEffortFinalize()
end

task.spawn(function()
	local ok, runError = pcall(run)
	if not ok then
		peer.last_error = sanitizeMessage(runError)
		bestEffortFinalize()
	end
end)
