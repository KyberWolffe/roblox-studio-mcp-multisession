--[[
Fixed Studio MCP Multisession PlayServer plugin bridge.

This source is embedded twice in the rendered Studio plugin: as executable
PlayServer plugin code and as the inert temporary Script marker's auditable
source. It accepts only the closed ExecutePlayModeAsync bootstrap schema below.
The loopback origin, endpoint paths, retry limits, watchdog duration, and
EndTest payload are not caller controlled.
]]

local EXPECTED_RUN_ID = "__RUN_ID__"
local BASE_URL = "http://127.0.0.1:44756"
local TEMP_SCRIPT_PREFIX = "_StudioMCPv2_PlayServerBridge_v2_"
local HARD_WATCHDOG_SECONDS = 180
local MAX_ACTIVATION_WINDOW_SECONDS = 165
local END_TEST_ATTEMPTS = 3
local HTTP_TIMEOUT_SECONDS = 5
local MAX_HTTP_BODY_BYTES = 32_768
local MAX_HTTP_RESPONSE_BYTES = 65_536
local MAX_REQUEST_ATTEMPTS = 4
local POLL_INTERVAL_SECONDS = 0.2

local ATTACH_PATH = "/v2/play-bridge/attach"
local POLL_PATH = "/v2/play-bridge/server-poll"
local ACK_PATH = "/v2/play-bridge/server-ack"
local ALLOWED_PATHS = table.freeze({
	[ATTACH_PATH] = true,
	[POLL_PATH] = true,
	[ACK_PATH] = true,
})

-- Durable routing has no place-count or place-ID allowlist.

local BOOTSTRAP_KEYS = table.freeze({
	v = true,
	kind = true,
	run_id = true,
	studio_id = true,
	client_instance_id = true,
	document_epoch = true,
	transition_generation = true,
	play_request_id = true,
	expected_place_id = true,
	expected_game_id = true,
	transition_nonce = true,
	bridge_token = true,
	activation_deadline_unix = true,
	temporary_script_name = true,
})

local SNAPSHOT_KEYS = table.freeze({
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
	server_token = true,
	required_ack = true,
	command = true,
})

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local ServerScriptService = game:GetService("ServerScriptService")
local StudioTestService = game:GetService("StudioTestService")

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
		error(label .. " must be an object", 0)
	end
	for key in pairs(value) do
		if type(key) ~= "string" or allowed[key] ~= true then
			error(label .. " contains an unexpected field", 0)
		end
	end
end

local function invokeEndTest(payload)
	for attempt = 1, END_TEST_ATTEMPTS do
		local ok = pcall(function()
			StudioTestService:EndTest(payload)
		end)
		if ok then
			return true
		end
		if attempt < END_TEST_ATTEMPTS then
			task.wait(0.05 * attempt)
		end
	end
	return false
end

local function validateBootstrap(value)
	assertExactKeys(value, BOOTSTRAP_KEYS, "Play bootstrap")
	if value.v ~= 2 or value.kind ~= "studio_mcp_v2_play_bootstrap" then
		error("Play bootstrap version or kind is invalid", 0)
	end
	if value.run_id ~= EXPECTED_RUN_ID then
		error("Play bootstrap run_id is invalid", 0)
	end
	if not isCanonicalUuid(value.studio_id)
		or not isCanonicalUuid(value.client_instance_id)
		or not isCanonicalUuid(value.document_epoch)
		or not isCanonicalUuid(value.transition_nonce)
	then
		error("Play bootstrap UUID identity is invalid", 0)
	end
	if not isInteger(value.transition_generation)
		or value.transition_generation < 1
		or not isRequestId(value.play_request_id)
		or not isSecret(value.bridge_token)
	then
		error("Play bootstrap routing identity is invalid", 0)
	end
	if not isInteger(value.expected_place_id)
		or value.expected_place_id < 0
		or not isInteger(value.expected_game_id)
		or value.expected_game_id < 0
		or value.expected_place_id ~= game.PlaceId
		or value.expected_game_id ~= game.GameId
	then
		error("Play bootstrap does not match this DataModel", 0)
	end
	local expectedScriptName = TEMP_SCRIPT_PREFIX
		.. string.gsub(value.transition_nonce, "-", "")
	if value.temporary_script_name ~= expectedScriptName then
		error("Play bootstrap temporary Script name is invalid", 0)
	end
	local currentUnix = os.time()
	if not isInteger(value.activation_deadline_unix)
		or value.activation_deadline_unix < currentUnix
		or value.activation_deadline_unix
			> currentUnix + MAX_ACTIVATION_WINDOW_SECONDS
	then
		error("Play bootstrap activation deadline is invalid", 0)
	end
	return value
