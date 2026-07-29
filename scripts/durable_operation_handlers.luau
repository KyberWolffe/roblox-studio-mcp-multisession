--[[
Durable Studio MCP v2 operation registry.

This source is inserted into the side-by-side plugin at install time. Every
handler operates only inside the plugin's already-bound DataModel. Instance
targets are exact child-name segment arrays, never executable expressions.
There is no host process, filesystem, shell, network URL, or dynamic-code
operation in this registry.
]]

local function optionalService(name)
	local ok, service = pcall(function()
		return game:GetService(name)
	end)
	return ok and service or nil
end

-- Optional/version-gated APIs are resolved without preventing the core
-- session from registering. Their handlers fail closed when unavailable.
local ScriptEditorService = optionalService("ScriptEditorService")
local LogService = optionalService("LogService")
local StudioCaptureService = optionalService("StudioCaptureService")
local EncodingService = optionalService("EncodingService")

local MAX_PATH_SEGMENTS = 64
local MAX_PATH_SEGMENT_BYTES = 100
local MAX_SOURCE_BYTES = 262_144
local MAX_ATTRIBUTE_STRING_BYTES = 4_096
local MAX_CONSOLE_MESSAGE_BYTES = 2_000
local MAX_SCREENSHOT_BYTES = 600_000
local SCREENSHOT_READY_SECONDS = 5

local OPERATION_ARG_KEYS = table.freeze({
	studio_get_state = table.freeze({}),
	studio_list_tree = table.freeze({
		root_path = true,
		max_depth = true,
		max_results = true,
	}),
	studio_read_script = table.freeze({
		path = true,
		max_chars = true,
	}),
	studio_update_script = table.freeze({
		path = true,
		expected_sha256 = true,
		new_source = true,
	}),
	studio_set_attribute = table.freeze({
		path = true,
		name = true,
		expected_exists = true,
		expected_value_type = true,
		expected_value = true,
		value_type = true,
		value = true,
	}),
	studio_get_console = table.freeze({
		max_entries = true,
	}),
	studio_capture_screenshot = table.freeze({
		max_width = true,
		max_height = true,
	}),
	studio_fire_input_binding = table.freeze({
		path = true,
		state_type = true,
		state = true,
	}),
	studio_start_stop_play = table.freeze({
		is_start = true,
	}),
})

local function isFiniteNumber(value)
	return type(value) == "number"
		and value == value
		and value > -math.huge
		and value < math.huge
end

local function validateIntegerRange(value, minimum, maximum, label)
	if not isInteger(value) or value < minimum or value > maximum then
		adapterError(
			"invalid_args",
			label .. " must be an integer from "
				.. tostring(minimum) .. " to " .. tostring(maximum),
			false
		)
	end
	return value
end

local function validatePath(path, allowEmpty, label)
	if type(path) ~= "table" then
		adapterError("invalid_args", label .. " must be a path array", false)
	end
	local count = #path
	if (not allowEmpty and count < 1) or count > MAX_PATH_SEGMENTS then
		adapterError("invalid_args", label .. " has an invalid length", false)
	end
	for key in pairs(path) do
		if not isInteger(key) or key < 1 or key > count then
			adapterError(
				"invalid_args",
				label .. " must be a dense path array",
				false
			)
		end
	end
	for index = 1, count do
		local segment = path[index]
		if type(segment) ~= "string"
			or #segment < 1
			or #segment > MAX_PATH_SEGMENT_BYTES
			or string.find(segment, "[%c]") ~= nil
		then
			adapterError(
				"invalid_args",
				label .. " contains an invalid child-name segment",
				false
			)
		end
	end
	return path
end

local function copyPath(path)
	local result = {}
	for index, segment in ipairs(path) do
		result[index] = segment
	end
	return result
end

local function appendPath(path, segment)
	local result = copyPath(path)
	table.insert(result, segment)
	return result
end

