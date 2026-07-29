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
local MAX_TREE_FILTER_BYTES = 100
local MAX_TREE_CURSOR_BYTES = 512
local MAX_TREE_SCAN_LIMIT = 5_000
local DEFAULT_TREE_SCAN_LIMIT = 2_000
local MAX_TREE_PAGE_SIZE = 500
local MAX_TREE_CHILDREN_PER_INSTANCE = 10_000
local MAX_TREE_RETAINED_CHILDREN = 20_000
local MAX_TREE_OUTPUT_BYTES = 600_000
local TREE_COOPERATIVE_YIELD_INTERVAL = 100
local TREE_CURSOR_VERSION = 1
local TREE_SORT_VERSION = "name-class-v1"
local MAX_SCRIPT_QUERY_BYTES = 256
local MAX_SCRIPT_KEYWORDS = 8
local MAX_SCRIPT_KEYWORD_BYTES = 64
local MAX_SCRIPT_CURSOR_BYTES = 2_048
local MAX_SCRIPT_SCAN_LIMIT = 5_000
local DEFAULT_SCRIPT_SCAN_LIMIT = 2_000
local MAX_SCRIPT_SEARCH_PAGE_SIZE = 10
local DEFAULT_SCRIPT_SEARCH_PAGE_SIZE = 10
local MAX_SCRIPT_GREP_PAGE_SIZE = 50
local DEFAULT_SCRIPT_GREP_PAGE_SIZE = 50
local MIN_SCRIPT_TIME_LIMIT_MS = 100
local MAX_SCRIPT_TIME_LIMIT_MS = 10_000
local DEFAULT_SCRIPT_SEARCH_TIME_LIMIT_MS = 3_000
local DEFAULT_SCRIPT_GREP_TIME_LIMIT_MS = 5_000
local MIN_SCRIPT_SOURCE_BUDGET = MAX_SOURCE_BYTES
local MAX_SCRIPT_SOURCE_BUDGET = 4_194_304
local DEFAULT_SCRIPT_SOURCE_BUDGET = 1_048_576
local MAX_SCRIPT_SOURCE_LINES = 20_000
local MAX_SCRIPT_PREVIEW_BYTES = 512
local MAX_SCRIPT_SEARCH_OUTPUT_BYTES = 200_000
local MAX_SCRIPT_GREP_OUTPUT_BYTES = 500_000
local SCRIPT_SEARCH_ITEM_BUDGET = 160_000
local SCRIPT_GREP_ITEM_BUDGET = 450_000
local SCRIPT_CURSOR_VERSION = 1
local SCRIPT_SEARCH_QUERY_VERSION = "script-name-query-v1"
local SCRIPT_GREP_QUERY_VERSION = "script-grep-query-v1"
local SCRIPT_SEARCH_MATCH_SEMANTICS =
	"all_keywords_ascii_case_insensitive_literal_subsequence"