end

if not RunService:IsStudio()
	or not RunService:IsServer()
	or not RunService:IsRunning()
then
	error("Studio MCP Multisession Play bridge must run in server context", 0)
end

-- Arm the bounded EndTest watchdog only after proving this is a running
-- Studio server DataModel with nonnegative document identifiers. Exact
-- bootstrap identity, token, transition nonce, and Script ownership are
-- validated below; no global Studio selector exists.
if not isInteger(game.PlaceId)
	or game.PlaceId < 0
	or not isInteger(game.GameId)
	or game.GameId < 0
then
	error("Studio MCP Multisession Play bridge document identity is invalid", 0)
end

-- Roblox only permits EndTest from the running server DataModel. Arm the
-- watchdog immediately after that context is proven, before GetTestArgs or any
-- bootstrap/ownership validation can throw. Until a fully validated bootstrap
-- is available, its fallback result is intentionally uncorrelated and cannot
-- be accepted as a successful controller completion.
local finished = false
local ending = false
local watchdogHandler = nil
task.delay(HARD_WATCHDOG_SECONDS, function()
	if finished then
		return
	end
	if watchdogHandler ~= nil then
		watchdogHandler()
		return
	end
	ending = true
	if invokeEndTest({
		v = 2,
		kind = "studio_mcp_v2_validation_abort",
		run_id = EXPECTED_RUN_ID,
		reason = "hard_watchdog_validation_abort",
	}) then
		finished = true
	end
	ending = false
end)

local bootstrap = validateBootstrap(StudioTestService:GetTestArgs())
local documentPolicy = table.freeze({
	session_tag = string.sub(
		string.gsub(bootstrap.client_instance_id, "-", ""),
		1,
		12
	),
})
local serverInstanceId = string.lower(HttpService:GenerateGUID(false))
local attachId = string.lower(HttpService:GenerateGUID(false))
local watchdogAckId = string.lower(HttpService:GenerateGUID(false))
local stopAckId = string.lower(HttpService:GenerateGUID(false))
local attached = false
local serverReady = false
local activeStopCommandId = nil

local FAILURE_CODES = table.freeze({
	request_blocked = true,
	request_encode_invalid = true,
	request_exception = true,
	request_exception_http_disabled = true,
	response_invalid = true,
	response_non_success = true,
	response_oversize = true,
	envelope_invalid = true,
	bridge_already_ended = true,
})

local function endTest(reason, correlation, failureCode)
	if finished or ending then
		return finished
	end
	ending = true
	local payload = {
		v = 2,
		kind = "studio_mcp_v2_end_test",
		run_id = EXPECTED_RUN_ID,
		studio_id = bootstrap.studio_id,
		client_instance_id = bootstrap.client_instance_id,
		document_epoch = bootstrap.document_epoch,
		transition_generation = bootstrap.transition_generation,
		play_request_id = bootstrap.play_request_id,
		transition_nonce = bootstrap.transition_nonce,
		reason = reason,
		end_test_correlation = correlation,
		server_instance_id = serverInstanceId,
	}
	if failureCode ~= nil then
		if FAILURE_CODES[failureCode] ~= true then
			failureCode = "request_exception"
		end
		payload.failure_code = failureCode
	end
	if invokeEndTest(payload) then
		finished = true
		ending = false
		return true
	end
	ending = false
	return false
end

watchdogHandler = function()
	if finished then
		return
	end
	if activeStopCommandId ~= nil then
		endTest("hard_watchdog_stop", activeStopCommandId)
	elseif serverReady then
		endTest("hard_watchdog_natural", bootstrap.transition_nonce)
	elseif attached then
		-- There is intentionally no claim that this is a valid start_failed
		-- completion. The controller must inspect host state and fail closed.
		endTest("hard_watchdog_attached_unready", bootstrap.transition_nonce)
	else
		endTest("hard_watchdog_start_failed", bootstrap.play_request_id)
	end