local function resolveExactPath(path, allowRoot)
	validatePath(path, allowRoot, "path")
	local current = game
	for _, segment in ipairs(path) do
		local match = nil
		for _, child in ipairs(current:GetChildren()) do
			if child.Name == segment then
				if match ~= nil then
					adapterError(
						"ambiguous_instance_path",
						"Exact path has duplicate child-name matches",
						false
					)
				end
				match = child
			end
		end
		if match == nil then
			adapterError(
				"instance_not_found",
				"Exact instance path was not found",
				false
			)
		end
		current = match
	end
	return current
end

local function sourceSha256(source)
	local ok, binaryDigest = pcall(function()
		return EncodingService:ComputeStringHash(
			source,
			Enum.HashAlgorithm.Sha256
		)
	end)
	if not ok
		or type(binaryDigest) ~= "string"
		or #binaryDigest ~= 32
	then
		adapterError(
			"source_revision_unavailable",
			"Studio could not produce a SHA-256 source revision",
			false
		)
	end
	local hexadecimal = table.create(32)
	for index = 1, 32 do
		hexadecimal[index] = string.format(
			"%02x",
			string.byte(binaryDigest, index)
		)
	end
	return table.concat(hexadecimal)
end

local function requireLuaSourceContainer(path)
	local target = resolveExactPath(path, false)
	if not target:IsA("LuaSourceContainer") then
		adapterError(
			"invalid_script_target",
			"Exact target is not a LuaSourceContainer",
			false
		)
	end
	return target
end

local function readEditorSource(target)
	local ok, source = pcall(function()
		return ScriptEditorService:GetEditorSource(target)
	end)
	if not ok or type(source) ~= "string" or #source > MAX_SOURCE_BYTES then
		adapterError(
			"script_source_unavailable",
			"Editor source is unavailable or exceeds the durable limit",
			false
		)
	end
	return source
end

local function validatePrimitive(valueType, value, label, allowNone)
	if valueType == "none" and allowNone then
		if value ~= nil then
			adapterError("invalid_args", label .. " must omit its value", false)
		end
		return nil
	elseif valueType == "string" then
		if type(value) ~= "string" or #value > MAX_ATTRIBUTE_STRING_BYTES then
			adapterError(
				"invalid_args",
				label .. " string is invalid or too large",
				false
			)
		end
	elseif valueType == "number" then
		if not isFiniteNumber(value) then
			adapterError("invalid_args", label .. " number is invalid", false)
		end
	elseif valueType == "boolean" then
		if type(value) ~= "boolean" then
			adapterError("invalid_args", label .. " must be boolean", false)
		end
	else
		adapterError("invalid_args", label .. " type is unsupported", false)
	end
	return value
end

local function validateVectorState(value, dimensions)
	if type(value) ~= "table" then
		adapterError("invalid_args", "Vector input state must be an object", false)
	end
	local allowed = dimensions == 2
		and table.freeze({ x = true, y = true })
		or table.freeze({ x = true, y = true, z = true })
	assertExactKeys(value, allowed, "input_state")
	if not isFiniteNumber(value.x)
		or not isFiniteNumber(value.y)
		or (dimensions == 3 and not isFiniteNumber(value.z))
	then
		adapterError(
			"invalid_args",
			"Vector input state components must be finite numbers",
			false
		)
	end
	if dimensions == 2 then
		return Vector2.new(value.x, value.y)
	end
	return Vector3.new(value.x, value.y, value.z)
end