local OPERATION_ARG_KEYS = table.freeze({
	studio_get_state = table.freeze({}),
	studio_list_tree = table.freeze({
		root_path = true,
		max_depth = true,
		max_results = true,
		name_filter = true,
		class_filter = true,
		class_is_a = true,
		scan_limit = true,
		page_size = true,
		continuation_cursor = true,
	}),
	studio_search_scripts = table.freeze({
		keywords = true,
		root_path = true,
		max_depth = true,
		scan_limit = true,
		page_size = true,
		time_limit_ms = true,
		continuation_cursor = true,
	}),
	studio_grep_scripts = table.freeze({
		query = true,
		root_path = true,
		max_depth = true,
		case_sensitive = true,
		scan_limit = true,
		source_byte_limit = true,
		page_size = true,
		time_limit_ms = true,
		continuation_cursor = true,
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

local function validateTreeFilter(value, label)
	if type(value) ~= "string"
		or #value < 1
		or #value > MAX_TREE_FILTER_BYTES
		or string.find(value, "[%c]") ~= nil
	then
		adapterError(
			"invalid_args",
			label .. " must be bounded literal text without controls",
			false
		)
	end
	return value
end

local function validatePrintableAsciiScriptText(value, label)
	if type(value) ~= "string"
		or #value < 1
		or #value > MAX_SCRIPT_QUERY_BYTES
	then
		adapterError(
			"invalid_args",
			label .. " must be 1 to "
				.. tostring(MAX_SCRIPT_QUERY_BYTES)
				.. " printable ASCII bytes",
			false
		)
	end
	for index = 1, #value do
		local byte = string.byte(value, index)
		if byte < 32 or byte > 126 then
			adapterError(
				"invalid_args",
				label .. " must contain printable ASCII only",
				false
			)
		end
	end
	return value
end

local function normalizeScriptKeywords(value)
	validatePrintableAsciiScriptText(value, "keywords")
	local normalized = {}
	local seen = {}
	local startIndex = 1
	while true do
		local commaIndex = string.find(value, ",", startIndex, true)
		local finishIndex = commaIndex ~= nil and commaIndex - 1 or #value
		local token = string.sub(value, startIndex, finishIndex)
		token = string.match(token, "^ *(.-) *$") or ""
		if #token < 1 or #token > MAX_SCRIPT_KEYWORD_BYTES then
			adapterError(
				"invalid_args",
				"Each comma-separated keyword must be 1 to "
					.. tostring(MAX_SCRIPT_KEYWORD_BYTES)
					.. " bytes after ASCII-space trimming",
				false
			)
		end
		local folded = string.lower(token)
		if seen[folded] == true then
			adapterError(
				"invalid_args",
				"keywords must not contain duplicate normalized tokens",
				false
			)
		end
		seen[folded] = true
		table.insert(normalized, folded)
		if #normalized > MAX_SCRIPT_KEYWORDS then
			adapterError(
				"invalid_args",
				"keywords contains too many comma-separated tokens",
				false
			)
		end
		if commaIndex == nil then
			break
		end
		startIndex = commaIndex + 1
		if startIndex > #value then
			adapterError(
				"invalid_args",
				"keywords must not end with an empty token",
				false
			)
		end
	end
	return normalized
end

local function validateScriptCursor(value)
	if type(value) ~= "string"
		or #value < 1
		or #value > MAX_SCRIPT_CURSOR_BYTES
		or string.find(value, "[%c%s]") ~= nil
	then
		adapterError(
			"invalid_args",
			"continuation_cursor is invalid or exceeds its script-search bound",
			false
		)
	end
end

local function validateScriptTraversalArgs(args, maximumPageSize)
	local rootPath = validatePath(args.root_path or {}, true, "root_path")
	if args.max_depth ~= nil then
		validateIntegerRange(args.max_depth, 0, MAX_PATH_SEGMENTS, "max_depth")
	end
	local maxDepth = args.max_depth
		or (MAX_PATH_SEGMENTS - #rootPath)
	if #rootPath + maxDepth > MAX_PATH_SEGMENTS then
		adapterError(
			"invalid_args",
			"root_path plus max_depth exceeds the reusable path bound",
			false
		)
	end
	if args.scan_limit ~= nil then
		validateIntegerRange(
			args.scan_limit,
			1,
			MAX_SCRIPT_SCAN_LIMIT,
			"scan_limit"
		)
	end
	if args.page_size ~= nil then
		validateIntegerRange(
			args.page_size,
			1,
			maximumPageSize,
			"page_size"
		)
	end
	if args.time_limit_ms ~= nil then
		validateIntegerRange(
			args.time_limit_ms,
			MIN_SCRIPT_TIME_LIMIT_MS,
			MAX_SCRIPT_TIME_LIMIT_MS,
			"time_limit_ms"
		)
	end
	if args.continuation_cursor ~= nil then
		validateScriptCursor(args.continuation_cursor)
	end
	return rootPath, maxDepth
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
		local children = current:GetChildren()
		if #children > MAX_TREE_CHILDREN_PER_INSTANCE then
			adapterError(
				"tree_width_unsupported",
				"Exact-path resolution exceeds the bounded child width",
				false
			)
		end
		for _, child in ipairs(children) do
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
		local rootPath = validatePath(
			args.root_path or {},
			true,
			"root_path"
		)
		if args.max_depth ~= nil then
			validateIntegerRange(args.max_depth, 0, 6, "max_depth")
		end
		local maxDepth = args.max_depth or 2
		if #rootPath + maxDepth > MAX_PATH_SEGMENTS then
			adapterError(
				"invalid_args",
				"root_path plus max_depth exceeds the reusable path bound",
				false
			)
		end
		if args.max_results ~= nil then
			validateIntegerRange(args.max_results, 1, 500, "max_results")
		end
		if args.name_filter ~= nil then
			validateTreeFilter(args.name_filter, "name_filter")
		end
		if args.class_filter ~= nil then
			validateTreeFilter(args.class_filter, "class_filter")
			if string.find(args.class_filter, "^[%a_][%w_]*$") == nil then
				adapterError(
					"invalid_args",
					"class_filter must be a literal Roblox class name",
					false
				)
			end
		end
		if args.class_is_a ~= nil
			and type(args.class_is_a) ~= "boolean"
		then
			adapterError("invalid_args", "class_is_a must be boolean", false)
		end
		if args.class_is_a ~= nil and args.class_filter == nil then
			adapterError(
				"invalid_args",
				"class_is_a requires class_filter",
				false
			)
		end
		if args.scan_limit ~= nil then
			validateIntegerRange(
				args.scan_limit,
				1,
				MAX_TREE_SCAN_LIMIT,
				"scan_limit"
			)
		end
		if args.page_size ~= nil then
			validateIntegerRange(
				args.page_size,
				1,
				MAX_TREE_PAGE_SIZE,
				"page_size"
			)
		end
		if args.max_results ~= nil and args.page_size ~= nil then
			adapterError(
				"invalid_args",
				"max_results and page_size are mutually exclusive",
				false
			)
		end
		if args.continuation_cursor ~= nil
			and (type(args.continuation_cursor) ~= "string"
				or #args.continuation_cursor < 1
				or #args.continuation_cursor > MAX_TREE_CURSOR_BYTES
				or string.find(args.continuation_cursor, "[%c%s]") ~= nil)
		then
			adapterError(
				"invalid_args",
				"continuation_cursor is invalid or exceeds its bound",
				false
			)
		end
	elseif operation == "studio_search_scripts" then
		normalizeScriptKeywords(args.keywords)
		validateScriptTraversalArgs(
			args,
			MAX_SCRIPT_SEARCH_PAGE_SIZE
		)
	elseif operation == "studio_grep_scripts" then
		validatePrintableAsciiScriptText(args.query, "query")
		validateScriptTraversalArgs(
			args,
			MAX_SCRIPT_GREP_PAGE_SIZE
		)
		if args.case_sensitive ~= nil
			and type(args.case_sensitive) ~= "boolean"
		then
			adapterError(
				"invalid_args",
				"case_sensitive must be boolean",
				false
			)
		end
		if args.source_byte_limit ~= nil then
			validateIntegerRange(
				args.source_byte_limit,
				MIN_SCRIPT_SOURCE_BUDGET,
				MAX_SCRIPT_SOURCE_BUDGET,
				"source_byte_limit"
			)
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
	local controllerMode = modeSnapshot()
	local mode, modeSource = observedMode(play, controllerMode)
	return {
		adapter = "studio-mcp-v2-durable-plugin",
		source = "studio_controller",
		connected = true,
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
		mode_source = modeSource,
		raw_mode_predicates = controllerMode.raw_mode_predicates,
		controller_context = {
			role = "edit_controller",
			datamodel_type = "Edit",
			request_channel_available = true,
		},
		available_datamodel_types = { "Edit" },
		play = play,
	}
end

local TREE_CURSOR_KEYS = table.freeze({
	v = true,
	s = true,
	d = true,
	g = true,
	q = true,
	p = true,
	l = true,
})

local function appendCanonicalTreeValue(parts, value)
	local text = tostring(value)
	table.insert(parts, tostring(#text))
	table.insert(parts, ":")
	table.insert(parts, text)
	table.insert(parts, ";")
end

local function treeSha256(value)
	if EncodingService == nil then
		adapterError(
			"tree_cursor_unavailable",
			"Studio SHA-256 support is unavailable for tree pagination",
			false
		)
	end
	local ok, binaryDigest = pcall(function()
		return EncodingService:ComputeStringHash(
			value,
			Enum.HashAlgorithm.Sha256
		)
	end)
	if not ok
		or type(binaryDigest) ~= "string"
		or #binaryDigest ~= 32
	then
		adapterError(
			"tree_cursor_unavailable",
			"Studio could not compute a tree cursor digest",
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

local function scriptCursorSha256(value)
	if EncodingService == nil then
		adapterError(
			"script_cursor_unavailable",
			"Studio SHA-256 support is unavailable for script pagination",
			false
		)
	end
	local ok, binaryDigest = pcall(function()
		return EncodingService:ComputeStringHash(
			value,
			Enum.HashAlgorithm.Sha256
		)
	end)
	if not ok
		or type(binaryDigest) ~= "string"
		or #binaryDigest ~= 32
	then
		adapterError(
			"script_cursor_unavailable",
			"Studio could not compute a script cursor digest",
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

local function treeCursorIntegrity(payload)
	return treeSha256(
		REGISTRATION_SECRET
			.. "\0studio-tree-cursor-v1\0"
			.. payload
			.. "\0"
			.. REGISTRATION_SECRET
	)
end

local function constantTimeTextEqual(left, right)
	if type(left) ~= "string"
		or type(right) ~= "string"
		or #left ~= #right
	then
		return false
	end
	local different = 0
	for index = 1, #left do
		if string.byte(left, index) ~= string.byte(right, index) then
			different = 1
		end
	end
	return different == 0
end

local function encodeTreeCursorPayload(payload)
	local jsonOk, json = pcall(function()
		return HttpService:JSONEncode(payload)
	end)
	if not jsonOk or type(json) ~= "string" or #json > 1_024 then
		adapterError(
			"tree_cursor_unavailable",
			"Tree cursor state could not be encoded within its bound",
			false
		)
	end
	local base64Ok, encodedBuffer = pcall(function()
		return EncodingService:Base64Encode(buffer.fromstring(json))
	end)
	if not base64Ok or typeof(encodedBuffer) ~= "buffer" then
		adapterError(
			"tree_cursor_unavailable",
			"Studio could not encode tree cursor state",
			false
		)
	end
	local encoded = buffer.tostring(encodedBuffer)
	local cursor = encoded .. "." .. treeCursorIntegrity(json)
	if #cursor > MAX_TREE_CURSOR_BYTES then
		adapterError(
			"tree_cursor_unavailable",
			"Encoded tree cursor exceeds its durable bound",
			false
		)
	end
	return cursor
end

local function decodeTreeCursorPayload(cursor, querySha256, maxDepth)
	local encodedCore, padding, suppliedIntegrity = string.match(
		cursor,
		"^([A-Za-z0-9+/]+)(=*)%.([0-9a-f]+)$"
	)
	if encodedCore == nil
		or padding == nil
		or #padding > 2
		or suppliedIntegrity == nil
		or #suppliedIntegrity ~= 64
	then
		adapterError(
			"invalid_continuation_cursor",
			"Tree continuation cursor encoding is invalid",
			false
		)
	end
	local encoded = encodedCore .. padding
	local decodeOk, decodedBuffer = pcall(function()
		return EncodingService:Base64Decode(buffer.fromstring(encoded))
	end)
	if not decodeOk or typeof(decodedBuffer) ~= "buffer" then
		adapterError(
			"invalid_continuation_cursor",
			"Tree continuation cursor payload is invalid",
			false
		)
	end
	local json = buffer.tostring(decodedBuffer)
	if #json < 1 or #json > 1_024 then
		adapterError(
			"invalid_continuation_cursor",
			"Tree continuation cursor payload exceeds its bound",
			false
		)
	end
	if not constantTimeTextEqual(
		suppliedIntegrity,
		treeCursorIntegrity(json)
	) then
		adapterError(
			"invalid_continuation_cursor",
			"Tree continuation cursor integrity check failed",
			false
		)
	end
	local payloadOk, payload = pcall(function()
		return HttpService:JSONDecode(json)
	end)
	if not payloadOk or type(payload) ~= "table" then
		adapterError(
			"invalid_continuation_cursor",
			"Tree continuation cursor JSON is invalid",
			false
		)
	end
	for key in pairs(payload) do
		if type(key) ~= "string" or TREE_CURSOR_KEYS[key] ~= true then
			adapterError(
				"invalid_continuation_cursor",
				"Tree continuation cursor contains an unexpected field",
				false
			)
		end
	end
	for key in pairs(TREE_CURSOR_KEYS) do
		if payload[key] == nil then
			adapterError(
				"invalid_continuation_cursor",
				"Tree continuation cursor is missing required state",
				false
			)
		end
	end
	if payload.v ~= TREE_CURSOR_VERSION
		or payload.s ~= peer.studio_id
		or payload.d ~= DOCUMENT_EPOCH
		or payload.g ~= peer.generation
	then
		adapterError(
			"stale_continuation_cursor",
			"Tree continuation cursor belongs to another session generation",
			false
		)
	end
	if payload.q ~= querySha256 then
		adapterError(
			"continuation_cursor_query_mismatch",
			"Tree continuation cursor does not match this query",
			false
		)
	end
	if type(payload.l) ~= "string"
		or #payload.l ~= 64
		or string.find(payload.l, "[^0-9a-f]") ~= nil
		or type(payload.p) ~= "table"
		or #payload.p > maxDepth
	then
		adapterError(
			"invalid_continuation_cursor",
			"Tree continuation cursor traversal state is invalid",
			false
		)
	end
	local positionCount = #payload.p
	for key in pairs(payload.p) do
		if not isInteger(key) or key < 1 or key > positionCount then
			adapterError(
				"invalid_continuation_cursor",
				"Tree continuation position must be a dense array",
				false
			)
		end
	end
	for _, ordinal in ipairs(payload.p) do
		if not isInteger(ordinal)
			or ordinal < 1
			or ordinal > MAX_TREE_CHILDREN_PER_INSTANCE
		then
			adapterError(
				"invalid_continuation_cursor",
				"Tree continuation child ordinal is invalid",
				false
			)
		end
	end
	return payload.p, payload.l
end

local function canonicalTreeQuery(
	rootPath,
	maxDepth,
	nameFilter,
	classFilter,
	classIsA,
	scanLimit,
	pageSize
)
	local parts = {}
	appendCanonicalTreeValue(parts, "studio-tree-query-v1")
	appendCanonicalTreeValue(parts, TREE_SORT_VERSION)
	appendCanonicalTreeValue(parts, maxDepth)
	appendCanonicalTreeValue(parts, string.lower(nameFilter or ""))
	appendCanonicalTreeValue(parts, classFilter or "")
	appendCanonicalTreeValue(parts, classIsA and "1" or "0")
	appendCanonicalTreeValue(parts, scanLimit)
	appendCanonicalTreeValue(parts, pageSize)
	appendCanonicalTreeValue(parts, #rootPath)
	for _, segment in ipairs(rootPath) do
		appendCanonicalTreeValue(parts, segment)
	end
	return table.concat(parts)
end

local function sortedTreeChildren(instance)
	local children = instance:GetChildren()
	if #children > MAX_TREE_CHILDREN_PER_INSTANCE then
		adapterError(
			"tree_width_unsupported",
			"One tree node exceeds the deterministic child-order bound",
			false
		)
	end
	local decorated = table.create(#children)
	for index, child in ipairs(children) do
		if type(child.Name) ~= "string"
			or #child.Name < 1
			or #child.Name > MAX_PATH_SEGMENT_BYTES
			or string.find(child.Name, "[%c]") ~= nil
		then
			adapterError(
				"tree_name_unsupported",
				"One tree child name cannot form a reusable exact path",
				false
			)
		end
		decorated[index] = {
			instance = child,
			original_index = index,
		}
	end
	table.sort(decorated, function(left, right)
		local leftInstance = left.instance
		local rightInstance = right.instance
		if leftInstance.Name == rightInstance.Name then
			if leftInstance.ClassName == rightInstance.ClassName then
				return left.original_index < right.original_index
			end
			return leftInstance.ClassName < rightInstance.ClassName
		end
		return leftInstance.Name < rightInstance.Name
	end)
	for index = 2, #decorated do
		local previous = decorated[index - 1].instance
		local current = decorated[index].instance
		if previous.Name == current.Name then
			adapterError(
				"ambiguous_tree_order",
				"Sibling instances with duplicate names cannot produce "
					.. "reusable exact paths",
				false
			)
		end
	end
	local sorted = table.create(#decorated)
	for index, entry in ipairs(decorated) do
		sorted[index] = entry.instance
	end
	return sorted
end

local function boundedTreeChildCount(instance)
	local children = instance:GetChildren()
	if #children > MAX_TREE_CHILDREN_PER_INSTANCE then
		adapterError(
			"tree_width_unsupported",
			"One tree node exceeds the deterministic child-count bound",
			false
		)
	end
	return #children
end

local function treePositionLineage(root, position)
	local parts = {}
	appendCanonicalTreeValue(parts, "studio-tree-lineage-v1")
	appendCanonicalTreeValue(parts, TREE_SORT_VERSION)
	appendCanonicalTreeValue(parts, root.Name)
	appendCanonicalTreeValue(parts, root.ClassName)
	local current = root
	for _, ordinal in ipairs(position) do
		local children = sortedTreeChildren(current)
		if ordinal > #children then
			adapterError(
				"stale_continuation_cursor",
				"Tree continuation location no longer exists",
				false
			)
		end
		appendCanonicalTreeValue(parts, #children)
		appendCanonicalTreeValue(parts, ordinal)
		local first = math.max(1, ordinal - 2)
		local last = math.min(#children, ordinal + 2)
		appendCanonicalTreeValue(parts, first)
		appendCanonicalTreeValue(parts, last)
		for index = first, last do
			local child = children[index]
			appendCanonicalTreeValue(parts, index)
			appendCanonicalTreeValue(parts, child.Name)
			appendCanonicalTreeValue(parts, child.ClassName)
		end
		current = children[ordinal]
	end
	appendCanonicalTreeValue(parts, current.Name)
	appendCanonicalTreeValue(parts, current.ClassName)
	return treeSha256(table.concat(parts))
end

local function assertTreeFrontierBound(frames, newChildren)
	local retained = #newChildren
	for _, frame in ipairs(frames) do
		if frame.children ~= nil then
			retained = retained + #frame.children
		end
	end
	if retained > MAX_TREE_RETAINED_CHILDREN then
		adapterError(
			"tree_frontier_unsupported",
			"Deterministic traversal exceeds the retained-child frontier bound",
			false
		)
	end
end

local function newTreeTraversal(root, rootPath, position, maxDepth)
	local frames = {}
	local current = root
	local currentPath = copyPath(rootPath)
	local currentPosition = {}
	for level, ordinal in ipairs(position) do
		if level > maxDepth then
			adapterError(
				"invalid_continuation_cursor",
				"Tree continuation exceeds max_depth",
				false
			)
		end
		local children = sortedTreeChildren(current)
		assertTreeFrontierBound(frames, children)
		if ordinal > #children then
			adapterError(
				"stale_continuation_cursor",
				"Tree continuation location no longer exists",
				false
			)
		end
		table.insert(frames, {
			instance = current,
			path = copyPath(currentPath),
			position = copyPath(currentPosition),
			depth = level - 1,
			emitted = true,
			children = children,
			next_child = ordinal + 1,
		})
		table.insert(currentPosition, ordinal)
		current = children[ordinal]
		currentPath = appendPath(currentPath, current.Name)
	end
	table.insert(frames, {
		instance = current,
		path = copyPath(currentPath),
		position = copyPath(currentPosition),
		depth = #position,
		emitted = false,
		children = nil,
		next_child = 1,
	})
	return frames
end

local function nextTreeNode(frames, maxDepth)
	while #frames > 0 do
		local frame = frames[#frames]
		if not frame.emitted then
			frame.emitted = true
			return frame.instance,
				frame.path,
				frame.depth,
				frame.position
		end
		if frame.depth < maxDepth then
			if frame.children == nil then
				local children = sortedTreeChildren(frame.instance)
				assertTreeFrontierBound(frames, children)
				frame.children = children
			end
			local ordinal = frame.next_child
			if ordinal <= #frame.children then
				frame.next_child = ordinal + 1
				local child = frame.children[ordinal]
				table.insert(frames, {
					instance = child,
					path = appendPath(frame.path, child.Name),
					position = appendPath(frame.position, ordinal),
					depth = frame.depth + 1,
					emitted = false,
					children = nil,
					next_child = 1,
				})
			else
				table.remove(frames)
			end
		else
			table.remove(frames)
		end
	end
	return nil, nil, nil, nil
end

local function treeInstanceMatches(
	instance,
	nameFilterLower,
	classFilter,
	classIsA
)
	if nameFilterLower ~= nil
		and string.find(
			string.lower(instance.Name),
			nameFilterLower,
			1,
			true
		) == nil
	then
		return false
	end
	if classFilter == nil then
		return true
	end
	if not classIsA then
		return instance.ClassName == classFilter
	end
	local ok, matches = pcall(function()
		return instance:IsA(classFilter)
	end)
	if not ok then
		adapterError(
			"invalid_class_filter",
			"Studio rejected class_filter for IsA matching",
			false
		)
	end
	return matches == true
end

local function listTree(args)
	local rootPath = args.root_path or {}
	local root = resolveExactPath(rootPath, true)
	local maxDepth = args.max_depth or 2
	local pageSize = args.page_size or args.max_results or 200
	local scanLimit = args.scan_limit or DEFAULT_TREE_SCAN_LIMIT
	local nameFilter = args.name_filter
	local nameFilterLower = nameFilter ~= nil
		and string.lower(nameFilter) or nil
	local classFilter = args.class_filter
	local classIsA = args.class_is_a == true
	if classFilter ~= nil and classIsA then
		local classOk = pcall(function()
			return root:IsA(classFilter)
		end)
		if not classOk then
			adapterError(
				"invalid_class_filter",
				"Studio rejected class_filter for IsA matching",
				false
			)
		end
	end
	local queryCanonical = canonicalTreeQuery(
		rootPath,
		maxDepth,
		nameFilter,
		classFilter,
		classIsA,
		scanLimit,
		pageSize
	)
	local querySha256 = nil
	local startPosition = {}
	local expectedLineage = nil
	if args.continuation_cursor ~= nil then
		querySha256 = treeSha256(queryCanonical)
		startPosition, expectedLineage = decodeTreeCursorPayload(
			args.continuation_cursor,
			querySha256,
			maxDepth
		)
		local observedLineage = treePositionLineage(root, startPosition)
		if not constantTimeTextEqual(expectedLineage, observedLineage) then
			adapterError(
				"stale_continuation_cursor",
				"Tree changed at the continuation location",
				false
			)
		end
	end

	local frames = newTreeTraversal(
		root,
		rootPath,
		startPosition,
		maxDepth
	)
	local items = {}
	local scanned = 0
	local outputBytes = 0
	local nextPosition = nil
	local truncationReason = nil

	while true do
		local instance, path, _, position = nextTreeNode(frames, maxDepth)
		if instance == nil then
			break
		end
		scanned = scanned + 1
		if treeInstanceMatches(
			instance,
			nameFilterLower,
			classFilter,
			classIsA
		) then
				local item = {
					path = copyPath(path),
					name = instance == game and game.Name or instance.Name,
					class_name = instance.ClassName,
					child_count = boundedTreeChildCount(instance),
				}
			local itemJsonOk, itemJson = pcall(function()
				return HttpService:JSONEncode(item)
			end)
			if not itemJsonOk or type(itemJson) ~= "string" then
				adapterError(
					"tree_encoding_unavailable",
					"Studio could not encode a bounded tree item",
					false
				)
			end
			local encodedBytes = #itemJson + 1
			if outputBytes + encodedBytes > MAX_TREE_OUTPUT_BYTES then
				nextPosition = copyPath(position)
				truncationReason = "output_bytes"
				break
			end
			table.insert(items, item)
			outputBytes = outputBytes + encodedBytes
		end

		if #items >= pageSize or scanned >= scanLimit then
			local _, _, _, followingPosition =
				nextTreeNode(frames, maxDepth)
			if followingPosition ~= nil then
				nextPosition = copyPath(followingPosition)
				truncationReason = #items >= pageSize
					and "page_size" or "scan_limit"
			end
			break
		end
		if scanned % TREE_COOPERATIVE_YIELD_INTERVAL == 0 then
			task.wait()
			assertExpectedDocument()
		end
	end

	assertExpectedDocument()
	local continuationCursor = nil
	if nextPosition ~= nil then
		querySha256 = querySha256 or treeSha256(queryCanonical)
		continuationCursor = encodeTreeCursorPayload({
			v = TREE_CURSOR_VERSION,
			s = peer.studio_id,
			d = DOCUMENT_EPOCH,
			g = peer.generation,
			q = querySha256,
			p = nextPosition,
			l = treePositionLineage(root, nextPosition),
		})
	end
	return {
		root_path = copyPath(rootPath),
		items = items,
		truncated = continuationCursor ~= nil,
		has_more = continuationCursor ~= nil,
		continuation_cursor = continuationCursor,
		truncation_reason = truncationReason,
		max_depth = maxDepth,
		max_results = pageSize,
		page_size = pageSize,
		scan_limit = scanLimit,
		scanned = scanned,
		returned = #items,
		output_bytes = outputBytes,
		name_filter = nameFilter,
		class_filter = classFilter,
		class_is_a = classIsA,
		sort_version = TREE_SORT_VERSION,
	}
end

local SCRIPT_SEARCH_CURSOR_KEYS = table.freeze({
	v = true,
	s = true,
	c = true,
	d = true,
	g = true,
	q = true,
	p = true,
	l = true,
})

local SCRIPT_GREP_CURSOR_KEYS = table.freeze({
	v = true,
	s = true,
	c = true,
	d = true,
	g = true,
	q = true,
	p = true,
	l = true,
	b = true,
	h = true,
})

local function scriptCursorIntegrity(domain, payload)
	return scriptCursorSha256(
		REGISTRATION_SECRET
			.. "\0"
			.. domain
			.. "\0"
			.. payload
			.. "\0"
			.. REGISTRATION_SECRET
	)
end

local function encodeScriptCursorPayload(payload, domain)
	local jsonOk, json = pcall(function()
		return HttpService:JSONEncode(payload)
	end)
	if not jsonOk or type(json) ~= "string" or #json > 1_400 then
		adapterError(
			"script_cursor_unavailable",
			"Script continuation state could not be encoded within its bound",
			false
		)
	end
	local base64Ok, encodedBuffer = pcall(function()
		return EncodingService:Base64Encode(buffer.fromstring(json))
	end)
	if not base64Ok or typeof(encodedBuffer) ~= "buffer" then
		adapterError(
			"script_cursor_unavailable",
			"Studio could not encode script continuation state",
			false
		)
	end
	local encoded = buffer.tostring(encodedBuffer)
	local cursor = encoded .. "." .. scriptCursorIntegrity(domain, json)
	if #cursor > MAX_SCRIPT_CURSOR_BYTES then
		adapterError(
			"script_cursor_unavailable",
			"Encoded script continuation exceeds its durable bound",
			false
		)
	end
	return cursor
end

local function decodeScriptCursorPayload(
	cursor,
	domain,
	allowedKeys,
	querySha256,
	maxDepth
)
	local encodedCore, padding, suppliedIntegrity = string.match(
		cursor,
		"^([A-Za-z0-9+/]+)(=*)%.([0-9a-f]+)$"
	)
	if encodedCore == nil
		or padding == nil
		or #padding > 2
		or suppliedIntegrity == nil
		or #suppliedIntegrity ~= 64
	then
		adapterError(
			"invalid_script_cursor",
			"Script continuation cursor encoding is invalid",
			false
		)
	end
	local decodeOk, decodedBuffer = pcall(function()
		return EncodingService:Base64Decode(
			buffer.fromstring(encodedCore .. padding)
		)
	end)
	if not decodeOk or typeof(decodedBuffer) ~= "buffer" then
		adapterError(
			"invalid_script_cursor",
			"Script continuation cursor payload is invalid",
			false
		)
	end
	local json = buffer.tostring(decodedBuffer)
	if #json < 1 or #json > 1_400 then
		adapterError(
			"invalid_script_cursor",
			"Script continuation cursor payload exceeds its bound",
			false
		)
	end
	if not constantTimeTextEqual(
		suppliedIntegrity,
		scriptCursorIntegrity(domain, json)
	) then
		adapterError(
			"invalid_script_cursor",
			"Script continuation cursor integrity check failed",
			false
		)
	end
	local payloadOk, payload = pcall(function()
		return HttpService:JSONDecode(json)
	end)
	if not payloadOk or type(payload) ~= "table" then
		adapterError(
			"invalid_script_cursor",
			"Script continuation cursor JSON is invalid",
			false
		)
	end
	for key in pairs(payload) do
		if type(key) ~= "string" or allowedKeys[key] ~= true then
			adapterError(
				"invalid_script_cursor",
				"Script continuation cursor contains an unexpected field",
				false
			)
		end
	end
	for key in pairs(allowedKeys) do
		if payload[key] == nil then
			adapterError(
				"invalid_script_cursor",
				"Script continuation cursor is missing required state",
				false
			)
		end
	end
	if payload.v ~= SCRIPT_CURSOR_VERSION
		or payload.s ~= peer.studio_id
		or payload.c ~= CLIENT_INSTANCE_ID
		or payload.d ~= DOCUMENT_EPOCH
		or payload.g ~= peer.generation
	then
		adapterError(
			"stale_script_cursor",
			"Script continuation belongs to another session generation",
			false
		)
	end
	if payload.q ~= querySha256 then
		adapterError(
			"script_cursor_query_mismatch",
			"Script continuation does not match this normalized query",
			false
		)
	end
	if type(payload.l) ~= "string"
		or #payload.l ~= 64
		or string.find(payload.l, "[^0-9a-f]") ~= nil
		or type(payload.p) ~= "table"
		or #payload.p > maxDepth
	then
		adapterError(
			"invalid_script_cursor",
			"Script continuation traversal state is invalid",
			false
		)
	end
	local positionCount = #payload.p
	for key in pairs(payload.p) do
		if not isInteger(key) or key < 1 or key > positionCount then
			adapterError(
				"invalid_script_cursor",
				"Script continuation position must be a dense array",
				false
			)
		end
	end
	for _, ordinal in ipairs(payload.p) do
		if not isInteger(ordinal)
			or ordinal < 1
			or ordinal > MAX_TREE_CHILDREN_PER_INSTANCE
		then
			adapterError(
				"invalid_script_cursor",
				"Script continuation child ordinal is invalid",
				false
			)
		end
	end
	return payload
end

local function canonicalScriptSearchQuery(
	rootPath,
	maxDepth,
	keywords,
	scanLimit,
	pageSize,
	timeLimitMs
)
	local parts = {}
	appendCanonicalTreeValue(parts, SCRIPT_SEARCH_QUERY_VERSION)
	appendCanonicalTreeValue(parts, TREE_SORT_VERSION)
	appendCanonicalTreeValue(parts, maxDepth)
	appendCanonicalTreeValue(parts, scanLimit)
	appendCanonicalTreeValue(parts, pageSize)
	appendCanonicalTreeValue(parts, timeLimitMs)
	appendCanonicalTreeValue(parts, #rootPath)
	for _, segment in ipairs(rootPath) do
		appendCanonicalTreeValue(parts, segment)
	end
	appendCanonicalTreeValue(parts, #keywords)
	for _, keyword in ipairs(keywords) do
		appendCanonicalTreeValue(parts, keyword)
	end
	return table.concat(parts)
end

local function canonicalScriptGrepQuery(
	rootPath,
	maxDepth,
	query,
	caseSensitive,
	scanLimit,
	sourceByteLimit,
	pageSize,
	timeLimitMs
)
	local parts = {}
	appendCanonicalTreeValue(parts, SCRIPT_GREP_QUERY_VERSION)
	appendCanonicalTreeValue(parts, TREE_SORT_VERSION)
	appendCanonicalTreeValue(parts, maxDepth)
	appendCanonicalTreeValue(parts, query)
	appendCanonicalTreeValue(parts, caseSensitive and "1" or "0")
	appendCanonicalTreeValue(parts, scanLimit)
	appendCanonicalTreeValue(parts, sourceByteLimit)
	appendCanonicalTreeValue(parts, pageSize)
	appendCanonicalTreeValue(parts, timeLimitMs)
	appendCanonicalTreeValue(parts, #rootPath)
	for _, segment in ipairs(rootPath) do
		appendCanonicalTreeValue(parts, segment)
	end
	return table.concat(parts)
end

local function scriptCursorForPosition(
	root,
	position,
	querySha256,
	domain,
	extra
)
	local payload = {
		v = SCRIPT_CURSOR_VERSION,
		s = peer.studio_id,
		c = CLIENT_INSTANCE_ID,
		d = DOCUMENT_EPOCH,
		g = peer.generation,
		q = querySha256,
		p = copyPath(position),
		l = treePositionLineage(root, position),
	}
	if extra ~= nil then
		for key, value in pairs(extra) do
			payload[key] = value
		end
	end
	return encodeScriptCursorPayload(payload, domain)
end

local function isPlaceScript(instance)
	return instance.ClassName == "Script"
		or instance.ClassName == "LocalScript"
		or instance.ClassName == "ModuleScript"
end

local function nameMatchesScriptKeywords(name, keywords)
	local foldedName = string.lower(name)
	for _, keyword in ipairs(keywords) do
		local nextIndex = 1
		for keywordIndex = 1, #keyword do
			local byteText = string.sub(
				keyword,
				keywordIndex,
				keywordIndex
			)
			local found = string.find(
				foldedName,
				byteText,
				nextIndex,
				true
			)
			if found == nil then
				return false
			end
			nextIndex = found + 1
		end
	end
	return true
end

local function newScriptResult(operation, requestId)
	return {
		adapter = "studio-mcp-v2-durable-plugin",
		v = 1,
		operation = operation,
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		request_id = requestId,
	}
end

local function assertBoundedScriptResult(result, maximumBytes)
	local encodedOk, encoded = pcall(function()
		return HttpService:JSONEncode(result)
	end)
	if not encodedOk
		or type(encoded) ~= "string"
		or #encoded > maximumBytes
	then
		adapterError(
			"script_result_oversize",
			"Script query result exceeds its encoded output bound",
			false
		)
	end
end

local function searchScripts(args, requestId)
	requireEditMode()
	local startedAt = os.clock()
	local rootPath, maxDepth = validateScriptTraversalArgs(
		args,
		MAX_SCRIPT_SEARCH_PAGE_SIZE
	)
	local root = resolveExactPath(rootPath, true)
	local keywords = normalizeScriptKeywords(args.keywords)
	local scanLimit = args.scan_limit or DEFAULT_SCRIPT_SCAN_LIMIT
	local pageSize = args.page_size or DEFAULT_SCRIPT_SEARCH_PAGE_SIZE
	local timeLimitMs = args.time_limit_ms
		or DEFAULT_SCRIPT_SEARCH_TIME_LIMIT_MS
	local queryCanonical = canonicalScriptSearchQuery(
		rootPath,
		maxDepth,
		keywords,
		scanLimit,
		pageSize,
		timeLimitMs
	)
	local querySha256 = scriptCursorSha256(queryCanonical)
	local startPosition = {}
	if args.continuation_cursor ~= nil then
		local payload = decodeScriptCursorPayload(
			args.continuation_cursor,
			"studio-script-search-cursor-v1",
			SCRIPT_SEARCH_CURSOR_KEYS,
			querySha256,
			maxDepth
		)
		startPosition = payload.p
		local observedLineage = treePositionLineage(root, startPosition)
		if not constantTimeTextEqual(payload.l, observedLineage) then
			adapterError(
				"stale_script_cursor",
				"Script search tree changed at the continuation location",
				false
			)
		end
	end

	local frames = newTreeTraversal(
		root,
		rootPath,
		startPosition,
		maxDepth
	)
	local items = {}
	local itemBytes = 0
	local scannedInstances = 0
	local scannedScripts = 0
	local continuationCursor = ""
	local truncationReason = "complete"

	while true do
		local instance, path, _, position = nextTreeNode(frames, maxDepth)
		if instance == nil then
			break
		end
		if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
			continuationCursor = scriptCursorForPosition(
				root,
				position,
				querySha256,
				"studio-script-search-cursor-v1",
				nil
			)
			truncationReason = "time_budget"
			break
		end
		scannedInstances = scannedInstances + 1
		if isPlaceScript(instance) then
			scannedScripts = scannedScripts + 1
			if nameMatchesScriptKeywords(instance.Name, keywords) then
				local item = {
					path = copyPath(path),
					name = instance.Name,
					class_name = instance.ClassName,
				}
				local itemJsonOk, itemJson = pcall(function()
					return HttpService:JSONEncode(item)
				end)
				if not itemJsonOk or type(itemJson) ~= "string" then
					adapterError(
						"script_result_encoding_unavailable",
						"Studio could not encode a script-search item",
						false
					)
				end
				if itemBytes + #itemJson + 1
					> SCRIPT_SEARCH_ITEM_BUDGET
				then
					continuationCursor = scriptCursorForPosition(
						root,
						position,
						querySha256,
						"studio-script-search-cursor-v1",
						nil
					)
					truncationReason = "output_bytes"
					break
				end
				table.insert(items, item)
				itemBytes = itemBytes + #itemJson + 1
				if #items >= pageSize then
					local _, _, _, followingPosition =
						nextTreeNode(frames, maxDepth)
					if followingPosition ~= nil then
						continuationCursor = scriptCursorForPosition(
							root,
							followingPosition,
							querySha256,
							"studio-script-search-cursor-v1",
							nil
						)
						truncationReason = "page_size"
					end
					break
				end
			end
		end

		if scannedInstances >= scanLimit then
			local _, _, _, followingPosition =
				nextTreeNode(frames, maxDepth)
			if followingPosition ~= nil then
				continuationCursor = scriptCursorForPosition(
					root,
					followingPosition,
					querySha256,
					"studio-script-search-cursor-v1",
					nil
				)
				truncationReason = "scan_limit"
			end
			break
		end
		if scannedInstances % TREE_COOPERATIVE_YIELD_INTERVAL == 0 then
			task.wait()
			assertExpectedDocument()
			requireEditMode()
		end
	end

	assertExpectedDocument()
	requireEditMode()
	local result = newScriptResult("studio_search_scripts", requestId)
	result.root_path = copyPath(rootPath)
	result.keywords = copyPath(keywords)
	result.match_semantics = SCRIPT_SEARCH_MATCH_SEMANTICS
	result.query_version = SCRIPT_SEARCH_QUERY_VERSION
	result.sort_version = TREE_SORT_VERSION
	result.max_depth = maxDepth
	result.scan_limit = scanLimit
	result.page_size = pageSize
	result.time_limit_ms = timeLimitMs
	result.scanned_instances = scannedInstances
	result.scanned_scripts = scannedScripts
	result.returned = #items
	result.items = items
	result.truncated = continuationCursor ~= ""
	result.has_more = continuationCursor ~= ""
	result.continuation_cursor = continuationCursor
	result.truncation_reason = truncationReason
	result.output_limit_bytes = MAX_SCRIPT_SEARCH_OUTPUT_BYTES
	assertBoundedScriptResult(result, MAX_SCRIPT_SEARCH_OUTPUT_BYTES)
	return result
end

local function readGrepSource(target)
	if ScriptEditorService == nil then
		adapterError(
			"script_editor_unavailable",
			"ScriptEditorService is unavailable for bounded grep",
			false
		)
	end
	local ok, source = pcall(function()
		return ScriptEditorService:GetEditorSource(target)
	end)
	if not ok or type(source) ~= "string" then
		adapterError(
			"script_source_unavailable",
			"One script source is unavailable; grep cannot claim completeness",
			false
		)
	end
	if #source > MAX_SOURCE_BYTES then
		adapterError(
			"script_source_oversize",
			"One script source exceeds the durable per-script grep bound",
			false
		)
	end
	if utf8.len(source) == nil then
		adapterError(
			"script_source_invalid_utf8",
			"One script source is not valid UTF-8",
			false
		)
	end
	local lineCount = 1
	local nextIndex = 1
	while true do
		local newline = string.find(source, "\n", nextIndex, true)
		if newline == nil then
			break
		end
		lineCount = lineCount + 1
		if lineCount > MAX_SCRIPT_SOURCE_LINES then
			adapterError(
				"script_source_line_limit",
				"One script source exceeds the bounded logical-line count",
				false
			)
		end
		nextIndex = newline + 1
	end
	return source
end

local function sourceLineStateAt(source, bytePosition)
	local lineNumber = 1
	local lineStart = 1
	while true do
		local newline = string.find(source, "\n", lineStart, true)
		if newline == nil or newline >= bytePosition then
			return lineNumber, lineStart, newline
		end
		lineNumber = lineNumber + 1
		lineStart = newline + 1
	end
end

local function byteIsUtf8Continuation(value)
	return value ~= nil and value >= 128 and value <= 191
end

local function grepPreview(
	source,
	lineStart,
	lineEnd,
	matchStart,
	matchEnd
)
	local roomBefore = math.floor(
		(MAX_SCRIPT_PREVIEW_BYTES - (matchEnd - matchStart + 1)) / 2
	)
	local previewStart = math.max(lineStart, matchStart - roomBefore)
	while previewStart > lineStart
		and byteIsUtf8Continuation(string.byte(source, previewStart))
	do
		previewStart = previewStart - 1
	end
	local previewEnd = math.min(
		lineEnd,
		previewStart + MAX_SCRIPT_PREVIEW_BYTES - 1
	)
	while previewEnd >= previewStart
		and previewEnd < lineEnd
		and byteIsUtf8Continuation(string.byte(source, previewEnd + 1))
	do
		previewEnd = previewEnd - 1
	end
	if previewStart > matchStart or previewEnd < matchEnd then
		adapterError(
			"script_preview_unavailable",
			"Studio could not form a bounded UTF-8 grep preview",
			false
		)
	end
	return previewStart,
		string.sub(source, previewStart, previewEnd),
		previewStart > lineStart,
		previewEnd < lineEnd
end

local function grepScripts(args, requestId)
	requireEditMode()
	local startedAt = os.clock()
	local rootPath, maxDepth = validateScriptTraversalArgs(
		args,
		MAX_SCRIPT_GREP_PAGE_SIZE
	)
	local root = resolveExactPath(rootPath, true)
	local query = validatePrintableAsciiScriptText(args.query, "query")
	local caseSensitive = args.case_sensitive ~= false
	local scanLimit = args.scan_limit or DEFAULT_SCRIPT_SCAN_LIMIT
	local sourceByteLimit = args.source_byte_limit
		or DEFAULT_SCRIPT_SOURCE_BUDGET
	local pageSize = args.page_size or DEFAULT_SCRIPT_GREP_PAGE_SIZE
	local timeLimitMs = args.time_limit_ms
		or DEFAULT_SCRIPT_GREP_TIME_LIMIT_MS
	local queryCanonical = canonicalScriptGrepQuery(
		rootPath,
		maxDepth,
		query,
		caseSensitive,
		scanLimit,
		sourceByteLimit,
		pageSize,
		timeLimitMs
	)
	local querySha256 = scriptCursorSha256(queryCanonical)
	local startPosition = {}
	local resumeByte = 1
	local resumeSourceSha256 = ""
	if args.continuation_cursor ~= nil then
		local payload = decodeScriptCursorPayload(
			args.continuation_cursor,
			"studio-script-grep-cursor-v1",
			SCRIPT_GREP_CURSOR_KEYS,
			querySha256,
			maxDepth
		)
		if not isInteger(payload.b)
			or payload.b < 1
			or payload.b > MAX_SOURCE_BYTES + 1
			or type(payload.h) ~= "string"
			or (payload.h ~= ""
				and (#payload.h ~= 64
					or string.find(payload.h, "[^0-9a-f]") ~= nil))
			or (payload.h == "" and payload.b ~= 1)
		then
			adapterError(
				"invalid_script_cursor",
				"Grep continuation source state is invalid",
				false
			)
		end
		startPosition = payload.p
		resumeByte = payload.b
		resumeSourceSha256 = payload.h
		local observedLineage = treePositionLineage(root, startPosition)
		if not constantTimeTextEqual(payload.l, observedLineage) then
			adapterError(
				"stale_script_cursor",
				"Grep tree changed at the continuation location",
				false
			)
		end
	end

	local frames = newTreeTraversal(
		root,
		rootPath,
		startPosition,
		maxDepth
	)
	local items = {}
	local itemBytes = 0
	local scannedInstances = 0
	local scannedScripts = 0
	local sourceBytesScanned = 0
	local continuationCursor = ""
	local truncationReason = "complete"
	local firstNode = true
	local stop = false

	while not stop do
		local instance, path, _, position = nextTreeNode(frames, maxDepth)
		if instance == nil then
			break
		end
		if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
			continuationCursor = scriptCursorForPosition(
				root,
				position,
				querySha256,
				"studio-script-grep-cursor-v1",
				{ b = 1, h = "" }
			)
			truncationReason = "time_budget"
			break
		end
		scannedInstances = scannedInstances + 1
		if isPlaceScript(instance) then
			local source = readGrepSource(instance)
			local sourceDigest = sourceSha256(source)
			local searchStart = 1
			if firstNode and resumeSourceSha256 ~= "" then
				if not constantTimeTextEqual(
					resumeSourceSha256,
					sourceDigest
				) then
					adapterError(
						"stale_script_source_cursor",
						"Script source changed after the grep continuation",
						false
					)
				end
				searchStart = resumeByte
			elseif firstNode and resumeByte ~= 1 then
				adapterError(
					"invalid_script_cursor",
					"Grep continuation does not identify a source revision",
					false
				)
			end
			if sourceBytesScanned + #source > sourceByteLimit then
				continuationCursor = scriptCursorForPosition(
					root,
					position,
					querySha256,
					"studio-script-grep-cursor-v1",
					{ b = 1, h = "" }
				)
				truncationReason = "source_bytes"
				break
			end
			sourceBytesScanned = sourceBytesScanned + #source
			scannedScripts = scannedScripts + 1
			local haystack = caseSensitive and source
				or string.lower(source)
			local needle = caseSensitive and query or string.lower(query)
			local lineNumber, lineStart, nextNewline =
				sourceLineStateAt(source, searchStart)

			while searchStart <= #source do
				if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
					continuationCursor = scriptCursorForPosition(
						root,
						position,
						querySha256,
						"studio-script-grep-cursor-v1",
						{ b = searchStart, h = sourceDigest }
					)
					truncationReason = "time_budget"
					stop = true
					break
				end
				local matchStart, matchEnd = string.find(
					haystack,
					needle,
					searchStart,
					true
				)
				if matchStart == nil then
					break
				end
				while nextNewline ~= nil and nextNewline < matchStart do
					lineNumber = lineNumber + 1
					lineStart = nextNewline + 1
					nextNewline = string.find(
						source,
						"\n",
						lineStart,
						true
					)
				end
				local lineEnd = nextNewline ~= nil
					and nextNewline - 1 or #source
				if lineEnd >= lineStart
					and string.byte(source, lineEnd) == 13
				then
					lineEnd = lineEnd - 1
				end
				local previewStart,
					preview,
					prefixTruncated,
					suffixTruncated = grepPreview(
						source,
						lineStart,
						lineEnd,
						matchStart,
						matchEnd
					)
				local item = {
					path = copyPath(path),
					name = instance.Name,
					class_name = instance.ClassName,
					source_sha256 = sourceDigest,
					source_length = #source,
					match_start_byte = matchStart,
					match_end_byte = matchEnd,
					line_number = lineNumber,
					column_byte = matchStart - lineStart + 1,
					preview_start_byte = previewStart,
					preview = preview,
					preview_prefix_truncated = prefixTruncated,
					preview_suffix_truncated = suffixTruncated,
				}
				local itemJsonOk, itemJson = pcall(function()
					return HttpService:JSONEncode(item)
				end)
				if not itemJsonOk or type(itemJson) ~= "string" then
					adapterError(
						"script_result_encoding_unavailable",
						"Studio could not encode a grep match",
						false
					)
				end
				if itemBytes + #itemJson + 1 > SCRIPT_GREP_ITEM_BUDGET then
					continuationCursor = scriptCursorForPosition(
						root,
						position,
						querySha256,
						"studio-script-grep-cursor-v1",
						{ b = matchStart, h = sourceDigest }
					)
					truncationReason = "output_bytes"
					stop = true
					break
				end
				table.insert(items, item)
				itemBytes = itemBytes + #itemJson + 1
				searchStart = matchEnd + 1
				if #items >= pageSize then
					local furtherMatch = string.find(
						haystack,
						needle,
						searchStart,
						true
					)
					if furtherMatch ~= nil then
						continuationCursor = scriptCursorForPosition(
							root,
							position,
							querySha256,
							"studio-script-grep-cursor-v1",
							{ b = searchStart, h = sourceDigest }
						)
					else
						local _, _, _, followingPosition =
							nextTreeNode(frames, maxDepth)
						if followingPosition ~= nil then
							continuationCursor = scriptCursorForPosition(
								root,
								followingPosition,
								querySha256,
								"studio-script-grep-cursor-v1",
								{ b = 1, h = "" }
							)
						end
					end
					if continuationCursor ~= "" then
						truncationReason = "page_size"
					end
					stop = true
					break
				end
			end
		elseif firstNode and resumeSourceSha256 ~= "" then
			adapterError(
				"stale_script_source_cursor",
				"Grep continuation no longer points to a place script",
				false
			)
		end
		firstNode = false
		resumeByte = 1
		resumeSourceSha256 = ""

		if not stop and scannedInstances >= scanLimit then
			local _, _, _, followingPosition =
				nextTreeNode(frames, maxDepth)
			if followingPosition ~= nil then
				continuationCursor = scriptCursorForPosition(
					root,
					followingPosition,
					querySha256,
					"studio-script-grep-cursor-v1",
					{ b = 1, h = "" }
				)
				truncationReason = "scan_limit"
			end
			break
		end
		if not stop
			and scannedInstances % TREE_COOPERATIVE_YIELD_INTERVAL == 0
		then
			task.wait()
			assertExpectedDocument()
			requireEditMode()
		end
	end

	assertExpectedDocument()
	requireEditMode()
	local result = newScriptResult("studio_grep_scripts", requestId)
	result.root_path = copyPath(rootPath)
	result.query = query
	result.match_mode = "literal"
	result.case_sensitive = caseSensitive
	result.query_version = SCRIPT_GREP_QUERY_VERSION
	result.sort_version = TREE_SORT_VERSION
	result.max_depth = maxDepth
	result.scan_limit = scanLimit
	result.source_byte_limit = sourceByteLimit
	result.page_size = pageSize
	result.time_limit_ms = timeLimitMs
	result.scanned_instances = scannedInstances
	result.scanned_scripts = scannedScripts
	result.source_bytes_scanned = sourceBytesScanned
	result.returned = #items
	result.items = items
	result.truncated = continuationCursor ~= ""
	result.has_more = continuationCursor ~= ""
	result.continuation_cursor = continuationCursor
	result.truncation_reason = truncationReason
	result.output_limit_bytes = MAX_SCRIPT_GREP_OUTPUT_BYTES
	assertBoundedScriptResult(result, MAX_SCRIPT_GREP_OUTPUT_BYTES)
	return result
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
	elseif request.operation == "studio_search_scripts" then
		return searchScripts(request.args, request.request_id)
	elseif request.operation == "studio_grep_scripts" then
		return grepScripts(request.args, request.request_id)
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