end

local bridgeMarker = ServerScriptService:FindFirstChild(
	bootstrap.temporary_script_name
)
if bridgeMarker == nil
	or bridgeMarker.Name ~= bootstrap.temporary_script_name
	or bridgeMarker.Parent ~= ServerScriptService
	or not bridgeMarker:IsA("Script")
	or bridgeMarker.Enabled ~= false
	or bridgeMarker:GetAttribute("StudioMCPv2TemporaryKind")
		~= "play_server_bridge_v2"
	or bridgeMarker:GetAttribute("StudioMCPv2RunId") ~= EXPECTED_RUN_ID
	or bridgeMarker:GetAttribute("StudioMCPv2ClientInstanceId")
		~= bootstrap.client_instance_id
	or bridgeMarker:GetAttribute("StudioMCPv2DocumentEpoch")
		~= bootstrap.document_epoch
	or bridgeMarker:GetAttribute("StudioMCPv2TransitionNonce")
		~= bootstrap.transition_nonce
	or bridgeMarker:GetAttribute("StudioMCPv2SessionTag")
		~= documentPolicy.session_tag
then
	error("Play bridge marker ownership is invalid", 0)
end

local function commonBody()
	return {
		studio_id = bootstrap.studio_id,
		client_instance_id = bootstrap.client_instance_id,
		document_epoch = bootstrap.document_epoch,
		transition_generation = bootstrap.transition_generation,
		play_request_id = bootstrap.play_request_id,
		expected_place_id = bootstrap.expected_place_id,
		expected_game_id = bootstrap.expected_game_id,
		transition_nonce = bootstrap.transition_nonce,
		server_instance_id = serverInstanceId,
	}
end

local function requestOnce(path, bearerToken, body)
	if ALLOWED_PATHS[path] ~= true or not isSecret(bearerToken) then
		return false, "request_blocked"
	end
	local encodedOk, encoded = pcall(function()
		return HttpService:JSONEncode(body)
	end)
	if not encodedOk
		or type(encoded) ~= "string"
		or #encoded > MAX_HTTP_BODY_BYTES
	then
		return false, "request_encode_invalid"
	end
	local callOk, response = pcall(function()
		return HttpService:RequestAsync({
			Url = BASE_URL .. path,
			Method = "POST",
			Headers = {
				["Authorization"] = "Bearer " .. bearerToken,
				["Content-Type"] = "application/json",
			},
			Body = encoded,
			Compress = Enum.HttpCompression.None,
			Timeout = HTTP_TIMEOUT_SECONDS,
		})
	end)
	if not callOk then
		local lowered = string.lower(tostring(response))
		if string.find(lowered, "http requests are not enabled", 1, true)
			or string.find(lowered, "allow http requests", 1, true)
		then
			return false, "request_exception_http_disabled"
		end
		return false, "request_exception"
	end
	if type(response) ~= "table" or type(response.Body) ~= "string" then
		return false, "response_invalid"
	end
	if response.Success ~= true then
		return false, "response_non_success"
	end
	if #response.Body > MAX_HTTP_RESPONSE_BYTES then
		return false, "response_oversize"
	end
	local decodeOk, payload = pcall(function()
		return HttpService:JSONDecode(response.Body)
	end)
	if not decodeOk
		or type(payload) ~= "table"
		or payload.ok ~= true
		or type(payload.result) ~= "table"
	then
		return false, "envelope_invalid"
	end
	return true, payload.result
end

local function requestWithRetry(path, bearerToken, body)
	local lastError = "request_exception"
	for attempt = 1, MAX_REQUEST_ATTEMPTS do
		if finished then
			return false, "bridge_already_ended"
		end
		local ok, result = requestOnce(path, bearerToken, body)
		if ok then
			return true, result
		end
		lastError = result
		if attempt < MAX_REQUEST_ATTEMPTS then
			task.wait(math.min(0.1 * (2 ^ (attempt - 1)), 0.8))
		end
	end
	return false, lastError
end