local function validateDurableArgs(operation, args, deadlineMs)
	if type(args) ~= "table" then
		adapterError("invalid_args", "args must be an object", false)
	end
	local allowed = OPERATION_ARG_KEYS[operation]
	if allowed == nil then
		adapterError(
			"unsupported_operation",
			"Operation is outside the durable allowlist",
			false
		)
	end
	assertExactKeys(args, allowed, "args")

	if operation == "studio_get_state" then
		return
	elseif operation == "studio_list_tree" then
		validatePath(args.root_path or {}, true, "root_path")
		if args.max_depth ~= nil then
			validateIntegerRange(args.max_depth, 0, 6, "max_depth")
		end
		if args.max_results ~= nil then
			validateIntegerRange(args.max_results, 1, 500, "max_results")
		end
	elseif operation == "studio_read_script" then
		validatePath(args.path, false, "path")
		if args.max_chars ~= nil then
			validateIntegerRange(args.max_chars, 1, MAX_SOURCE_BYTES, "max_chars")
		end
	elseif operation == "studio_update_script" then
		validatePath(args.path, false, "path")
		if type(args.expected_sha256) ~= "string"
			or #args.expected_sha256 ~= 64
			or string.find(args.expected_sha256, "[^0-9a-f]") ~= nil
		then
			adapterError(
				"invalid_args",
				"expected_sha256 must be a lowercase SHA-256 digest",
				false
			)
		end
		if type(args.new_source) ~= "string"
			or #args.new_source > MAX_SOURCE_BYTES
		then
			adapterError(
				"invalid_args",
				"new_source is invalid or exceeds the durable limit",
				false
			)
		end
	elseif operation == "studio_set_attribute" then
		validatePath(args.path, false, "path")
		if type(args.name) ~= "string"
			or #args.name < 1
			or #args.name > 100
			or string.find(args.name, "[%c]") ~= nil
		then
			adapterError("invalid_args", "Attribute name is invalid", false)
		end
		if type(args.expected_exists) ~= "boolean" then
			adapterError(
				"invalid_args",
				"expected_exists must be boolean",
				false
			)
		end
		if args.expected_exists then
			if args.expected_value_type == "none" then
				adapterError(
					"invalid_args",
					"An existing attribute requires an expected primitive type",
					false
				)
			end
			validatePrimitive(
				args.expected_value_type,
				args.expected_value,
				"expected attribute",
				false
			)
		else
			if args.expected_value_type ~= "none"
				or args.expected_value ~= nil
			then
				adapterError(
					"invalid_args",
					"A missing attribute requires expected_value_type none",
					false
				)
			end
		end
		if args.value_type == "remove" then
			if args.value ~= nil then
				adapterError(
					"invalid_args",
					"Remove must omit the replacement value",
					false
				)
			end
		else
			validatePrimitive(
				args.value_type,
				args.value,
				"replacement attribute",
				false
			)
		end
	elseif operation == "studio_get_console" then
		if args.max_entries ~= nil then
			validateIntegerRange(args.max_entries, 1, 200, "max_entries")
		end
	elseif operation == "studio_capture_screenshot" then
		if args.max_width ~= nil then
			validateIntegerRange(args.max_width, 64, 1280, "max_width")
		end
		if args.max_height ~= nil then
			validateIntegerRange(args.max_height, 64, 720, "max_height")
		end
	elseif operation == "studio_fire_input_binding" then
		validatePath(args.path, false, "path")
		if args.state_type == "boolean" then
			if type(args.state) ~= "boolean" then
				adapterError("invalid_args", "Input state must be boolean", false)
			end
		elseif args.state_type == "number" then
			if not isFiniteNumber(args.state) then
				adapterError(
					"invalid_args",
					"Input state must be a finite number",
					false
				)
			end
		elseif args.state_type == "vector2" then
			validateVectorState(args.state, 2)
		elseif args.state_type == "vector3" then
			validateVectorState(args.state, 3)
		else
			adapterError("invalid_args", "Input state type is unsupported", false)
		end
	elseif operation == "studio_start_stop_play" then
		if type(args.is_start) ~= "boolean" then
			adapterError("invalid_args", "is_start must be boolean", false)
		end
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
	validateDurableArgs(request.operation, request.args, request.deadline_ms)
	local encodedOk, encoded = pcall(function()
		return HttpService:JSONEncode(request.args)
	end)
	if not encodedOk or type(encoded) ~= "string" then
		adapterError("invalid_args", "args signature is unavailable", false)
	end
	return table.concat({
		request.operation,
		tostring(request.deadline_ms),
		encoded,
	}, "\0")
end

local function durableGetState()
	local play = playStateSnapshot()
	local mode = observedMode(play)
	return {
		adapter = "studio-mcp-v2-durable-plugin",
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		broker_instance_id = peer.broker_instance_id,
		run_id = CONFIG.run_id,
		session_tag = DOCUMENT_POLICY.session_tag,
		name = DOCUMENT_NAME,
		place_id = EXPECTED_PLACE_ID,
		game_id = EXPECTED_GAME_ID,
		mode = mode,
		is_edit = mode == "edit",
		play = play,
	}
end

local function listTree(args)
	local rootPath = args.root_path or {}
	local root = resolveExactPath(rootPath, true)
	local maxDepth = args.max_depth or 2
	local maxResults = args.max_results or 200
	local items = {}
	local truncated = false

	local function visit(instance, path, depth)
		if #items >= maxResults then
			truncated = true
			return
		end
		table.insert(items, {
			path = copyPath(path),
			name = instance == game and game.Name or instance.Name,
			class_name = instance.ClassName,
			child_count = #instance:GetChildren(),
		})
		if depth >= maxDepth then
			return
		end
		local children = instance:GetChildren()
		table.sort(children, function(left, right)
			if left.Name == right.Name then
				return left.ClassName < right.ClassName
			end
			return left.Name < right.Name
		end)
		for _, child in ipairs(children) do
			visit(child, appendPath(path, child.Name), depth + 1)
			if truncated then
				return
			end
		end
	end

	visit(root, rootPath, 0)
	return {
		root_path = copyPath(rootPath),
		items = items,
		truncated = truncated,
		max_depth = maxDepth,
		max_results = maxResults,
	}
end

local function readScript(args)
	requireEditMode()
	local target = requireLuaSourceContainer(args.path)
	local source = readEditorSource(target)
	local maxChars = args.max_chars or MAX_SOURCE_BYTES
	local truncated = #source > maxChars
	return {
		path = copyPath(args.path),
		class_name = target.ClassName,
		source = truncated and string.sub(source, 1, maxChars) or source,
		source_length = #source,
		source_sha256 = sourceSha256(source),
		truncated = truncated,
	}
end

local function updateScript(args)
	requireEditMode()
	local target = requireLuaSourceContainer(args.path)
	local callbackInvoked = false
	local replaced = false
	local observedSha256 = nil
	local updateOk, updateError = pcall(function()
		ScriptEditorService:UpdateSourceAsync(target, function(currentSource)
			if callbackInvoked then
				return currentSource
			end
			callbackInvoked = true
			if type(currentSource) ~= "string"
				or #currentSource > MAX_SOURCE_BYTES
			then
				return currentSource
			end
			observedSha256 = sourceSha256(currentSource)
			if observedSha256 ~= args.expected_sha256 then
				return currentSource
			end
			replaced = true
			return args.new_source
		end)
	end)
	if not updateOk then
		adapterError(
			"script_update_unavailable",
			"Studio rejected the bounded script update: "
				.. sanitizeMessage(updateError),
			false
		)
	end
	if not callbackInvoked or not replaced then
		adapterError(
			"script_revision_conflict",
			"Script source changed after it was read; no update was applied",
			false
		)
	end
	assertExpectedDocument()
	local confirmed = readEditorSource(target)
	local newSha256 = sourceSha256(confirmed)
	if confirmed ~= args.new_source then
		adapterError(
			"script_update_unconfirmed",
			"Studio did not confirm the exact requested source",
			false
		)
	end
	return {
		updated = true,
		path = copyPath(args.path),
		previous_sha256 = observedSha256,
		source_sha256 = newSha256,
		source_length = #confirmed,
	}
end

local function primitiveMatches(expectedType, expectedValue, actualValue)
	if expectedType == "string" then
		return type(actualValue) == "string" and actualValue == expectedValue
	elseif expectedType == "number" then
		return type(actualValue) == "number" and actualValue == expectedValue
	elseif expectedType == "boolean" then
		return type(actualValue) == "boolean" and actualValue == expectedValue
	end
	return false