local function validateSnapshot(result)
	assertExactKeys(result, SNAPSHOT_KEYS, "Play bridge result")
	if result.v ~= 2
		or result.studio_id ~= bootstrap.studio_id
		or result.client_instance_id ~= bootstrap.client_instance_id
		or result.document_epoch ~= bootstrap.document_epoch
		or result.transition_generation ~= bootstrap.transition_generation
		or result.play_request_id ~= bootstrap.play_request_id
		or result.expected_place_id ~= bootstrap.expected_place_id
		or result.expected_game_id ~= bootstrap.expected_game_id
		or result.transition_nonce ~= bootstrap.transition_nonce
	then
		error("Play bridge result context is stale or mismatched", 0)
	end
	if type(result.state) ~= "string"
		or type(result.attached) ~= "boolean"
		or type(result.watchdog_armed) ~= "boolean"
		or type(result.stop_requested) ~= "boolean"
		or type(result.stop_acked) ~= "boolean"
	then
		error("Play bridge result state is invalid", 0)
	end
	return result
end

local attachBody = commonBody()
attachBody.attach_id = attachId
local attachedOk, attachedResult = requestWithRetry(
	ATTACH_PATH,
	bootstrap.bridge_token,
	attachBody
)
if not attachedOk then
	endTest("attach_failed", bootstrap.play_request_id, attachedResult)
	return
end
local attachSnapshotOk, attachSnapshot = pcall(function()
	return validateSnapshot(attachedResult)
end)
if not attachSnapshotOk
	or attachSnapshot.attached ~= true
	or not isSecret(attachSnapshot.server_token)
then
	endTest("attach_rejected", bootstrap.play_request_id)
	return
end

local serverToken = attachSnapshot.server_token
attached = true
bootstrap.bridge_token = nil

-- Readiness is the explicit watchdog_armed acknowledgement. It is always sent
-- immediately after attach, including attach-after-stop recovery.
local readyAckBody = commonBody()
readyAckBody.ack_kind = "watchdog_armed"
readyAckBody.ack_id = watchdogAckId
local readyOk, readyResult = requestWithRetry(
	ACK_PATH,
	serverToken,
	readyAckBody
)
if not readyOk then
	endTest("watchdog_ack_failed", bootstrap.play_request_id, readyResult)
	return
end
local readySnapshotOk, readySnapshot = pcall(function()
	return validateSnapshot(readyResult)
end)
if not readySnapshotOk or readySnapshot.watchdog_armed ~= true then
	endTest("watchdog_ack_rejected", bootstrap.play_request_id)
	return
end
serverReady = true

while not finished do
	local polledOk, polledResult = requestWithRetry(
		POLL_PATH,
		serverToken,
		commonBody()
	)
	if not polledOk then
		task.wait(POLL_INTERVAL_SECONDS)
		continue
	end
	local pollSnapshotOk, pollSnapshot = pcall(function()
		return validateSnapshot(polledResult)
	end)
	if not pollSnapshotOk then
		endTest("invalid_poll_result", bootstrap.transition_nonce)
		return
	end
	if pollSnapshot.command == "stop" then
		if not isCanonicalUuid(pollSnapshot.stop_command_id) then
			endTest("invalid_stop_command", bootstrap.transition_nonce)
			return
		end
		activeStopCommandId = pollSnapshot.stop_command_id
		local stopAckBody = commonBody()
		stopAckBody.ack_kind = "stop_received"
		stopAckBody.ack_id = stopAckId
		stopAckBody.stop_command_id = activeStopCommandId
		local ackedOk, ackedResult = requestWithRetry(
			ACK_PATH,
			serverToken,
			stopAckBody
		)
		if ackedOk then
			local ackSnapshotOk, ackSnapshot = pcall(function()
				return validateSnapshot(ackedResult)
			end)
			if ackSnapshotOk and ackSnapshot.stop_acked == true then
				endTest("server_stop", activeStopCommandId)
				return
			end
		end
	elseif pollSnapshot.command ~= "wait"
		and pollSnapshot.command ~= "stop_acked"
		and pollSnapshot.command ~= "arm_watchdog"
	then
		endTest("invalid_poll_command", bootstrap.transition_nonce)
		return
	end
	task.wait(POLL_INTERVAL_SECONDS)
end