end

local function setAttribute(args)
	requireEditMode()
	local target = resolveExactPath(args.path, false)
	local current = target:GetAttribute(args.name)
	if args.expected_exists then
		if current == nil
			or not primitiveMatches(
				args.expected_value_type,
				args.expected_value,
				current
			)
		then
			adapterError(
				"attribute_revision_conflict",
				"Attribute no longer matches its expected prior state",
				false
			)
		end
	elseif current ~= nil then
		adapterError(
			"attribute_revision_conflict",
			"Attribute was created after its missing state was observed",
			false
		)
	end

	local replacement = args.value_type == "remove" and nil or args.value
	local setOk, setError = pcall(function()
		target:SetAttribute(args.name, replacement)
	end)
	if not setOk then
		adapterError(
			"attribute_update_unavailable",
			"Studio rejected the bounded attribute update: "
				.. sanitizeMessage(setError),
			false
		)
	end
	assertExpectedDocument()
	local confirmed = target:GetAttribute(args.name)
	if args.value_type == "remove" then
		if confirmed ~= nil then
			adapterError(
				"attribute_update_unconfirmed",
				"Studio did not confirm attribute removal",
				false
			)
		end
	elseif not primitiveMatches(args.value_type, args.value, confirmed) then
		adapterError(
			"attribute_update_unconfirmed",
			"Studio did not confirm the exact replacement attribute",
			false
		)
	end
	return {
		updated = true,
		path = copyPath(args.path),
		name = args.name,
		exists = args.value_type ~= "remove",
		value_type = args.value_type,
		value = replacement,
	}
end

local function getConsole(args)
	local maxEntries = args.max_entries or 100
	local ok, history = pcall(function()
		return LogService:GetLogHistory()
	end)
	if not ok or type(history) ~= "table" then
		adapterError(
			"console_unavailable",
			"Studio log history is unavailable",
			false
		)
	end
	local first = math.max(1, #history - maxEntries + 1)
	local entries = {}
	for index = first, #history do
		local entry = history[index]
		if type(entry) == "table" then
			local message = tostring(entry.message or "")
			message = string.gsub(message, "[%c]", " ")
			table.insert(entries, {
				message = string.sub(message, 1, MAX_CONSOLE_MESSAGE_BYTES),
				message_type = tostring(entry.messageType or "unknown"),
				timestamp = isFiniteNumber(entry.timestamp)
					and entry.timestamp or nil,
			})
		end
	end
	return {
		entries = entries,
		returned = #entries,
		total_available = #history,
		truncated = #history > #entries,
	}
end

local function waitForScreenshotReady(capture)
	local deadline = os.clock() + SCREENSHOT_READY_SECONDS
	while os.clock() < deadline do
		local statusOk, status = pcall(function()
			return capture.BufferStatus
		end)
		if not statusOk then
			break
		end
		if status == Enum.StudioCaptureBufferStatus.Ready then
			return
		end
		if status == Enum.StudioCaptureBufferStatus.Error then
			break
		end
		task.wait(0.05)
	end
	adapterError(
		"screenshot_unavailable",
		"Studio screenshot capture did not reach Ready within the bound",
		false
	)
end

local function captureScreenshot(args)
	local canOk, canCapture = pcall(function()
		return StudioCaptureService:CanCaptureScreenshot()
	end)
	if not canOk or canCapture ~= true then
		adapterError(
			"screenshot_permission_required",
			"Studio screenshot permission or context is unavailable",
			false
		)
	end
	local captureOk, capture = pcall(function()
		return StudioCaptureService:CaptureScreenshot({})
	end)
	if not captureOk or capture == nil then
		adapterError(
			"screenshot_unavailable",
			"Studio screenshot capture is unavailable",
			false
		)
	end
	waitForScreenshotReady(capture)

	local maxWidth = args.max_width or 1280
	local maxHeight = args.max_height or 720
	local resolution = capture.Resolution
	if resolution.X > maxWidth or resolution.Y > maxHeight then
		local scale = math.min(
			maxWidth / resolution.X,
			maxHeight / resolution.Y
		)
		local newSize = Vector2.new(
			math.max(1, math.floor(resolution.X * scale)),
			math.max(1, math.floor(resolution.Y * scale))
		)
		local scaleOk, scaled = pcall(function()
			return capture:ScaleAsync(Enum.ResamplerMode.Default, newSize)
		end)
		if not scaleOk or scaled == nil then
			adapterError(
				"screenshot_scaling_unavailable",
				"Studio could not scale the screenshot into the bound",
				false
			)
		end
		capture = scaled
		waitForScreenshotReady(capture)
		resolution = capture.Resolution
	end

	if capture.BufferFormat ~= Enum.StudioCaptureScreenshotFormat.PNG then
		adapterError(
			"screenshot_format_unsupported",
			"Studio did not provide a PNG screenshot buffer",
			false
		)
	end
	local bufferOk, pngBuffer = pcall(function()
		return capture:GetBuffer()
	end)
	if not bufferOk
		or typeof(pngBuffer) ~= "buffer"
		or buffer.len(pngBuffer) < 1
		or buffer.len(pngBuffer) > MAX_SCREENSHOT_BYTES
	then
		adapterError(
			"screenshot_size_unsupported",
			"PNG screenshot buffer is empty or exceeds the response bound",
			false
		)
	end
	local encodeOk, encodedBuffer = pcall(function()
		return EncodingService:Base64Encode(pngBuffer)
	end)
	if not encodeOk or typeof(encodedBuffer) ~= "buffer" then
		adapterError(
			"screenshot_encoding_unavailable",
			"Studio could not encode the PNG screenshot",
			false
		)
	end
	local imageBase64 = buffer.tostring(encodedBuffer)
	if #imageBase64 < 1 or #imageBase64 > 800_000 then
		adapterError(
			"screenshot_encoding_oversize",
			"Encoded screenshot exceeds the response bound",
			false
		)
	end
	return {
		image_base64 = imageBase64,
		mime_type = "image/png",
		width = math.floor(resolution.X),
		height = math.floor(resolution.Y),
	}
end

local function fireInputBinding(args)
	local target = resolveExactPath(args.path, false)
	if not target:IsA("InputBinding")
		or target.Type ~= Enum.InputBindingType.Scriptable
	then
		adapterError(
			"input_target_not_scriptable",
			"Exact target must be an existing Scriptable InputBinding",
			false
		)
	end
	local state = args.state
	if args.state_type == "vector2" then
		state = validateVectorState(args.state, 2)
	elseif args.state_type == "vector3" then
		state = validateVectorState(args.state, 3)
	end
	local fireOk, fireError = pcall(function()
		target:Fire(state)
	end)
	if not fireOk then
		adapterError(
			"input_binding_unavailable",
			"Studio rejected Scriptable InputBinding Fire: "
				.. sanitizeMessage(fireError),
			false
		)
	end
	return {
		fired = true,
		path = copyPath(args.path),
		state_type = args.state_type,
	}
end

local function dispatch(request)
	assertExpectedDocument()
	if request.operation == "studio_get_state" then
		return durableGetState()
	elseif request.operation == "studio_list_tree" then
		return listTree(request.args)
	elseif request.operation == "studio_read_script" then
		return readScript(request.args)
	elseif request.operation == "studio_update_script" then
		return updateScript(request.args)
	elseif request.operation == "studio_set_attribute" then
		return setAttribute(request.args)
	elseif request.operation == "studio_get_console" then
		return getConsole(request.args)
	elseif request.operation == "studio_capture_screenshot" then
		return captureScreenshot(request.args)
	elseif request.operation == "studio_fire_input_binding" then
		return fireInputBinding(request.args)
	elseif request.operation == "studio_start_stop_play" then
		if request.args.is_start then
			return startPlay(request.request_id)
		end
		return stopPlay(request.request_id)
	end
	adapterError("unsupported_operation", "Operation is outside the allowlist", false)
end
