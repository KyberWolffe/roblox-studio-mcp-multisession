--[[
Durable Studio MCP Multisession operation registry.

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
local OPTIONAL_SERVICES = table.freeze({
	ScriptEditorService = optionalService("ScriptEditorService"),
	LogService = optionalService("LogService"),
	StudioCaptureService = optionalService("StudioCaptureService"),
	EncodingService = optionalService("EncodingService"),
})

local DURABLE_BOUNDS = table.freeze({
	MAX_PATH_SEGMENTS = 64,
	MAX_PATH_SEGMENT_BYTES = 100,
	MAX_SOURCE_BYTES = 262_144,
	MAX_ATTRIBUTE_STRING_BYTES = 4_096,
	MAX_CONSOLE_MESSAGE_BYTES = 2_000,
	MAX_SCREENSHOT_BYTES = 600_000,
	SCREENSHOT_READY_SECONDS = 5,
	MAX_TREE_FILTER_BYTES = 100,
	MAX_TREE_CURSOR_BYTES = 512,
	MAX_TREE_SCAN_LIMIT = 5_000,
	DEFAULT_TREE_SCAN_LIMIT = 2_000,
	MAX_TREE_PAGE_SIZE = 500,
	MAX_TREE_CHILDREN_PER_INSTANCE = 10_000,
	MAX_TREE_RETAINED_CHILDREN = 20_000,
	MAX_TREE_OUTPUT_BYTES = 600_000,
	TREE_COOPERATIVE_YIELD_INTERVAL = 100,
	MAX_SCRIPT_QUERY_BYTES = 256,
	MAX_SCRIPT_KEYWORDS = 8,
	MAX_SCRIPT_KEYWORD_BYTES = 64,
	MAX_SCRIPT_CURSOR_BYTES = 2_048,
	MAX_SCRIPT_SCAN_LIMIT = 5_000,
	DEFAULT_SCRIPT_SCAN_LIMIT = 2_000,
	MAX_SCRIPT_SEARCH_PAGE_SIZE = 10,
	DEFAULT_SCRIPT_SEARCH_PAGE_SIZE = 10,
	MAX_SCRIPT_GREP_PAGE_SIZE = 50,
	DEFAULT_SCRIPT_GREP_PAGE_SIZE = 50,
	MIN_SCRIPT_TIME_LIMIT_MS = 100,
	MAX_SCRIPT_TIME_LIMIT_MS = 10_000,
	DEFAULT_SCRIPT_SEARCH_TIME_LIMIT_MS = 3_000,
	DEFAULT_SCRIPT_GREP_TIME_LIMIT_MS = 5_000,
	MIN_SCRIPT_SOURCE_BUDGET = 262_144,
	MAX_SCRIPT_SOURCE_BUDGET = 4_194_304,
	DEFAULT_SCRIPT_SOURCE_BUDGET = 1_048_576,
	MAX_SCRIPT_SOURCE_LINES = 20_000,
	MAX_SCRIPT_PREVIEW_BYTES = 512,
	MAX_SCRIPT_SEARCH_OUTPUT_BYTES = 200_000,
	MAX_SCRIPT_GREP_OUTPUT_BYTES = 500_000,
	SCRIPT_SEARCH_ITEM_BUDGET = 160_000,
	SCRIPT_GREP_ITEM_BUDGET = 450_000,
	MAX_INSPECT_CHILD_LIMIT = 200,
	DEFAULT_INSPECT_CHILD_LIMIT = 50,
	MAX_INSPECT_DESCENDANT_DEPTH = 64,
	MAX_INSPECT_DESCENDANT_SCAN_LIMIT = 5_000,
	DEFAULT_INSPECT_DESCENDANT_SCAN_LIMIT = 2_000,
	MIN_INSPECT_TIME_LIMIT_MS = 100,
	MAX_INSPECT_TIME_LIMIT_MS = 10_000,
	DEFAULT_INSPECT_TIME_LIMIT_MS = 3_000,
	MAX_INSPECT_ATTRIBUTES_RAW = 1_024,
	MAX_INSPECT_ATTRIBUTES_RETURNED = 64,
	MAX_INSPECT_TAGS_RAW = 1_024,
	MAX_INSPECT_TAGS_RETURNED = 128,
	MAX_INSPECT_SEQUENCE_KEYPOINTS = 64,
	MAX_INSPECT_CLASS_COUNTS = 256,
	MAX_INSPECT_STRING_RAW_BYTES = 262_144,
	MAX_INSPECT_STRING_PREFIX_BYTES = 1_024,
	MAX_INSPECT_CHILD_OUTPUT_BYTES = 300_000,
	MAX_INSPECT_RESULT_BYTES = 500_000,
	INSPECT_COOPERATIVE_YIELD_INTERVAL = 100,
	MAX_MULTI_EDIT_TARGETS = 16,
	MAX_MULTI_EDIT_CREATES = 16,
	MAX_MULTI_EDIT_EDITS_PER_TARGET = 64,
	MAX_MULTI_EDIT_EDITS = 128,
	MAX_MULTI_EDIT_LITERAL_BYTES = 262_144,
	MAX_MULTI_EDIT_SOURCE_BYTES = 262_144,
	MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES = 1_048_576,
	MAX_MULTI_EDIT_REPLACEMENT_SPANS = 1_024,
	MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES = 8_192,
	MAX_MULTI_EDIT_RECEIPT_BYTES = 100_000,
	MULTI_EDIT_PREPARE_TTL_SECONDS = 120,
})
local PROTOCOL_METADATA = table.freeze({
	TREE_CURSOR_VERSION = 1,
	TREE_SORT_VERSION = "name-class-v1",
	SCRIPT_CURSOR_VERSION = 1,
	SCRIPT_SEARCH_QUERY_VERSION = "script-name-query-v1",
	SCRIPT_GREP_QUERY_VERSION = "script-grep-query-v1",
	SCRIPT_SEARCH_MATCH_SEMANTICS =
		"all_keywords_ascii_case_insensitive_literal_subsequence",
	MULTI_EDIT_ORDERING_VERSION =
		"edit-target-input-then-create-input-v2",
	MULTI_EDIT_ATOMICITY =
		"preflight-all-per-target-cas-created-script-compensation-no-cross-script-atomicity-v2",
	MULTI_EDIT_RECEIPT_CONTRACT =
		"broker-validated-downstream-ack-v2",
	INSPECT_SNAPSHOT_CONTRACT =
		"path-edit-generation-fenced-observational-v1",
	INSPECT_PROPERTY_ALLOWLIST_VERSION =
		"instance-property-allowlist-v1",
	INSPECT_VALUE_ENCODING_VERSION = "instance-value-v1",
	INSPECT_SORT_VERSION = "name-class-original-v1",
})

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
	studio_inspect_instance = table.freeze({
		path = true,
		child_limit = true,
		descendant_max_depth = true,
		descendant_scan_limit = true,
		time_limit_ms = true,
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
	studio_multi_edit = table.freeze({
		_phase = true,
		transaction_id = true,
		datamodel_type = true,
		targets = true,
		creates = true,
		prepare_request_id = true,
		prepare_sha256 = true,
		prepared_targets = true,
	}),
	studio_recover_multi_edit = table.freeze({
		transaction_id = true,
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

local function validPathSegmentCodepoints(value)
	if type(value) ~= "string" then
		return false
	end
	local decoded, noControls = pcall(function()
		for _, codepoint in utf8.codes(value) do
			if codepoint <= 0x1f
				or (codepoint >= 0x7f and codepoint <= 0x9f)
			then
				return false
			end
		end
		return true
	end)
	return decoded and noControls
end

local function validatePath(path, allowEmpty, label)
	if type(path) ~= "table" then
		adapterError("invalid_args", label .. " must be a path array", false)
	end
	local count = #path
	if (not allowEmpty and count < 1) or count > DURABLE_BOUNDS.MAX_PATH_SEGMENTS then
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
				or #segment > DURABLE_BOUNDS.MAX_PATH_SEGMENT_BYTES
				or not validPathSegmentCodepoints(segment)
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
		or #value > DURABLE_BOUNDS.MAX_TREE_FILTER_BYTES
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
		or #value > DURABLE_BOUNDS.MAX_SCRIPT_QUERY_BYTES
	then
		adapterError(
			"invalid_args",
			label .. " must be 1 to "
				.. tostring(DURABLE_BOUNDS.MAX_SCRIPT_QUERY_BYTES)
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
		if #token < 1 or #token > DURABLE_BOUNDS.MAX_SCRIPT_KEYWORD_BYTES then
			adapterError(
				"invalid_args",
				"Each comma-separated keyword must be 1 to "
					.. tostring(DURABLE_BOUNDS.MAX_SCRIPT_KEYWORD_BYTES)
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
		if #normalized > DURABLE_BOUNDS.MAX_SCRIPT_KEYWORDS then
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
		or #value > DURABLE_BOUNDS.MAX_SCRIPT_CURSOR_BYTES
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
		validateIntegerRange(args.max_depth, 0, DURABLE_BOUNDS.MAX_PATH_SEGMENTS, "max_depth")
	end
	local maxDepth = args.max_depth
		or (DURABLE_BOUNDS.MAX_PATH_SEGMENTS - #rootPath)
	if #rootPath + maxDepth > DURABLE_BOUNDS.MAX_PATH_SEGMENTS then
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
			DURABLE_BOUNDS.MAX_SCRIPT_SCAN_LIMIT,
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
			DURABLE_BOUNDS.MIN_SCRIPT_TIME_LIMIT_MS,
			DURABLE_BOUNDS.MAX_SCRIPT_TIME_LIMIT_MS,
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
		if #children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE then
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
		return OPTIONAL_SERVICES.EncodingService:ComputeStringHash(
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
		return OPTIONAL_SERVICES.ScriptEditorService:GetEditorSource(target)
	end)
	if not ok or type(source) ~= "string" or #source > DURABLE_BOUNDS.MAX_SOURCE_BYTES then
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
		if type(value) ~= "string" or #value > DURABLE_BOUNDS.MAX_ATTRIBUTE_STRING_BYTES then
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

local MULTI_EDIT_TARGET_KEYS = table.freeze({
	path = true,
	expected_sha256 = true,
	edits = true,
})
local MULTI_EDIT_CREATE_KEYS = table.freeze({
	parent_path = true,
	name = true,
	class_name = true,
	source = true,
	expected_absent = true,
})
local MULTI_EDIT_EDIT_KEYS = table.freeze({
	old_string = true,
	new_string = true,
	replace_all = true,
	start_byte = true,
	end_byte = true,
})
local MULTI_EDIT_PREPARED_EDIT_TARGET_KEYS = table.freeze({
	index = true,
	kind = true,
	path = true,
	expected_sha256 = true,
	prepared_sha256 = true,
	planned_sha256 = true,
	source_length = true,
	planned_source_length = true,
	edit_count = true,
	replacement_count = true,
	status = true,
})
local MULTI_EDIT_PREPARED_CREATE_TARGET_KEYS = table.freeze({
	index = true,
	kind = true,
	path = true,
	parent_path = true,
	name = true,
	class_name = true,
	expected_absent = true,
	prepared_absent = true,
	planned_sha256 = true,
	planned_source_length = true,
	status = true,
})
local MULTI_EDIT_PREPARE_ARGS_KEYS = table.freeze({
	_phase = true,
	transaction_id = true,
	datamodel_type = true,
	targets = true,
	creates = true,
})
local MULTI_EDIT_APPLY_ARGS_KEYS = table.freeze({
	_phase = true,
	transaction_id = true,
	prepare_request_id = true,
	prepare_sha256 = true,
	prepared_targets = true,
})

local function validUtf8(value)
	if type(value) ~= "string" then
		return false
	end
	local length = utf8.len(value)
	return length ~= nil
end

local function validateSha256(value, label)
	if type(value) ~= "string"
		or #value ~= 64
		or string.find(value, "[^0-9a-f]") ~= nil
	then
		adapterError(
			"invalid_args",
			label .. " must be a lowercase SHA-256 digest",
			false
		)
	end
	return value
end

local function validateDenseArray(value, label)
	if type(value) ~= "table" then
		adapterError("invalid_args", label .. " must be a dense array", false)
	end
	local count = #value
	for key in pairs(value) do
		if not isInteger(key) or key < 1 or key > count then
			adapterError("invalid_args", label .. " must be a dense array", false)
		end
	end
	return count
end

local function multiEditPathSignature(path)
	local parts = {}
	for _, segment in ipairs(path) do
		table.insert(parts, tostring(#segment))
		table.insert(parts, ":")
		table.insert(parts, segment)
		table.insert(parts, ";")
	end
	return table.concat(parts)
end

local function pathsEqual(left, right)
	if #left ~= #right then
		return false
	end
	for index, segment in ipairs(left) do
		if right[index] ~= segment then
			return false
		end
	end
	return true
end

local function validateCreateClass(className, label)
	if className ~= "Script"
		and className ~= "LocalScript"
		and className ~= "ModuleScript"
	then
		adapterError(
			"invalid_args",
			label .. " must be Script, LocalScript, or ModuleScript",
			false
		)
	end
	return className
end

local function validateMultiEditTargets(targets, creates)
	local targetCount = validateDenseArray(targets, "targets")
	local createCount = creates == nil
		and 0
		or validateDenseArray(creates, "creates")
	if targetCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_TARGETS
		or createCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_CREATES
		or targetCount + createCount < 1
		or targetCount + createCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_TARGETS
	then
		adapterError(
			"invalid_args",
			"targets plus creates must contain 1 to 16 entries",
			false
		)
	end
	local seenPaths = {}
	local totalEdits = 0
	local literalBytes = 0
	local aggregatePathBytes = 0
	for targetIndex, target in ipairs(targets) do
		if type(target) ~= "table" then
			adapterError("invalid_args", "Each target must be an object", false)
		end
		assertExactKeys(target, MULTI_EDIT_TARGET_KEYS, "multi_edit_target")
		local path = validatePath(
			target.path,
			false,
			"targets[" .. tostring(targetIndex) .. "].path"
		)
		for _, segment in ipairs(path) do
			aggregatePathBytes = aggregatePathBytes + #segment
		end
		if aggregatePathBytes > DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES then
			adapterError(
				"invalid_args",
				"Aggregate multi-edit target paths exceed 8192 UTF-8 bytes",
				false
			)
		end
		local pathSignature = multiEditPathSignature(path)
		if seenPaths[pathSignature] == true then
			adapterError(
				"invalid_args",
				"multi-edit target paths must be unique",
				false
			)
		end
		seenPaths[pathSignature] = true
		validateSha256(
			target.expected_sha256,
			"targets[" .. tostring(targetIndex) .. "].expected_sha256"
		)
		local edits = target.edits
		if type(edits) ~= "table"
			or #edits < 1
			or #edits > DURABLE_BOUNDS.MAX_MULTI_EDIT_EDITS_PER_TARGET
		then
			adapterError(
				"invalid_args",
				"Each target must contain 1 to 64 edits",
				false
			)
		end
		for key in pairs(edits) do
			if not isInteger(key) or key < 1 or key > #edits then
				adapterError(
					"invalid_args",
					"edits must be a dense array",
					false
				)
			end
		end
		for editIndex, edit in ipairs(edits) do
			if type(edit) ~= "table" then
				adapterError("invalid_args", "Each edit must be an object", false)
			end
			assertExactKeys(edit, MULTI_EDIT_EDIT_KEYS, "multi_edit_edit")
			if not validUtf8(edit.old_string)
				or #edit.old_string < 1
				or #edit.old_string > DURABLE_BOUNDS.MAX_MULTI_EDIT_LITERAL_BYTES
				or not validUtf8(edit.new_string)
				or #edit.new_string > DURABLE_BOUNDS.MAX_MULTI_EDIT_LITERAL_BYTES
				or edit.old_string == edit.new_string
			then
				adapterError(
					"invalid_args",
					"Edit strings must be distinct bounded valid UTF-8",
					false
				)
			end
			local replaceAll = edit.replace_all
			if replaceAll ~= nil and type(replaceAll) ~= "boolean" then
				adapterError(
					"invalid_args",
					"replace_all must be boolean",
					false
				)
			end
			local hasStart = edit.start_byte ~= nil
			local hasEnd = edit.end_byte ~= nil
			if hasStart ~= hasEnd then
				adapterError(
					"invalid_args",
					"start_byte and end_byte must be supplied together",
					false
				)
			end
			if hasStart then
				if replaceAll == true
					or not isInteger(edit.start_byte)
					or not isInteger(edit.end_byte)
					or edit.start_byte < 0
					or edit.end_byte <= edit.start_byte
					or edit.end_byte > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
					or edit.end_byte - edit.start_byte ~= #edit.old_string
				then
					adapterError(
						"invalid_args",
						"Edit byte offsets are invalid or incompatible",
						false
					)
				end
			end
			literalBytes = literalBytes
				+ #edit.old_string
				+ #edit.new_string
			totalEdits = totalEdits + 1
			if literalBytes > DURABLE_BOUNDS.MAX_MULTI_EDIT_LITERAL_BYTES
				or totalEdits > DURABLE_BOUNDS.MAX_MULTI_EDIT_EDITS
			then
				adapterError(
					"invalid_args",
					"Aggregate multi-edit payload exceeds its bound",
					false
				)
			end
		end
	end
	for createIndex, create in ipairs(creates or {}) do
		if type(create) ~= "table" then
			adapterError("invalid_args", "Each create must be an object", false)
		end
		assertExactKeys(create, MULTI_EDIT_CREATE_KEYS, "multi_edit_create")
		local parentPath = validatePath(
			create.parent_path,
			false,
			"creates[" .. tostring(createIndex) .. "].parent_path"
		)
		if #parentPath >= DURABLE_BOUNDS.MAX_PATH_SEGMENTS then
			adapterError(
				"invalid_args",
				"Create parent path leaves no bounded child segment",
				false
			)
		end
		validatePath(
			{ create.name },
			false,
			"creates[" .. tostring(createIndex) .. "].name"
		)
		if not validUtf8(create.name) then
			adapterError(
				"invalid_args",
				"Create name must be valid UTF-8",
				false
			)
		end
		validateCreateClass(
			create.class_name,
			"creates[" .. tostring(createIndex) .. "].class_name"
		)
		if create.expected_absent ~= true then
			adapterError(
				"invalid_args",
				"Every script create must require expected_absent=true",
				false
			)
		end
		if not validUtf8(create.source)
			or #create.source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
		then
			adapterError(
				"invalid_args",
				"Create source must be bounded valid UTF-8",
				false
			)
		end
		local path = appendPath(parentPath, create.name)
		for _, segment in ipairs(path) do
			aggregatePathBytes = aggregatePathBytes + #segment
		end
		if aggregatePathBytes > DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES then
			adapterError(
				"invalid_args",
				"Aggregate multi-edit target paths exceed 8192 UTF-8 bytes",
				false
			)
		end
		local pathSignature = multiEditPathSignature(path)
		if seenPaths[pathSignature] == true then
			adapterError(
				"invalid_args",
				"Edit and create exact target paths must be unique",
				false
			)
		end
		seenPaths[pathSignature] = true
	end
	return totalEdits, createCount
end

local function validatePreparedTargets(targets)
	local targetCount = validateDenseArray(targets, "prepared_targets")
	if targetCount < 1 or targetCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_TARGETS then
		adapterError("invalid_args", "prepared_targets is invalid", false)
	end
	local aggregatePathBytes = 0
	local seenPaths = {}
	local sawCreate = false
	local createCount = 0
	for index, target in ipairs(targets) do
		if type(target) ~= "table" then
			adapterError("invalid_args", "prepared target is invalid", false)
		end
		if target.index ~= index or target.status ~= "prepared" then
			adapterError("invalid_args", "prepared target fields are invalid", false)
		end
		local path = validatePath(
			target.path,
			false,
			"prepared_target.path"
		)
		for _, segment in ipairs(path) do
			aggregatePathBytes = aggregatePathBytes + #segment
		end
		if aggregatePathBytes > DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_PATH_BYTES then
			adapterError(
				"invalid_args",
				"Prepared target paths exceed the aggregate bound",
				false
			)
		end
		local pathSignature = multiEditPathSignature(path)
		if seenPaths[pathSignature] == true then
			adapterError("invalid_args", "prepared target paths must be unique", false)
		end
		seenPaths[pathSignature] = true
		if target.kind == "edit" then
			if sawCreate then
				adapterError(
					"invalid_args",
					"Prepared edits must precede prepared creates",
					false
				)
			end
			assertExactKeys(
				target,
				MULTI_EDIT_PREPARED_EDIT_TARGET_KEYS,
				"prepared_edit_target"
			)
			if not isInteger(target.source_length)
				or target.source_length < 0
				or target.source_length > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
				or not isInteger(target.planned_source_length)
				or target.planned_source_length < 0
				or target.planned_source_length > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
				or not isInteger(target.edit_count)
				or target.edit_count < 1
				or target.edit_count > DURABLE_BOUNDS.MAX_MULTI_EDIT_EDITS_PER_TARGET
				or not isInteger(target.replacement_count)
				or target.replacement_count < target.edit_count
				or target.replacement_count > DURABLE_BOUNDS.MAX_MULTI_EDIT_REPLACEMENT_SPANS
			then
				adapterError("invalid_args", "prepared edit fields are invalid", false)
			end
			validateSha256(target.expected_sha256, "prepared expected_sha256")
			validateSha256(target.prepared_sha256, "prepared prepared_sha256")
			validateSha256(target.planned_sha256, "prepared planned_sha256")
		elseif target.kind == "create" then
			sawCreate = true
			createCount = createCount + 1
			assertExactKeys(
				target,
				MULTI_EDIT_PREPARED_CREATE_TARGET_KEYS,
				"prepared_create_target"
			)
			local parentPath = validatePath(
				target.parent_path,
				false,
				"prepared_create_target.parent_path"
			)
			if target.expected_absent ~= true
				or target.prepared_absent ~= true
				or not validUtf8(target.name)
				or target.name ~= path[#path]
				or #parentPath + 1 ~= #path
				or not isInteger(target.planned_source_length)
				or target.planned_source_length < 0
				or target.planned_source_length > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
			then
				adapterError("invalid_args", "prepared create fields are invalid", false)
			end
			for parentIndex, segment in ipairs(parentPath) do
				if path[parentIndex] ~= segment then
					adapterError(
						"invalid_args",
						"prepared create path does not match its parent and name",
						false
					)
				end
			end
			validateCreateClass(target.class_name, "prepared create class_name")
			validateSha256(target.planned_sha256, "prepared planned_sha256")
		else
			adapterError(
				"invalid_args",
				"prepared target kind must be edit or create",
				false
			)
		end
	end
	if createCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_CREATES then
		adapterError("invalid_args", "prepared creates exceed their bound", false)
	end
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
		if #rootPath + maxDepth > DURABLE_BOUNDS.MAX_PATH_SEGMENTS then
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
				DURABLE_BOUNDS.MAX_TREE_SCAN_LIMIT,
				"scan_limit"
			)
		end
		if args.page_size ~= nil then
			validateIntegerRange(
				args.page_size,
				1,
				DURABLE_BOUNDS.MAX_TREE_PAGE_SIZE,
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
				or #args.continuation_cursor > DURABLE_BOUNDS.MAX_TREE_CURSOR_BYTES
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
			DURABLE_BOUNDS.MAX_SCRIPT_SEARCH_PAGE_SIZE
		)
	elseif operation == "studio_grep_scripts" then
		validatePrintableAsciiScriptText(args.query, "query")
		validateScriptTraversalArgs(
			args,
			DURABLE_BOUNDS.MAX_SCRIPT_GREP_PAGE_SIZE
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
				DURABLE_BOUNDS.MIN_SCRIPT_SOURCE_BUDGET,
				DURABLE_BOUNDS.MAX_SCRIPT_SOURCE_BUDGET,
				"source_byte_limit"
			)
		end
	elseif operation == "studio_inspect_instance" then
		local path = validatePath(args.path, false, "path")
		if args.child_limit ~= nil then
			validateIntegerRange(
				args.child_limit,
				0,
				DURABLE_BOUNDS.MAX_INSPECT_CHILD_LIMIT,
				"child_limit"
			)
		end
		if args.descendant_max_depth ~= nil then
			validateIntegerRange(
				args.descendant_max_depth,
				0,
				DURABLE_BOUNDS.MAX_INSPECT_DESCENDANT_DEPTH,
				"descendant_max_depth"
			)
		end
		local descendantMaxDepth = args.descendant_max_depth
			or (DURABLE_BOUNDS.MAX_PATH_SEGMENTS - #path)
		if #path + descendantMaxDepth > DURABLE_BOUNDS.MAX_PATH_SEGMENTS then
			adapterError(
				"invalid_args",
				"path plus descendant_max_depth exceeds the reusable path bound",
				false
			)
		end
		if args.descendant_scan_limit ~= nil then
			validateIntegerRange(
				args.descendant_scan_limit,
				1,
				DURABLE_BOUNDS.MAX_INSPECT_DESCENDANT_SCAN_LIMIT,
				"descendant_scan_limit"
			)
		end
		if args.time_limit_ms ~= nil then
			validateIntegerRange(
				args.time_limit_ms,
				DURABLE_BOUNDS.MIN_INSPECT_TIME_LIMIT_MS,
				DURABLE_BOUNDS.MAX_INSPECT_TIME_LIMIT_MS,
				"time_limit_ms"
			)
		end
	elseif operation == "studio_read_script" then
		validatePath(args.path, false, "path")
		if args.max_chars ~= nil then
			validateIntegerRange(args.max_chars, 1, DURABLE_BOUNDS.MAX_SOURCE_BYTES, "max_chars")
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
			or #args.new_source > DURABLE_BOUNDS.MAX_SOURCE_BYTES
		then
			adapterError(
				"invalid_args",
				"new_source is invalid or exceeds the durable limit",
				false
			)
		end
	elseif operation == "studio_multi_edit" then
		if args._phase == "prepare" then
			assertExactKeys(
				args,
				MULTI_EDIT_PREPARE_ARGS_KEYS,
				"multi_edit_prepare_args"
			)
			if not isCanonicalUuid(args.transaction_id)
				or args.datamodel_type ~= "Edit"
			then
				adapterError(
					"invalid_args",
					"Multi-edit prepare identity or DataModel is invalid",
					false
				)
			end
			validateMultiEditTargets(args.targets, args.creates)
		elseif args._phase == "apply" then
			assertExactKeys(
				args,
				MULTI_EDIT_APPLY_ARGS_KEYS,
				"multi_edit_apply_args"
			)
			if not isCanonicalUuid(args.transaction_id)
				or not isRequestId(args.prepare_request_id)
			then
				adapterError(
					"invalid_args",
					"Multi-edit apply identity is invalid",
					false
				)
			end
			validateSha256(
				args.prepare_sha256,
				"prepare_sha256"
			)
			validatePreparedTargets(args.prepared_targets)
		else
			adapterError(
				"invalid_args",
				"Multi-edit _phase must be prepare or apply",
				false
			)
		end
	elseif operation == "studio_recover_multi_edit" then
		if not isCanonicalUuid(args.transaction_id) then
			adapterError(
				"invalid_args",
				"transaction_id must be a canonical UUID",
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
	c = true,
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
	if OPTIONAL_SERVICES.EncodingService == nil then
		adapterError(
			"tree_cursor_unavailable",
			"Studio SHA-256 support is unavailable for tree pagination",
			false
		)
	end
	local ok, binaryDigest = pcall(function()
		return OPTIONAL_SERVICES.EncodingService:ComputeStringHash(
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
	if OPTIONAL_SERVICES.EncodingService == nil then
		adapterError(
			"script_cursor_unavailable",
			"Studio SHA-256 support is unavailable for script pagination",
			false
		)
	end
	local ok, binaryDigest = pcall(function()
		return OPTIONAL_SERVICES.EncodingService:ComputeStringHash(
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
		return OPTIONAL_SERVICES.EncodingService:Base64Encode(buffer.fromstring(json))
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
	if #cursor > DURABLE_BOUNDS.MAX_TREE_CURSOR_BYTES then
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
		return OPTIONAL_SERVICES.EncodingService:Base64Decode(buffer.fromstring(encoded))
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
	if payload.v ~= PROTOCOL_METADATA.TREE_CURSOR_VERSION
		or payload.s ~= peer.studio_id
		or payload.c ~= CLIENT_INSTANCE_ID
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
			or ordinal > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE
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
	appendCanonicalTreeValue(parts, PROTOCOL_METADATA.TREE_SORT_VERSION)
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
	if #children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE then
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
			or #child.Name > DURABLE_BOUNDS.MAX_PATH_SEGMENT_BYTES
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
	if #children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE then
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
	appendCanonicalTreeValue(parts, PROTOCOL_METADATA.TREE_SORT_VERSION)
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
	if retained > DURABLE_BOUNDS.MAX_TREE_RETAINED_CHILDREN then
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

local function listTree(args, requestId)
	local rootPath = args.root_path or {}
	local root = resolveExactPath(rootPath, true)
	local maxDepth = args.max_depth or 2
	local pageSize = args.page_size or args.max_results or 200
	local scanLimit = args.scan_limit or DURABLE_BOUNDS.DEFAULT_TREE_SCAN_LIMIT
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
			if outputBytes + encodedBytes > DURABLE_BOUNDS.MAX_TREE_OUTPUT_BYTES then
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
		if scanned % DURABLE_BOUNDS.TREE_COOPERATIVE_YIELD_INTERVAL == 0 then
			task.wait()
			assertExpectedDocument()
		end
	end

	assertExpectedDocument()
	local continuationCursor = nil
	if nextPosition ~= nil then
		querySha256 = querySha256 or treeSha256(queryCanonical)
		continuationCursor = encodeTreeCursorPayload({
			v = PROTOCOL_METADATA.TREE_CURSOR_VERSION,
			s = peer.studio_id,
			c = CLIENT_INSTANCE_ID,
			d = DOCUMENT_EPOCH,
			g = peer.generation,
			q = querySha256,
			p = nextPosition,
			l = treePositionLineage(root, nextPosition),
		})
	end
	local result = {
		adapter = "studio-mcp-v2-durable-plugin",
		v = 1,
		operation = "studio_list_tree",
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		request_id = requestId,
		root_path = copyPath(rootPath),
		items = items,
		truncated = continuationCursor ~= nil,
		has_more = continuationCursor ~= nil,
		continuation_cursor = continuationCursor or "",
		truncation_reason = truncationReason or "complete",
		max_depth = maxDepth,
		max_results = pageSize,
		page_size = pageSize,
		scan_limit = scanLimit,
		scanned = scanned,
		returned = #items,
		output_bytes = outputBytes,
		name_filter = nameFilter or "",
		class_filter = classFilter or "",
		class_is_a = classIsA,
		sort_version = PROTOCOL_METADATA.TREE_SORT_VERSION,
		output_limit_bytes = DURABLE_BOUNDS.MAX_TREE_OUTPUT_BYTES,
	}
	local resultOk, encodedResult = pcall(function()
		return HttpService:JSONEncode(result)
	end)
	if not resultOk
		or type(encodedResult) ~= "string"
		or #encodedResult > DURABLE_BOUNDS.MAX_TREE_OUTPUT_BYTES
	then
		adapterError(
			"tree_result_oversize",
			"Tree result exceeds its durable output bound",
			false
		)
	end
	return result
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
		return OPTIONAL_SERVICES.EncodingService:Base64Encode(buffer.fromstring(json))
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
	if #cursor > DURABLE_BOUNDS.MAX_SCRIPT_CURSOR_BYTES then
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
		return OPTIONAL_SERVICES.EncodingService:Base64Decode(
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
	if payload.v ~= PROTOCOL_METADATA.SCRIPT_CURSOR_VERSION
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
			or ordinal > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE
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
	appendCanonicalTreeValue(parts, PROTOCOL_METADATA.SCRIPT_SEARCH_QUERY_VERSION)
	appendCanonicalTreeValue(parts, PROTOCOL_METADATA.TREE_SORT_VERSION)
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
	appendCanonicalTreeValue(parts, PROTOCOL_METADATA.SCRIPT_GREP_QUERY_VERSION)
	appendCanonicalTreeValue(parts, PROTOCOL_METADATA.TREE_SORT_VERSION)
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
		v = PROTOCOL_METADATA.SCRIPT_CURSOR_VERSION,
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
		DURABLE_BOUNDS.MAX_SCRIPT_SEARCH_PAGE_SIZE
	)
	local root = resolveExactPath(rootPath, true)
	local keywords = normalizeScriptKeywords(args.keywords)
	local scanLimit = args.scan_limit or DURABLE_BOUNDS.DEFAULT_SCRIPT_SCAN_LIMIT
	local pageSize = args.page_size or DURABLE_BOUNDS.DEFAULT_SCRIPT_SEARCH_PAGE_SIZE
	local timeLimitMs = args.time_limit_ms
		or DURABLE_BOUNDS.DEFAULT_SCRIPT_SEARCH_TIME_LIMIT_MS
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
					> DURABLE_BOUNDS.SCRIPT_SEARCH_ITEM_BUDGET
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
		if scannedInstances % DURABLE_BOUNDS.TREE_COOPERATIVE_YIELD_INTERVAL == 0 then
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
	result.match_semantics = PROTOCOL_METADATA.SCRIPT_SEARCH_MATCH_SEMANTICS
	result.query_version = PROTOCOL_METADATA.SCRIPT_SEARCH_QUERY_VERSION
	result.sort_version = PROTOCOL_METADATA.TREE_SORT_VERSION
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
	result.output_limit_bytes = DURABLE_BOUNDS.MAX_SCRIPT_SEARCH_OUTPUT_BYTES
	assertBoundedScriptResult(result, DURABLE_BOUNDS.MAX_SCRIPT_SEARCH_OUTPUT_BYTES)
	return result
end

local function readGrepSource(target)
	if OPTIONAL_SERVICES.ScriptEditorService == nil then
		adapterError(
			"script_editor_unavailable",
			"ScriptEditorService is unavailable for bounded grep",
			false
		)
	end
	local ok, source = pcall(function()
		return OPTIONAL_SERVICES.ScriptEditorService:GetEditorSource(target)
	end)
	if not ok or type(source) ~= "string" then
		adapterError(
			"script_source_unavailable",
			"One script source is unavailable; grep cannot claim completeness",
			false
		)
	end
	if #source > DURABLE_BOUNDS.MAX_SOURCE_BYTES then
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
		if lineCount > DURABLE_BOUNDS.MAX_SCRIPT_SOURCE_LINES then
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
		(DURABLE_BOUNDS.MAX_SCRIPT_PREVIEW_BYTES - (matchEnd - matchStart + 1)) / 2
	)
	local previewStart = math.max(lineStart, matchStart - roomBefore)
	while previewStart > lineStart
		and byteIsUtf8Continuation(string.byte(source, previewStart))
	do
		previewStart = previewStart - 1
	end
	local previewEnd = math.min(
		lineEnd,
		previewStart + DURABLE_BOUNDS.MAX_SCRIPT_PREVIEW_BYTES - 1
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
		DURABLE_BOUNDS.MAX_SCRIPT_GREP_PAGE_SIZE
	)
	local root = resolveExactPath(rootPath, true)
	local query = validatePrintableAsciiScriptText(args.query, "query")
	local caseSensitive = args.case_sensitive ~= false
	local scanLimit = args.scan_limit or DURABLE_BOUNDS.DEFAULT_SCRIPT_SCAN_LIMIT
	local sourceByteLimit = args.source_byte_limit
		or DURABLE_BOUNDS.DEFAULT_SCRIPT_SOURCE_BUDGET
	local pageSize = args.page_size or DURABLE_BOUNDS.DEFAULT_SCRIPT_GREP_PAGE_SIZE
	local timeLimitMs = args.time_limit_ms
		or DURABLE_BOUNDS.DEFAULT_SCRIPT_GREP_TIME_LIMIT_MS
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
			or payload.b > DURABLE_BOUNDS.MAX_SOURCE_BYTES + 1
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
				if itemBytes + #itemJson + 1 > DURABLE_BOUNDS.SCRIPT_GREP_ITEM_BUDGET then
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
			and scannedInstances % DURABLE_BOUNDS.TREE_COOPERATIVE_YIELD_INTERVAL == 0
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
	result.query_version = PROTOCOL_METADATA.SCRIPT_GREP_QUERY_VERSION
	result.sort_version = PROTOCOL_METADATA.TREE_SORT_VERSION
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
	result.output_limit_bytes = DURABLE_BOUNDS.MAX_SCRIPT_GREP_OUTPUT_BYTES
	assertBoundedScriptResult(result, DURABLE_BOUNDS.MAX_SCRIPT_GREP_OUTPUT_BYTES)
	return result
end

local function newInspectionValue(valueType)
	return {
		type = valueType,
		boolean_value = false,
		number_value = 0,
		text = "",
		numbers = {},
		labels = {},
		byte_length = 0,
		truncated = false,
	}
end

local function inspectionUtf8Prefix(value)
	if type(value) ~= "string"
		or #value > DURABLE_BOUNDS.MAX_INSPECT_STRING_RAW_BYTES
		or utf8.len(value) == nil
	then
		return nil
	end
	if #value <= DURABLE_BOUNDS.MAX_INSPECT_STRING_PREFIX_BYTES then
		return value
	end
	local boundary = utf8.offset(
		value,
		0,
		DURABLE_BOUNDS.MAX_INSPECT_STRING_PREFIX_BYTES + 1
	)
	if boundary == nil or boundary < 1 then
		return nil
	end
	return string.sub(value, 1, boundary - 1)
end

local function boundedInspectionLabel(value)
	if type(value) ~= "string"
		or #value > DURABLE_BOUNDS.MAX_INSPECT_STRING_PREFIX_BYTES
		or utf8.len(value) == nil
	then
		return nil
	end
	return value
end

local function hasInspectionControlCharacter(value)
	for _, codepoint in utf8.codes(value) do
		if codepoint <= 31
			or (codepoint >= 127 and codepoint <= 159)
		then
			return true
		end
	end
	return false
end

local function isInspectionName(value)
	return type(value) == "string"
		and #value >= 1
		and #value <= DURABLE_BOUNDS.MAX_PATH_SEGMENT_BYTES
		and utf8.len(value) ~= nil
		and not hasInspectionControlCharacter(value)
end

local function isInspectionAttributeName(value)
	return type(value) == "string"
		and #value >= 1
		and #value <= DURABLE_BOUNDS.MAX_PATH_SEGMENT_BYTES
		and string.find(value, "^[A-Za-z0-9._/%-]+$") ~= nil
end

local function isInspectionIdentifier(value)
	return type(value) == "string"
		and #value >= 1
		and #value <= DURABLE_BOUNDS.MAX_PATH_SEGMENT_BYTES
		and string.find(value, "^[A-Za-z][A-Za-z0-9_]*$") ~= nil
end

local function isInspectionFontUri(value)
	if boundedInspectionLabel(value) == nil
		or value == ""
		or string.find(value, "[%c%s]") ~= nil
		or hasInspectionControlCharacter(value)
	then
		return false
	end
	for index = 1, #value do
		local byte = string.byte(value, index)
		if byte < 33 or byte > 126 then
			return false
		end
	end
	return string.find(value, "^rbxasset://.+$") ~= nil
		or string.find(value, "^rbxassetid://.+$") ~= nil
		or string.find(value, "^http://.+$") ~= nil
		or string.find(value, "^https://.+$") ~= nil
end

local function finiteInspectionNumbers(values)
	for _, value in ipairs(values) do
		if not isFiniteNumber(value) then
			return nil
		end
	end
	return values
end

local function encodeInspectionEnumItem(value)
	local enumText = tostring(value.EnumType)
	local enumName = string.match(enumText, "^Enum%.([%a_][%w_]*)$")
	local itemName = value.Name
	if enumName == nil
		or not isInspectionIdentifier(enumName)
		or not isInspectionIdentifier(itemName)
		or not isInteger(value.Value)
	then
		return nil
	end
	local encoded = newInspectionValue("enum")
	encoded.number_value = value.Value
	encoded.labels = { enumName, itemName }
	return encoded
end

local function encodeInspectionNumberSequence(value)
	local keypoints = value.Keypoints
	if type(keypoints) ~= "table"
		or #keypoints < 2
		or #keypoints > DURABLE_BOUNDS.MAX_INSPECT_SEQUENCE_KEYPOINTS
	then
		return nil
	end
	local numbers = table.create(#keypoints * 3)
	local previousTime = nil
	for index, keypoint in ipairs(keypoints) do
		if not isFiniteNumber(keypoint.Time)
			or not isFiniteNumber(keypoint.Value)
			or not isFiniteNumber(keypoint.Envelope)
			or keypoint.Time < 0
			or keypoint.Time > 1
			or keypoint.Envelope < 0
			or (previousTime ~= nil and keypoint.Time <= previousTime)
		then
			return nil
		end
		if (index == 1 and keypoint.Time ~= 0)
			or (index == #keypoints and keypoint.Time ~= 1)
		then
			return nil
		end
		table.insert(numbers, keypoint.Time)
		table.insert(numbers, keypoint.Value)
		table.insert(numbers, keypoint.Envelope)
		previousTime = keypoint.Time
	end
	local encoded = newInspectionValue("number_sequence")
	encoded.numbers = numbers
	return encoded
end

local function encodeInspectionColorSequence(value)
	local keypoints = value.Keypoints
	if type(keypoints) ~= "table"
		or #keypoints < 2
		or #keypoints > DURABLE_BOUNDS.MAX_INSPECT_SEQUENCE_KEYPOINTS
	then
		return nil
	end
	local numbers = table.create(#keypoints * 4)
	local previousTime = nil
	for index, keypoint in ipairs(keypoints) do
		local color = keypoint.Value
		if not isFiniteNumber(keypoint.Time)
			or typeof(color) ~= "Color3"
			or finiteInspectionNumbers({
				color.R,
				color.G,
				color.B,
			}) == nil
			or keypoint.Time < 0
			or keypoint.Time > 1
			or color.R < 0
			or color.R > 1
			or color.G < 0
			or color.G > 1
			or color.B < 0
			or color.B > 1
			or (previousTime ~= nil and keypoint.Time <= previousTime)
		then
			return nil
		end
		if (index == 1 and keypoint.Time ~= 0)
			or (index == #keypoints and keypoint.Time ~= 1)
		then
			return nil
		end
		table.insert(numbers, keypoint.Time)
		table.insert(numbers, color.R)
		table.insert(numbers, color.G)
		table.insert(numbers, color.B)
		previousTime = keypoint.Time
	end
	local encoded = newInspectionValue("color_sequence")
	encoded.numbers = numbers
	return encoded
end

local function encodeInspectionFont(value)
	local family = value.Family
	if typeof(family) ~= "Content"
		or family.SourceType ~= Enum.ContentSourceType.Uri
	then
		return nil
	end
	local familyUri = family.Uri
	local weightName = value.Weight.Name
	local styleName = value.Style.Name
	if not isInspectionFontUri(familyUri)
		or not isInspectionIdentifier(weightName)
		or not isInspectionIdentifier(styleName)
		or type(value.Bold) ~= "boolean"
		or not isInteger(value.Weight.Value)
	then
		return nil
	end
	local encoded = newInspectionValue("font")
	encoded.boolean_value = value.Bold
	encoded.number_value = value.Weight.Value
	encoded.labels = { familyUri, weightName, styleName }
	return encoded
end

local function encodeInspectionValue(value)
	if value == nil then
		return newInspectionValue("nil")
	end
	local valueType = typeof(value)
	if valueType == "boolean" then
		local encoded = newInspectionValue("boolean")
		encoded.boolean_value = value
		return encoded
	elseif valueType == "number" then
		if not isFiniteNumber(value) then
			return nil
		end
		local encoded = newInspectionValue("number")
		encoded.number_value = value
		return encoded
	elseif valueType == "string" then
		local prefix = inspectionUtf8Prefix(value)
		if prefix == nil then
			return nil
		end
		local encoded = newInspectionValue("string")
		encoded.text = prefix
		encoded.byte_length = #value
		encoded.truncated = #prefix < #value
		return encoded
	elseif valueType == "EnumItem" then
		return encodeInspectionEnumItem(value)
	elseif valueType == "Vector2" then
		local numbers = finiteInspectionNumbers({ value.X, value.Y })
		if numbers == nil then
			return nil
		end
		local encoded = newInspectionValue("vector2")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "Vector3" then
		local numbers = finiteInspectionNumbers({
			value.X,
			value.Y,
			value.Z,
		})
		if numbers == nil then
			return nil
		end
		local encoded = newInspectionValue("vector3")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "Color3" then
		local numbers = finiteInspectionNumbers({
			value.R,
			value.G,
			value.B,
		})
		if numbers == nil
			or value.R < 0
			or value.R > 1
			or value.G < 0
			or value.G > 1
			or value.B < 0
			or value.B > 1
		then
			return nil
		end
		local encoded = newInspectionValue("color3")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "CFrame" then
		local x,
			y,
			z,
			r00,
			r01,
			r02,
			r10,
			r11,
			r12,
			r20,
			r21,
			r22 = value:GetComponents()
		local numbers = {
			x,
			y,
			z,
			r00,
			r01,
			r02,
			r10,
			r11,
			r12,
			r20,
			r21,
			r22,
		}
		if #numbers ~= 12 or finiteInspectionNumbers(numbers) == nil then
			return nil
		end
		local encoded = newInspectionValue("cframe")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "UDim" then
		local numbers = finiteInspectionNumbers({
			value.Scale,
			value.Offset,
		})
		if numbers == nil or not isInteger(value.Offset) then
			return nil
		end
		local encoded = newInspectionValue("udim")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "UDim2" then
		local numbers = finiteInspectionNumbers({
			value.X.Scale,
			value.X.Offset,
			value.Y.Scale,
			value.Y.Offset,
		})
		if numbers == nil
			or not isInteger(value.X.Offset)
			or not isInteger(value.Y.Offset)
		then
			return nil
		end
		local encoded = newInspectionValue("udim2")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "Rect" then
		local numbers = finiteInspectionNumbers({
			value.Min.X,
			value.Min.Y,
			value.Max.X,
			value.Max.Y,
		})
		if numbers == nil
			or value.Min.X > value.Max.X
			or value.Min.Y > value.Max.Y
		then
			return nil
		end
		local encoded = newInspectionValue("rect")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "BrickColor" then
		local color = value.Color
		local name = boundedInspectionLabel(value.Name)
		local numbers = finiteInspectionNumbers({
			color.R,
			color.G,
			color.B,
		})
		if not isInspectionName(name)
			or not isInteger(value.Number)
			or numbers == nil
			or color.R < 0
			or color.R > 1
			or color.G < 0
			or color.G > 1
			or color.B < 0
			or color.B > 1
		then
			return nil
		end
		local encoded = newInspectionValue("brick_color")
		encoded.number_value = value.Number
		encoded.numbers = numbers
		encoded.labels = { name }
		return encoded
	elseif valueType == "NumberRange" then
		local numbers = finiteInspectionNumbers({ value.Min, value.Max })
		if numbers == nil or value.Min > value.Max then
			return nil
		end
		local encoded = newInspectionValue("number_range")
		encoded.numbers = numbers
		return encoded
	elseif valueType == "NumberSequence" then
		return encodeInspectionNumberSequence(value)
	elseif valueType == "ColorSequence" then
		return encodeInspectionColorSequence(value)
	elseif valueType == "Font" then
		return encodeInspectionFont(value)
	end
	return nil
end

local INSPECT_PROPERTY_ALLOWLIST = table.freeze({
	table.freeze({
		selector = "BasePart.Anchored",
		class_name = "BasePart",
		getter = function(target)
			return target.Anchored
		end,
	}),
	table.freeze({
		selector = "BasePart.CFrame",
		class_name = "BasePart",
		getter = function(target)
			return target.CFrame
		end,
	}),
	table.freeze({
		selector = "BasePart.CanCollide",
		class_name = "BasePart",
		getter = function(target)
			return target.CanCollide
		end,
	}),
	table.freeze({
		selector = "BasePart.CanQuery",
		class_name = "BasePart",
		getter = function(target)
			return target.CanQuery
		end,
	}),
	table.freeze({
		selector = "BasePart.CanTouch",
		class_name = "BasePart",
		getter = function(target)
			return target.CanTouch
		end,
	}),
	table.freeze({
		selector = "BasePart.CastShadow",
		class_name = "BasePart",
		getter = function(target)
			return target.CastShadow
		end,
	}),
	table.freeze({
		selector = "BasePart.CollisionGroup",
		class_name = "BasePart",
		getter = function(target)
			return target.CollisionGroup
		end,
	}),
	table.freeze({
		selector = "BasePart.Color",
		class_name = "BasePart",
		getter = function(target)
			return target.Color
		end,
	}),
	table.freeze({
		selector = "BasePart.Locked",
		class_name = "BasePart",
		getter = function(target)
			return target.Locked
		end,
	}),
	table.freeze({
		selector = "BasePart.Massless",
		class_name = "BasePart",
		getter = function(target)
			return target.Massless
		end,
	}),
	table.freeze({
		selector = "BasePart.Material",
		class_name = "BasePart",
		getter = function(target)
			return target.Material
		end,
	}),
	table.freeze({
		selector = "BasePart.MaterialVariant",
		class_name = "BasePart",
		getter = function(target)
			return target.MaterialVariant
		end,
	}),
	table.freeze({
		selector = "BasePart.Reflectance",
		class_name = "BasePart",
		getter = function(target)
			return target.Reflectance
		end,
	}),
	table.freeze({
		selector = "BasePart.Size",
		class_name = "BasePart",
		getter = function(target)
			return target.Size
		end,
	}),
	table.freeze({
		selector = "BasePart.Transparency",
		class_name = "BasePart",
		getter = function(target)
			return target.Transparency
		end,
	}),
	table.freeze({
		selector = "BaseScript.Enabled",
		class_name = "BaseScript",
		getter = function(target)
			return target.Enabled
		end,
	}),
	table.freeze({
		selector = "BaseScript.RunContext",
		class_name = "BaseScript",
		getter = function(target)
			return target.RunContext
		end,
	}),
	table.freeze({
		selector = "GuiObject.Active",
		class_name = "GuiObject",
		getter = function(target)
			return target.Active
		end,
	}),
	table.freeze({
		selector = "GuiObject.AnchorPoint",
		class_name = "GuiObject",
		getter = function(target)
			return target.AnchorPoint
		end,
	}),
	table.freeze({
		selector = "GuiObject.BackgroundColor3",
		class_name = "GuiObject",
		getter = function(target)
			return target.BackgroundColor3
		end,
	}),
	table.freeze({
		selector = "GuiObject.BackgroundTransparency",
		class_name = "GuiObject",
		getter = function(target)
			return target.BackgroundTransparency
		end,
	}),
	table.freeze({
		selector = "GuiObject.BorderColor3",
		class_name = "GuiObject",
		getter = function(target)
			return target.BorderColor3
		end,
	}),
	table.freeze({
		selector = "GuiObject.BorderSizePixel",
		class_name = "GuiObject",
		getter = function(target)
			return target.BorderSizePixel
		end,
	}),
	table.freeze({
		selector = "GuiObject.ClipsDescendants",
		class_name = "GuiObject",
		getter = function(target)
			return target.ClipsDescendants
		end,
	}),
	table.freeze({
		selector = "GuiObject.LayoutOrder",
		class_name = "GuiObject",
		getter = function(target)
			return target.LayoutOrder
		end,
	}),
	table.freeze({
		selector = "GuiObject.Position",
		class_name = "GuiObject",
		getter = function(target)
			return target.Position
		end,
	}),
	table.freeze({
		selector = "GuiObject.Rotation",
		class_name = "GuiObject",
		getter = function(target)
			return target.Rotation
		end,
	}),
	table.freeze({
		selector = "GuiObject.Size",
		class_name = "GuiObject",
		getter = function(target)
			return target.Size
		end,
	}),
	table.freeze({
		selector = "GuiObject.Visible",
		class_name = "GuiObject",
		getter = function(target)
			return target.Visible
		end,
	}),
	table.freeze({
		selector = "GuiObject.ZIndex",
		class_name = "GuiObject",
		getter = function(target)
			return target.ZIndex
		end,
	}),
	table.freeze({
		selector = "Instance.Archivable",
		class_name = "Instance",
		getter = function(target)
			return target.Archivable
		end,
	}),
	table.freeze({
		selector = "LayerCollector.Enabled",
		class_name = "LayerCollector",
		getter = function(target)
			return target.Enabled
		end,
	}),
	table.freeze({
		selector = "LayerCollector.ResetOnSpawn",
		class_name = "LayerCollector",
		getter = function(target)
			return target.ResetOnSpawn
		end,
	}),
	table.freeze({
		selector = "LayerCollector.ZIndexBehavior",
		class_name = "LayerCollector",
		getter = function(target)
			return target.ZIndexBehavior
		end,
	}),
})

local function inspectProperties(target)
	local properties = {}
	local complete = true
	for _, property in ipairs(INSPECT_PROPERTY_ALLOWLIST) do
		local classOk, applies = pcall(function()
			return target:IsA(property.class_name)
		end)
		if not classOk or type(applies) ~= "boolean" then
			adapterError(
				"inspection_class_check_failed",
				"Studio could not evaluate the fixed property allowlist",
				false
			)
		end
		if applies then
			local readOk, rawValue = pcall(property.getter, target)
			local encodedValue = nil
			if readOk then
				local encodeOk, candidate = pcall(
					encodeInspectionValue,
					rawValue
				)
				if encodeOk then
					encodedValue = candidate
				end
			end
			if encodedValue == nil then
				complete = false
				encodedValue = newInspectionValue(
					readOk and "unsupported" or "unavailable"
				)
			end
			table.insert(properties, {
				selector = property.selector,
				value = encodedValue,
			})
		end
	end
	return properties, complete
end

local function inspectAttributes(target)
	local readOk, attributes = pcall(function()
		return target:GetAttributes()
	end)
	if not readOk or type(attributes) ~= "table" then
		adapterError(
			"inspection_attributes_failed",
			"Studio could not read the target's attributes",
			false
		)
	end
	local names = {}
	for name in pairs(attributes) do
		if not isInspectionAttributeName(name) then
			adapterError(
				"inspection_attribute_name_unsupported",
				"An attribute name is outside the inspection encoding",
				false
			)
		end
		table.insert(names, name)
		if #names > DURABLE_BOUNDS.MAX_INSPECT_ATTRIBUTES_RAW then
			adapterError(
				"inspection_attributes_oversize",
				"Target attribute cardinality exceeds the raw inspection bound",
				false
			)
		end
	end
	table.sort(names)
	local returned = {}
	for index = 1, math.min(#names, DURABLE_BOUNDS.MAX_INSPECT_ATTRIBUTES_RETURNED) do
		local name = names[index]
		local encodeOk, encodedValue = pcall(
			encodeInspectionValue,
			attributes[name]
		)
		if not encodeOk or encodedValue == nil then
			adapterError(
				"inspection_attribute_value_unsupported",
				"An attribute value is outside the reviewed inspection encoding",
				false
			)
		end
		table.insert(returned, {
			name = name,
			value = encodedValue,
		})
	end
	return returned, #names
end

local function inspectTags(target)
	local readOk, tags = pcall(function()
		return target:GetTags()
	end)
	if not readOk or type(tags) ~= "table" then
		adapterError(
			"inspection_tags_failed",
			"Studio could not read the target's tags",
			false
		)
	end
	local count = #tags
	if count > DURABLE_BOUNDS.MAX_INSPECT_TAGS_RAW then
		adapterError(
			"inspection_tags_oversize",
			"Target tag cardinality exceeds the raw inspection bound",
			false
		)
	end
	for key in pairs(tags) do
		if not isInteger(key) or key < 1 or key > count then
			adapterError(
				"inspection_tags_invalid",
				"Studio returned a non-array tag collection",
				false
			)
		end
	end
	local copied = table.create(count)
	local seen = {}
	for index, tag in ipairs(tags) do
		if not isInspectionName(tag) or seen[tag] == true then
			adapterError(
				"inspection_tag_unsupported",
				"A tag is invalid, duplicated, or outside the inspection encoding",
				false
			)
		end
		seen[tag] = true
		copied[index] = tag
	end
	table.sort(copied)
	local returned = {}
	for index = 1, math.min(#copied, DURABLE_BOUNDS.MAX_INSPECT_TAGS_RETURNED) do
		returned[index] = copied[index]
	end
	return returned, #copied
end

local function isReusableInspectionSegment(name)
	return isInspectionName(name)
end

local function sortedInspectionChildren(instance)
	local readOk, children = pcall(function()
		return instance:GetChildren()
	end)
	if not readOk or type(children) ~= "table" then
		adapterError(
			"inspection_children_failed",
			"Studio could not read one bounded child collection",
			false
		)
	end
	if #children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE then
		adapterError(
			"inspection_width_unsupported",
			"One instance exceeds the inspection child-width bound",
			false
		)
	end
	local decorated = table.create(#children)
	for index, child in ipairs(children) do
		local name = child.Name
		local className = child.ClassName
		if not isInspectionName(name)
			or not isInspectionIdentifier(className)
		then
			adapterError(
				"inspection_child_identity_unsupported",
				"One child identity is outside the inspection encoding",
				false
			)
		end
		decorated[index] = {
			instance = child,
			name = name,
			class_name = className,
			original_index = index,
		}
	end
	table.sort(decorated, function(left, right)
		if left.name == right.name then
			if left.class_name == right.class_name then
				return left.original_index < right.original_index
			end
			return left.class_name < right.class_name
		end
		return left.name < right.name
	end)
	return decorated
end

local function inspectImmediateChildren(children, path, childLimit)
	local duplicateNames = {}
	for _, child in ipairs(children) do
		duplicateNames[child.name] =
			(duplicateNames[child.name] or 0) + 1
	end
	local returned = {}
	local encodedBytes = 0
	local reason = "complete"
	local maximum = math.min(#children, childLimit)
	for index = 1, maximum do
		local child = children[index]
		local addressable = #path < DURABLE_BOUNDS.MAX_PATH_SEGMENTS
			and duplicateNames[child.name] == 1
			and isReusableInspectionSegment(child.name)
		local childPath = addressable
			and appendPath(path, child.name) or {}
		local item = {
			name = child.name,
			class_name = child.class_name,
			addressable = addressable,
			path = childPath,
		}
		local encodeOk, encoded = pcall(function()
			return HttpService:JSONEncode(item)
		end)
		if not encodeOk or type(encoded) ~= "string" then
			adapterError(
				"inspection_child_encoding_failed",
				"Studio could not encode a bounded child summary",
				false
			)
		end
		if encodedBytes + #encoded + 1 > DURABLE_BOUNDS.MAX_INSPECT_CHILD_OUTPUT_BYTES then
			reason = "output_bytes"
			break
		end
		table.insert(returned, item)
		encodedBytes = encodedBytes + #encoded + 1
	end
	if reason == "complete" and #returned < #children then
		reason = "child_limit"
	end
	if reason ~= "complete" and #returned > 0 then
		local boundary = returned[#returned]
		boundary.addressable = false
		boundary.path = {}
	end
	return returned, reason
end

local function assertInspectionFrontierBound(frames, newChildren)
	local retained = #newChildren
	for _, frame in ipairs(frames) do
		if frame.children ~= nil then
			retained = retained + #frame.children
		end
	end
	if retained > DURABLE_BOUNDS.MAX_TREE_RETAINED_CHILDREN then
		adapterError(
			"inspection_frontier_unsupported",
			"Inspection traversal exceeds the retained-child frontier bound",
			false
		)
	end
end

local function newInspectionTraversal(root, rootChildren)
	assertInspectionFrontierBound({}, rootChildren)
	return {
		{
			instance = root,
			depth = 0,
			emitted = true,
			children = rootChildren,
			next_child = 1,
			depth_checked = false,
		},
	}
end

local function nextInspectionDescendant(frames, maxDepth, traversalState)
	while #frames > 0 do
		local frame = frames[#frames]
		if not frame.emitted then
			frame.emitted = true
			return frame.instance, frame.depth
		end
		if frame.depth < maxDepth then
			if frame.children == nil then
				local children = sortedInspectionChildren(frame.instance)
				assertInspectionFrontierBound(frames, children)
				frame.children = children
			end
			local ordinal = frame.next_child
			if ordinal <= #frame.children then
				frame.next_child = ordinal + 1
				local child = frame.children[ordinal]
				table.insert(frames, {
					instance = child.instance,
					depth = frame.depth + 1,
					emitted = false,
					children = nil,
					next_child = 1,
					depth_checked = false,
				})
			else
				table.remove(frames)
			end
		else
			if not frame.depth_checked then
				frame.depth_checked = true
				local children = sortedInspectionChildren(frame.instance)
				if #children > 0 then
					traversalState.depth_limited = true
				end
			end
			table.remove(frames)
		end
	end
	return nil, nil
end

local function fenceInspectionSnapshot(path, target, generation)
	assertExpectedDocument()
	requireEditMode()
	if peer.generation ~= generation then
		adapterError(
			"inspection_generation_changed",
			"Session generation changed during instance inspection",
			false
		)
	end
	local observed = resolveExactPath(path, false)
	if observed ~= target then
		adapterError(
			"inspection_target_changed",
			"Exact inspection path no longer resolves to the same target",
			false
		)
	end
end

local function inspectDescendants(
	target,
	rootChildren,
	maxDepth,
	scanLimit,
	timeLimitMs,
	startedAt,
	fence
)
	if #rootChildren == 0 then
		return 0, true, "complete", {}
	end
	if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
		return 0, false, "time_limit", {}
	end
	if maxDepth == 0 then
		return 0, false, "depth_limit", {}
	end
	local frames = newInspectionTraversal(target, rootChildren)
	local traversalState = { depth_limited = false }
	local descendantCount = 0
	local classCountMap = {}
	local classNameCount = 0
	local reason = "complete"

	while true do
		if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
			reason = "time_limit"
			break
		end
		local instance = nextInspectionDescendant(
			frames,
			maxDepth,
			traversalState
		)
		if instance == nil then
			if traversalState.depth_limited then
				reason = "depth_limit"
			end
			break
		end
		descendantCount = descendantCount + 1
		local className = instance.ClassName
		if not isInspectionIdentifier(className) then
			adapterError(
				"inspection_descendant_class_unsupported",
				"One descendant class is outside the inspection encoding",
				false
			)
		end
		if classCountMap[className] == nil then
			classNameCount = classNameCount + 1
			if classNameCount > DURABLE_BOUNDS.MAX_INSPECT_CLASS_COUNTS then
				adapterError(
					"inspection_class_counts_oversize",
					"Observed descendant classes exceed the bounded summary",
					false
				)
			end
			classCountMap[className] = 1
		else
			classCountMap[className] = classCountMap[className] + 1
		end

		if descendantCount
			% DURABLE_BOUNDS.INSPECT_COOPERATIVE_YIELD_INTERVAL == 0
		then
			task.wait()
			fence()
		end
		if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
			reason = "time_limit"
			break
		end
		if descendantCount >= scanLimit then
			local following = nextInspectionDescendant(
				frames,
				maxDepth,
				traversalState
			)
			if (os.clock() - startedAt) * 1_000 >= timeLimitMs then
				reason = "time_limit"
			elseif following ~= nil then
				reason = "scan_limit"
			elseif traversalState.depth_limited then
				reason = "depth_limit"
			end
			break
		end
	end

	local classNames = {}
	for className in pairs(classCountMap) do
		table.insert(classNames, className)
	end
	table.sort(classNames)
	local classCounts = table.create(#classNames)
	for index, className in ipairs(classNames) do
		classCounts[index] = {
			class_name = className,
			count = classCountMap[className],
		}
	end
	return descendantCount, reason == "complete", reason, classCounts
end

local function inspectInstance(args, requestId)
	local startedAt = os.clock()
	requireEditMode()
	local generation = peer.generation
	local path = copyPath(args.path)
	local childLimit = args.child_limit or DURABLE_BOUNDS.DEFAULT_INSPECT_CHILD_LIMIT
	local descendantMaxDepth = args.descendant_max_depth
		or (DURABLE_BOUNDS.MAX_PATH_SEGMENTS - #path)
	local descendantScanLimit = args.descendant_scan_limit
		or DURABLE_BOUNDS.DEFAULT_INSPECT_DESCENDANT_SCAN_LIMIT
	local timeLimitMs = args.time_limit_ms or DURABLE_BOUNDS.DEFAULT_INSPECT_TIME_LIMIT_MS
	local target = resolveExactPath(path, false)
	local fence = function()
		fenceInspectionSnapshot(path, target, generation)
	end
	local properties, propertiesComplete = inspectProperties(target)
	local attributes, attributesTotal = inspectAttributes(target)
	local tags, tagsTotal = inspectTags(target)
	local sortedChildren = sortedInspectionChildren(target)
	local children, childrenReason = inspectImmediateChildren(
		sortedChildren,
		path,
		childLimit
	)
	local descendantCount,
		descendantCountComplete,
		descendantReason,
		descendantClassCounts = inspectDescendants(
			target,
			sortedChildren,
			descendantMaxDepth,
			descendantScanLimit,
			timeLimitMs,
			startedAt,
			fence
		)
	fence()

	local result = newScriptResult("studio_inspect_instance", requestId)
	result.datamodel_type = "Edit"
	result.path = path
	result.name = target.Name
	result.class_name = target.ClassName
	result.snapshot_contract = PROTOCOL_METADATA.INSPECT_SNAPSHOT_CONTRACT
	result.property_allowlist_version = PROTOCOL_METADATA.INSPECT_PROPERTY_ALLOWLIST_VERSION
	result.value_encoding_version = PROTOCOL_METADATA.INSPECT_VALUE_ENCODING_VERSION
	result.sort_version = PROTOCOL_METADATA.INSPECT_SORT_VERSION
	result.child_limit = childLimit
	result.descendant_max_depth = descendantMaxDepth
	result.descendant_scan_limit = descendantScanLimit
	result.time_limit_ms = timeLimitMs
	result.properties = properties
	result.property_count = #properties
	result.properties_complete = propertiesComplete
	result.attributes = attributes
	result.attributes_total = attributesTotal
	result.attributes_returned = #attributes
	result.attributes_truncated = #attributes < attributesTotal
	result.tags = tags
	result.tags_total = tagsTotal
	result.tags_returned = #tags
	result.tags_truncated = #tags < tagsTotal
	result.children = children
	result.children_total = #sortedChildren
	result.children_returned = #children
	result.children_truncated = childrenReason ~= "complete"
	result.children_truncation_reason = childrenReason
	result.descendant_count = descendantCount
	result.descendant_count_complete = descendantCountComplete
	result.descendant_truncation_reason = descendantReason
	result.descendant_class_counts = descendantClassCounts
	result.output_limit_bytes = DURABLE_BOUNDS.MAX_INSPECT_RESULT_BYTES

	local encodeOk, encoded = pcall(function()
		return HttpService:JSONEncode(result)
	end)
	if not encodeOk
		or type(encoded) ~= "string"
		or #encoded > DURABLE_BOUNDS.MAX_INSPECT_RESULT_BYTES
	then
		adapterError(
			"inspection_result_oversize",
			"Instance inspection exceeds its encoded result bound",
			false
		)
	end
	return result
end

local function appendCanonicalMultiEditValue(parts, value)
	local text = tostring(value)
	table.insert(parts, tostring(#text))
	table.insert(parts, ":")
	table.insert(parts, text)
	table.insert(parts, ";")
end

local function appendCanonicalMultiEditPath(parts, path)
	appendCanonicalMultiEditValue(parts, #path)
	for _, segment in ipairs(path) do
		appendCanonicalMultiEditValue(parts, segment)
	end
end

local function multiEditPrepareSha256(receipt)
	local parts = {}
	for _, value in ipairs({
		"studio-multi-edit-prepare-v2",
		receipt.studio_id,
		receipt.client_instance_id,
		receipt.document_epoch,
		receipt.generation,
		receipt.request_id,
		receipt.transaction_id,
		receipt.ordering_version,
		receipt.atomicity,
		receipt.target_count,
		receipt.edit_count,
		receipt.create_count,
		receipt.aggregate_source_bytes,
		receipt.aggregate_planned_source_bytes,
	}) do
		appendCanonicalMultiEditValue(parts, value)
	end
	appendCanonicalMultiEditValue(parts, #receipt.targets)
	for _, target in ipairs(receipt.targets) do
		appendCanonicalMultiEditValue(parts, target.index)
		appendCanonicalMultiEditValue(parts, target.kind)
		appendCanonicalMultiEditPath(parts, target.path)
		if target.kind == "edit" then
			for _, fieldName in ipairs({
				"expected_sha256",
				"prepared_sha256",
				"planned_sha256",
				"source_length",
				"planned_source_length",
				"edit_count",
				"replacement_count",
				"status",
			}) do
				appendCanonicalMultiEditValue(parts, target[fieldName])
			end
		else
			appendCanonicalMultiEditPath(parts, target.parent_path)
			for _, fieldName in ipairs({
				"name",
				"class_name",
				"expected_absent",
				"prepared_absent",
				"planned_sha256",
				"planned_source_length",
				"status",
			}) do
				appendCanonicalMultiEditValue(parts, target[fieldName])
			end
		end
	end
	return sourceSha256(table.concat(parts))
end

local function multiEditMutationSha256(receipt)
	local parts = {}
	for _, value in ipairs({
		"studio-multi-edit-mutation-v2",
		receipt.phase,
		receipt.studio_id,
		receipt.client_instance_id,
		receipt.document_epoch,
		receipt.generation,
		receipt.request_id,
		receipt.transaction_id,
		receipt.prepare_request_id,
		receipt.prepare_sha256,
		receipt.ordering_version,
		receipt.atomicity,
		receipt.receipt_contract,
		receipt.evidence_mode,
		receipt.prior_terminal_outcome,
		receipt.prior_terminal_receipt_sha256,
		receipt.outcome,
		receipt.safe_terminal,
		receipt.recovery_required,
		receipt.target_count,
		receipt.edit_count,
		receipt.create_count,
	}) do
		appendCanonicalMultiEditValue(parts, value)
	end
	appendCanonicalMultiEditValue(parts, #receipt.targets)
	for _, target in ipairs(receipt.targets) do
		appendCanonicalMultiEditValue(parts, target.index)
		appendCanonicalMultiEditValue(parts, target.kind)
		appendCanonicalMultiEditPath(parts, target.path)
		if target.kind == "edit" then
			for _, fieldName in ipairs({
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
			}) do
				appendCanonicalMultiEditValue(parts, target[fieldName])
			end
		else
			appendCanonicalMultiEditPath(parts, target.parent_path)
			for _, fieldName in ipairs({
				"name",
				"class_name",
				"expected_absent",
				"planned_sha256",
				"planned_source_length",
				"observed_before_state",
				"observed_after_state",
				"observed_after_class_name",
				"observed_after_sha256",
				"status",
			}) do
				appendCanonicalMultiEditValue(parts, target[fieldName])
			end
		end
	end
	return sourceSha256(table.concat(parts))
end

local function copyPreparedMultiEditTarget(target)
	if target.kind == "create" then
		return {
			index = target.index,
			kind = "create",
			path = copyPath(target.path),
			parent_path = copyPath(target.parent_path),
			name = target.name,
			class_name = target.class_name,
			expected_absent = true,
			prepared_absent = true,
			planned_sha256 = target.planned_sha256,
			planned_source_length = target.planned_source_length,
			status = "prepared",
		}
	end
	return {
		index = target.index,
		kind = "edit",
		path = copyPath(target.path),
		expected_sha256 = target.expected_sha256,
		prepared_sha256 = target.prepared_sha256,
		planned_sha256 = target.planned_sha256,
		source_length = target.source_length,
		planned_source_length = target.planned_source_length,
		edit_count = target.edit_count,
		replacement_count = target.replacement_count,
		status = "prepared",
	}
end

local function multiEditRangesOverlap(leftStart, leftEnd, rightStart, rightEnd)
	return leftStart <= rightEnd and rightStart <= leftEnd
end

local function requireMultiEditTime(deadline)
	if os.clock() > deadline then
		adapterError(
			"multi_edit_deadline",
			"Multi-edit exhausted its bounded planning deadline",
			false
		)
	end
end

local function findMultiEditMatches(source, edit, taintedRanges, deadline)
	requireMultiEditTime(deadline)
	local matches = {}
	if edit.start_byte ~= nil then
		local startIndex = edit.start_byte + 1
		local endIndex = edit.end_byte
		if string.sub(source, startIndex, endIndex) ~= edit.old_string then
			adapterError(
				"multi_edit_stale_span",
				"Explicit byte range no longer selects old_string",
				false
			)
		end
		table.insert(matches, { start_index = startIndex, end_index = endIndex })
	elseif edit.replace_all == true then
		local cursor = 1
		while cursor <= #source do
			requireMultiEditTime(deadline)
			local startIndex, endIndex = string.find(
				source,
				edit.old_string,
				cursor,
				true
			)
			if startIndex == nil then
				break
			end
			table.insert(matches, {
				start_index = startIndex,
				end_index = endIndex,
			})
			if #matches > DURABLE_BOUNDS.MAX_MULTI_EDIT_REPLACEMENT_SPANS then
				adapterError(
					"multi_edit_span_limit",
					"Expanded replacement spans exceed the durable bound",
					false
				)
			end
			cursor = endIndex + 1
		end
		if #matches == 0 then
			adapterError(
				"multi_edit_match_missing",
				"replace_all old_string was not found",
				false
			)
		end
	else
		local startIndex, endIndex = string.find(
			source,
			edit.old_string,
			1,
			true
		)
		if startIndex == nil then
			adapterError(
				"multi_edit_match_missing",
				"old_string was not found",
				false
			)
		end
		local secondStart = string.find(
			source,
			edit.old_string,
			startIndex + 1,
			true
		)
		if secondStart ~= nil then
			adapterError(
				"multi_edit_match_ambiguous",
				"old_string has multiple matches; use offsets or replace_all",
				false
			)
		end
		table.insert(matches, { start_index = startIndex, end_index = endIndex })
	end
	for _, match in ipairs(matches) do
		for _, tainted in ipairs(taintedRanges) do
			local overlaps = tainted.zero_width == true
				and match.start_index < tainted.start_index
				and match.end_index >= tainted.start_index
				or (tainted.zero_width ~= true
					and multiEditRangesOverlap(
						match.start_index,
						match.end_index,
						tainted.start_index,
						tainted.end_index
					))
			if overlaps then
				adapterError(
					"multi_edit_overlap",
					"An edit overlaps bytes changed by an earlier edit",
					false
				)
			end
		end
	end
	return matches
end

local function applyPlannedEdit(
	source,
	edit,
	taintedRanges,
	matches,
	deadline
)
	for matchIndex = #matches, 1, -1 do
		requireMultiEditTime(deadline)
		local match = matches[matchIndex]
		local oldLength = match.end_index - match.start_index + 1
		local newLength = #edit.new_string
		local delta = newLength - oldLength
		for _, tainted in ipairs(taintedRanges) do
			if tainted.start_index > match.end_index then
				tainted.start_index = tainted.start_index + delta
				tainted.end_index = tainted.end_index + delta
			end
		end
		source = string.sub(source, 1, match.start_index - 1)
			.. edit.new_string
			.. string.sub(source, match.end_index + 1)
		if newLength > 0 then
			table.insert(taintedRanges, {
				start_index = match.start_index,
				end_index = match.start_index + newLength - 1,
				zero_width = false,
			})
		else
			-- A deletion changes the boundary, not an occupied byte. A later
			-- edit may begin at that boundary (for example a→"" followed by
			-- b→B in "ab"), but a range spanning across it is overlapping.
			table.insert(taintedRanges, {
				start_index = match.start_index,
				end_index = match.start_index - 1,
				zero_width = true,
			})
		end
	end
	if #source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES or not validUtf8(source) then
		adapterError(
			"multi_edit_planned_source_invalid",
			"Planned source is invalid UTF-8 or exceeds the source bound",
			false
		)
	end
	return source
end

local function planMultiEditSource(source, edits, deadline)
	local planned = source
	local taintedRanges = {}
	local replacementCount = 0
	for _, edit in ipairs(edits) do
		requireMultiEditTime(deadline)
		local matches = findMultiEditMatches(
			planned,
			edit,
			taintedRanges,
			deadline
		)
		replacementCount = replacementCount + #matches
		if replacementCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_REPLACEMENT_SPANS then
			adapterError(
				"multi_edit_span_limit",
				"Expanded replacement spans exceed the durable bound",
				false
			)
		end
		planned = applyPlannedEdit(
			planned,
			edit,
			taintedRanges,
			matches,
			deadline
		)
	end
	return planned, replacementCount
end

local function exactNamedChild(parent, name)
	local children = parent:GetChildren()
	if #children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE then
		adapterError(
			"tree_width_unsupported",
			"Exact create-path resolution exceeds the bounded child width",
			false
		)
	end
	local match = nil
	local count = 0
	for _, child in ipairs(children) do
		if child.Name == name then
			count = count + 1
			match = child
		end
	end
	return match, count
end

local function prepareMultiEditExistingTarget(
	requested,
	targetIndex,
	deadline,
	seenInstances
)
	requireMultiEditTime(deadline)
	assertExpectedDocument()
	local target = requireLuaSourceContainer(requested.path)
	if seenInstances[target] == true then
		adapterError(
			"multi_edit_duplicate_target",
			"Two paths resolved to the same target Instance",
			false
		)
	end
	seenInstances[target] = true
	local source = readEditorSource(target)
	if requireLuaSourceContainer(requested.path) ~= target then
		adapterError(
			"multi_edit_target_moved",
			"Exact target identity changed during source read",
			false
		)
	end
	if not validUtf8(source) or #source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES then
		adapterError(
			"multi_edit_source_invalid",
			"Target source is invalid UTF-8 or exceeds the source bound",
			false
		)
	end
	local preparedSha256 = sourceSha256(source)
	if preparedSha256 ~= requested.expected_sha256 then
		adapterError(
			"multi_edit_revision_conflict",
			"One target no longer matches its expected revision",
			false
		)
	end
	local planned, replacementCount = planMultiEditSource(
		source,
		requested.edits,
		deadline
	)
	return {
		index = targetIndex,
		kind = "edit",
		path = copyPath(requested.path),
		target = target,
		expected_sha256 = requested.expected_sha256,
		prepared_sha256 = preparedSha256,
		planned_sha256 = sourceSha256(planned),
		source_length = #source,
		planned_source_length = #planned,
		edit_count = #requested.edits,
		replacement_count = replacementCount,
		original_source = source,
		planned_source = planned,
	}, #source, #planned, replacementCount
end

local function prepareMultiEditCreateTarget(requested, targetIndex, deadline)
	requireMultiEditTime(deadline)
	assertExpectedDocument()
	local parent = resolveExactPath(requested.parent_path, false)
	local existing, matchCount = exactNamedChild(parent, requested.name)
	if existing ~= nil or matchCount ~= 0 then
		adapterError(
			"multi_edit_create_revision_conflict",
			"Create target is no longer absent beneath its exact parent",
			false
		)
	end
	local plannedSha256 = sourceSha256(requested.source)
	return {
		index = targetIndex,
		kind = "create",
		path = appendPath(requested.parent_path, requested.name),
		parent_path = copyPath(requested.parent_path),
		parent = parent,
		name = requested.name,
		class_name = requested.class_name,
		expected_absent = true,
		prepared_absent = true,
		planned_sha256 = plannedSha256,
		planned_source_length = #requested.source,
		planned_source = requested.source,
		created_instance = nil,
	}, #requested.source
end

local function prepareMultiEdit(args, requestId, deadlineMs)
	requireEditMode()
	local deadline = os.clock() + (deadlineMs / 1_000)
	local existing = peer.multi_edit_plan
	if existing ~= nil
		and (
			existing.status == "applying"
			or existing.status == "recovery_required"
		)
	then
		adapterError(
			"multi_edit_recovery_required",
			"An earlier multi-edit requires exact recovery",
			false
		)
	end
	local totalEditCount, createCount = validateMultiEditTargets(
		args.targets,
		args.creates
	)
	local planTargets = {}
	local receiptTargets = {}
	local seenInstances = {}
	local aggregateSourceBytes = 0
	local aggregatePlannedSourceBytes = 0
	local aggregateReplacementCount = 0
	for targetIndex, requested in ipairs(args.targets) do
		local planTarget,
			sourceBytes,
			plannedBytes,
			replacementCount = prepareMultiEditExistingTarget(
				requested,
				targetIndex,
				deadline,
				seenInstances
			)
		aggregateReplacementCount =
			aggregateReplacementCount + replacementCount
		if aggregateReplacementCount > DURABLE_BOUNDS.MAX_MULTI_EDIT_REPLACEMENT_SPANS then
			adapterError(
				"multi_edit_span_limit",
				"Expanded replacement spans exceed the aggregate durable bound",
				false
			)
		end
		aggregateSourceBytes = aggregateSourceBytes + sourceBytes
		aggregatePlannedSourceBytes =
			aggregatePlannedSourceBytes + plannedBytes
		if aggregateSourceBytes > DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES
			or aggregatePlannedSourceBytes
				> DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES
		then
			adapterError(
				"multi_edit_aggregate_source_limit",
				"Aggregate staged source exceeds the durable bound",
				false
			)
		end
		table.insert(planTargets, planTarget)
		table.insert(
			receiptTargets,
			copyPreparedMultiEditTarget(planTarget)
		)
	end
	for createIndex, requested in ipairs(args.creates or {}) do
		local targetIndex = #args.targets + createIndex
		local planTarget, plannedBytes = prepareMultiEditCreateTarget(
			requested,
			targetIndex,
			deadline
		)
		aggregatePlannedSourceBytes =
			aggregatePlannedSourceBytes + plannedBytes
		if aggregatePlannedSourceBytes
			> DURABLE_BOUNDS.MAX_MULTI_EDIT_AGGREGATE_SOURCE_BYTES
		then
			adapterError(
				"multi_edit_aggregate_source_limit",
				"Aggregate staged source exceeds the durable bound",
				false
			)
		end
		table.insert(planTargets, planTarget)
		table.insert(
			receiptTargets,
			copyPreparedMultiEditTarget(planTarget)
		)
	end
	assertExpectedDocument()
	local receipt = {
		adapter = "studio-mcp-v2-durable-plugin",
		v = 2,
		operation = "studio_multi_edit",
		phase = "prepare",
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		request_id = requestId,
		transaction_id = args.transaction_id,
		ordering_version = PROTOCOL_METADATA.MULTI_EDIT_ORDERING_VERSION,
		atomicity = PROTOCOL_METADATA.MULTI_EDIT_ATOMICITY,
		target_count = #planTargets,
		edit_count = totalEditCount,
		create_count = createCount,
		aggregate_source_bytes = aggregateSourceBytes,
		aggregate_planned_source_bytes = aggregatePlannedSourceBytes,
		targets = receiptTargets,
		expires_in_ms = DURABLE_BOUNDS.MULTI_EDIT_PREPARE_TTL_SECONDS * 1_000,
	}
	receipt.prepare_sha256 = multiEditPrepareSha256(receipt)
	local receiptOk, encodedReceipt = pcall(function()
		return HttpService:JSONEncode(receipt)
	end)
	if not receiptOk
		or type(encodedReceipt) ~= "string"
		or #encodedReceipt > DURABLE_BOUNDS.MAX_MULTI_EDIT_RECEIPT_BYTES
	then
		adapterError(
			"multi_edit_receipt_oversize",
			"Prepared multi-edit receipt exceeds the durable output bound",
			false
		)
	end
	peer.multi_edit_plan = {
		transaction_id = args.transaction_id,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		prepare_request_id = requestId,
		prepare_sha256 = receipt.prepare_sha256,
		expires_at = os.clock() + DURABLE_BOUNDS.MULTI_EDIT_PREPARE_TTL_SECONDS,
		status = "prepared",
		targets = planTargets,
		edit_count = totalEditCount,
		create_count = createCount,
	}
	return receipt
end

local function multiEditCasReplace(
	target,
	path,
	expectedSha256,
	replacement,
	allowAlreadyReplacement
)
	local replacementSha256 = sourceSha256(replacement)
	local callbackInvoked = false
	local sawExpected = false
	local conflictingRevision = false
	local mutationFenceFailed = false
	local dispatchFenceOk = pcall(function()
		requireEditMode()
		assertExpectedDocument()
		if requireLuaSourceContainer(path) ~= target then
			error("exact multi-edit target identity changed")
		end
	end)
	if not dispatchFenceOk then
		return false, "", true
	end
	local updateOk = pcall(function()
		OPTIONAL_SERVICES.ScriptEditorService:UpdateSourceAsync(target, function(currentSource)
			callbackInvoked = true
			if type(currentSource) ~= "string"
				or #currentSource > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
				or not validUtf8(currentSource)
			then
				conflictingRevision = true
				return currentSource
			end
			local currentSha256 = sourceSha256(currentSource)
				if currentSha256 == expectedSha256 then
					local commitFenceOk = pcall(function()
						requireEditMode()
						assertExpectedDocument()
						if requireLuaSourceContainer(path) ~= target then
							error("exact multi-edit target identity changed")
						end
					end)
				if not commitFenceOk then
					mutationFenceFailed = true
					return currentSource
				end
				sawExpected = true
				return replacement
			end
			if allowAlreadyReplacement == true
				and currentSha256 == replacementSha256
			then
				return currentSource
			end
			conflictingRevision = true
			return currentSource
		end)
	end)
	local confirmedOk, confirmedSource = pcall(function()
		return readEditorSource(target)
	end)
	if not confirmedOk
		or type(confirmedSource) ~= "string"
		or #confirmedSource > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
		or not validUtf8(confirmedSource)
	then
		return false, "", true
	end
	local identityConfirmed = pcall(function()
		if requireLuaSourceContainer(path) ~= target then
			error("exact multi-edit target identity changed")
		end
	end)
	if not identityConfirmed then
		return false, "", true
	end
	local confirmedSha256 = sourceSha256(confirmedSource)
	if updateOk
		and callbackInvoked
		and not mutationFenceFailed
		and not conflictingRevision
		and (
			sawExpected
			or (
				allowAlreadyReplacement == true
				and confirmedSha256 == replacementSha256
			)
		)
		and confirmedSha256 == replacementSha256
		and confirmedSource == replacement
	then
		return true, confirmedSha256, false
	end
	return false,
			confirmedSha256,
			mutationFenceFailed or confirmedSha256 ~= expectedSha256
end

local function multiEditCreatedContentState(created)
	local inspectOk, changed = pcall(function()
		local children = created:GetChildren()
		if type(children) ~= "table"
			or #children > DURABLE_BOUNDS.MAX_TREE_CHILDREN_PER_INSTANCE
		then
			error("created child state is unavailable or unbounded")
		end
		if #children > 0 then
			return true
		end
		local attributes = created:GetAttributes()
		if type(attributes) ~= "table" then
			error("created attribute state is unavailable")
		end
		local attributeCount = 0
		for _ in pairs(attributes) do
			attributeCount = attributeCount + 1
			if attributeCount > DURABLE_BOUNDS.MAX_INSPECT_ATTRIBUTES_RAW then
				error("created attribute state is unbounded")
			end
		end
		if attributeCount > 0 then
			return true
		end
		local tags = created:GetTags()
		if type(tags) ~= "table"
			or #tags > DURABLE_BOUNDS.MAX_INSPECT_TAGS_RAW
		then
			error("created tag state is unavailable or unbounded")
		end
		return #tags > 0
	end)
	if not inspectOk then
		return "unavailable"
	end
	return changed and "present_unproven" or "empty"
end

local function observeMultiEditCreate(planTarget)
	local parentOk, parent = pcall(function()
		return resolveExactPath(planTarget.parent_path, false)
	end)
	if not parentOk or parent ~= planTarget.parent then
		return "unavailable", "", ""
	end
	local childOk, child, matchCount = pcall(function()
		local match, count = exactNamedChild(parent, planTarget.name)
		return match, count
	end)
	if not childOk then
		return "unavailable", "", ""
	end
	if matchCount == 0 then
		if planTarget.created_instance ~= nil
			and planTarget.created_instance.Parent ~= nil
		then
			return "present_unproven", "", ""
		end
		return "absent", "", ""
	end
	if matchCount ~= 1
		or planTarget.created_instance == nil
		or child ~= planTarget.created_instance
		or child.Parent ~= parent
		or child.Name ~= planTarget.name
		or child.ClassName ~= planTarget.class_name
	then
		return "present_unproven", "", ""
	end
	local contentState = multiEditCreatedContentState(child)
	if contentState ~= "empty" then
		return contentState, "", ""
	end
	local sourceOk, source = pcall(function()
		return readEditorSource(child)
	end)
	if not sourceOk
		or type(source) ~= "string"
		or not validUtf8(source)
		or #source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
	then
		return "unavailable", "", ""
	end
	local reboundOk, reboundParent, reboundChild, reboundCount =
		pcall(function()
			local exactParent =
				resolveExactPath(planTarget.parent_path, false)
			local exactChild, count =
				exactNamedChild(exactParent, planTarget.name)
			return exactParent, exactChild, count
		end)
	if not reboundOk
		or reboundParent ~= planTarget.parent
		or reboundCount ~= 1
		or reboundChild ~= child
	then
		return "present_unproven", "", ""
	end
	local digest = sourceSha256(source)
	if source ~= planTarget.planned_source
		or digest ~= planTarget.planned_sha256
	then
		return "present_unproven", "", ""
	end
	return "created_exact", planTarget.class_name, digest
end

local function discardUnparentedMultiEditCreate(created)
	if created == nil or created.Parent ~= nil then
		return false
	end
	local mutationFenceOk = pcall(function()
		requireEditMode()
		assertExpectedDocument()
	end)
	if not mutationFenceOk then
		return false
	end
	pcall(function()
		created:Destroy()
	end)
	return created.Parent == nil
end

local function applyMultiEditCreate(planTarget)
	local beforeState = observeMultiEditCreate(planTarget)
	if beforeState ~= "absent" then
		return false, false, beforeState, "", ""
	end
	local created = nil
	local constructOk = pcall(function()
		created = Instance.new(planTarget.class_name)
		created.Name = planTarget.name
		created.Source = planTarget.planned_source
	end)
	if not constructOk
		or created == nil
		or created.Parent ~= nil
		or created.Name ~= planTarget.name
		or created.ClassName ~= planTarget.class_name
	then
		discardUnparentedMultiEditCreate(created)
		local state = observeMultiEditCreate(planTarget)
		return false, false, state, "", ""
	end
	local stagedSourceOk, stagedSource = pcall(function()
		return created.Source
	end)
	if not stagedSourceOk
		or stagedSource ~= planTarget.planned_source
		or sourceSha256(stagedSource) ~= planTarget.planned_sha256
	then
		discardUnparentedMultiEditCreate(created)
		local state = observeMultiEditCreate(planTarget)
		return false, false, state, "", ""
	end
	assertExpectedDocument()
	local parentOk, parent = pcall(function()
		return resolveExactPath(planTarget.parent_path, false)
	end)
	if not parentOk or parent ~= planTarget.parent then
		discardUnparentedMultiEditCreate(created)
		local state = observeMultiEditCreate(planTarget)
		return false, false, state, "", ""
	end
	local childOk, existing, matchCount = pcall(function()
		local match, count = exactNamedChild(parent, planTarget.name)
		return match, count
	end)
	if not childOk
		or existing ~= nil
		or matchCount ~= 0
	then
		discardUnparentedMultiEditCreate(created)
		local state = observeMultiEditCreate(planTarget)
		return false, false, state, "", ""
	end
	local mutationFenceFailed = false
	local parentedOk = pcall(function()
		local reboundParent =
			resolveExactPath(planTarget.parent_path, false)
		if reboundParent ~= parent then
			error("exact create parent identity changed")
		end
		local finalExisting, finalMatchCount =
			exactNamedChild(parent, planTarget.name)
		if finalExisting ~= nil or finalMatchCount ~= 0 then
			error("exact create path is no longer absent")
		end
		local commitFenceOk = pcall(function()
			requireEditMode()
			assertExpectedDocument()
		end)
		if not commitFenceOk then
			mutationFenceFailed = true
			error("multi-edit create mutation fence failed")
		end
		created.Parent = parent
	end)
	if not parentedOk then
		discardUnparentedMultiEditCreate(created)
		local state, className, digest =
			observeMultiEditCreate(planTarget)
		if mutationFenceFailed and state == "absent" then
			state = "unavailable"
			className = ""
			digest = ""
		end
		return false, false, state, className, digest
	end
	local mutated = created.Parent ~= nil
	if mutated then
		planTarget.created_instance = created
	end
	local state, className, digest = observeMultiEditCreate(planTarget)
	return state == "created_exact", mutated, state, className, digest
end

local function compensateMultiEditCreate(planTarget)
	local beforeState, beforeClass, beforeDigest =
		observeMultiEditCreate(planTarget)
	if beforeState == "absent"
		and planTarget.created_instance ~= nil
		and planTarget.created_instance.Parent == nil
	then
		return true, beforeState, beforeState, "", ""
	end
	if beforeState ~= "created_exact"
		or beforeClass ~= planTarget.class_name
		or beforeDigest ~= planTarget.planned_sha256
	then
		return false, beforeState, beforeState, beforeClass, beforeDigest
	end
	local created = planTarget.created_instance
	local contentState = multiEditCreatedContentState(created)
	if contentState ~= "empty" then
		return false, contentState, contentState, "", ""
	end
	local finalState, finalClass, finalDigest =
		observeMultiEditCreate(planTarget)
	if finalState ~= "created_exact"
		or finalClass ~= planTarget.class_name
		or finalDigest ~= planTarget.planned_sha256
	then
		return false, finalState, finalState, finalClass, finalDigest
	end
	local destroyedOk = pcall(function()
		local commitFenceOk = pcall(function()
			requireEditMode()
			assertExpectedDocument()
		end)
		if not commitFenceOk then
			error("multi-edit compensation mutation fence failed")
		end
		local reboundParent =
			resolveExactPath(planTarget.parent_path, false)
		if reboundParent ~= planTarget.parent then
			error("exact create parent identity changed")
		end
		local exact, matchCount =
			exactNamedChild(planTarget.parent, planTarget.name)
		if matchCount ~= 1
			or exact ~= created
			or multiEditCreatedContentState(created) ~= "empty"
		then
			error("created script identity changed before compensation")
		end
		created:Destroy()
	end)
	if not destroyedOk then
		local retainedState, retainedClass, retainedDigest =
			observeMultiEditCreate(planTarget)
		return false,
			retainedState,
			retainedState,
			retainedClass,
			retainedDigest
	end
	local afterState, afterClass, afterDigest =
		observeMultiEditCreate(planTarget)
	if created.Parent == nil
		and afterState == "absent"
	then
		return true, beforeState, afterState, "", ""
	end
	return false, beforeState, afterState, afterClass, afterDigest
end

local function preparedMultiEditTargetMatches(supplied, expected)
	if supplied.index ~= expected.index
		or supplied.kind ~= expected.kind
		or supplied.status ~= expected.status
		or not pathsEqual(supplied.path, expected.path)
	then
		return false
	end
	if expected.kind == "create" then
		return pathsEqual(supplied.parent_path, expected.parent_path)
			and supplied.name == expected.name
			and supplied.class_name == expected.class_name
			and supplied.expected_absent == expected.expected_absent
			and supplied.prepared_absent == expected.prepared_absent
			and supplied.planned_sha256 == expected.planned_sha256
			and supplied.planned_source_length
				== expected.planned_source_length
	end
	return supplied.expected_sha256 == expected.expected_sha256
		and supplied.prepared_sha256 == expected.prepared_sha256
		and supplied.planned_sha256 == expected.planned_sha256
		and supplied.source_length == expected.source_length
		and supplied.planned_source_length == expected.planned_source_length
		and supplied.edit_count == expected.edit_count
		and supplied.replacement_count == expected.replacement_count
end

local function newMultiEditTargetOutcome(planTarget)
	if planTarget.kind == "create" then
		return {
			index = planTarget.index,
			kind = "create",
			path = copyPath(planTarget.path),
			parent_path = copyPath(planTarget.parent_path),
			name = planTarget.name,
			class_name = planTarget.class_name,
			expected_absent = true,
			planned_sha256 = planTarget.planned_sha256,
			planned_source_length = planTarget.planned_source_length,
			observed_before_state = "absent",
			observed_after_state = "absent",
			observed_after_class_name = "",
			observed_after_sha256 = "",
			status = "not_created",
		}
	end
	return {
		index = planTarget.index,
		kind = "edit",
		path = copyPath(planTarget.path),
		expected_sha256 = planTarget.expected_sha256,
		prepared_sha256 = planTarget.prepared_sha256,
		planned_sha256 = planTarget.planned_sha256,
		observed_before_sha256 = planTarget.prepared_sha256,
		observed_after_sha256 = planTarget.prepared_sha256,
		source_length = planTarget.source_length,
		planned_source_length = planTarget.planned_source_length,
		edit_count = planTarget.edit_count,
		replacement_count = planTarget.replacement_count,
		status = "not_applied",
	}
end

local function copyMultiEditTargetOutcomes(targetOutcomes)
	local copied = {}
	for index, outcome in ipairs(targetOutcomes) do
		local targetCopy = {}
		for key, value in pairs(outcome) do
			if key == "path" or key == "parent_path" then
				targetCopy[key] = copyPath(value)
			else
				targetCopy[key] = value
			end
		end
		copied[index] = targetCopy
	end
	return copied
end

local function newMultiEditMutationReceipt(
	plan,
	requestId,
	phase,
	outcome,
	safeTerminal,
	targetOutcomes,
	evidenceMode,
	priorTerminalOutcome,
	priorTerminalReceiptSha256
)
	local effectiveEvidenceMode = evidenceMode
		or (phase == "apply" and "apply_execution" or "live_recovery")
	local receipt = {
		adapter = "studio-mcp-v2-durable-plugin",
		v = 2,
		operation = phase == "recover"
			and "studio_recover_multi_edit"
			or "studio_multi_edit",
		phase = phase,
		studio_id = peer.studio_id,
		client_instance_id = CLIENT_INSTANCE_ID,
		document_epoch = DOCUMENT_EPOCH,
		generation = peer.generation,
		request_id = requestId,
		transaction_id = plan.transaction_id,
		prepare_request_id = plan.prepare_request_id,
		prepare_sha256 = plan.prepare_sha256,
		ordering_version = PROTOCOL_METADATA.MULTI_EDIT_ORDERING_VERSION,
		atomicity = PROTOCOL_METADATA.MULTI_EDIT_ATOMICITY,
		receipt_contract = PROTOCOL_METADATA.MULTI_EDIT_RECEIPT_CONTRACT,
		evidence_mode = effectiveEvidenceMode,
		prior_terminal_outcome = priorTerminalOutcome or "",
		prior_terminal_receipt_sha256 =
			priorTerminalReceiptSha256 or "",
		outcome = outcome,
		safe_terminal = safeTerminal,
		recovery_required = not safeTerminal,
		target_count = #plan.targets,
		edit_count = plan.edit_count,
		create_count = plan.create_count,
		targets = targetOutcomes,
	}
	receipt.receipt_sha256 = multiEditMutationSha256(receipt)
	local receiptOk, encodedReceipt = pcall(function()
		return HttpService:JSONEncode(receipt)
	end)
	if not receiptOk
		or type(encodedReceipt) ~= "string"
		or #encodedReceipt > DURABLE_BOUNDS.MAX_MULTI_EDIT_RECEIPT_BYTES
	then
		adapterError(
			"multi_edit_receipt_oversize",
			"Multi-edit mutation receipt exceeds the durable output bound",
			false
		)
	end
	if safeTerminal
		and (
			(phase == "apply"
				and (
					outcome == "aborted_preflight"
					or outcome == "rolled_back"
				))
			or (phase == "recover" and outcome == "recovered")
		)
	then
		plan.safe_terminal_evidence = {
			status = plan.status,
			outcome = outcome,
			receipt_sha256 = receipt.receipt_sha256,
			targets = copyMultiEditTargetOutcomes(targetOutcomes),
		}
	end
	return receipt
end

local function assertMultiEditMutationReceiptBound(plan, requestId, phase)
	local worstCaseOutcomes = {}
	for index, planTarget in ipairs(plan.targets) do
		local outcome = newMultiEditTargetOutcome(planTarget)
		if planTarget.kind == "create" then
			outcome.observed_before_state = "present_unproven"
			outcome.observed_after_state = "present_unproven"
			outcome.observed_after_class_name = planTarget.class_name
			outcome.observed_after_sha256 = planTarget.planned_sha256
		else
			outcome.observed_before_sha256 = planTarget.prepared_sha256
			outcome.observed_after_sha256 = planTarget.planned_sha256
		end
		outcome.status = "recovery_required"
		worstCaseOutcomes[index] = outcome
	end
	newMultiEditMutationReceipt(
		plan,
		requestId,
		phase,
		"recovery_required",
		false,
		worstCaseOutcomes,
		"cached_safe_terminal",
		"aborted_preflight",
		string.rep("0", 64)
	)
end

local function applyMultiEdit(args, requestId, deadlineMs)
	requireEditMode()
	local deadline = os.clock() + (deadlineMs / 1_000)
	local plan = peer.multi_edit_plan
	if plan == nil
		or plan.transaction_id ~= args.transaction_id
		or plan.document_epoch ~= DOCUMENT_EPOCH
		or plan.generation ~= peer.generation
		or plan.prepare_request_id ~= args.prepare_request_id
		or plan.prepare_sha256 ~= args.prepare_sha256
		or plan.status ~= "prepared"
		or os.clock() > plan.expires_at
	then
		adapterError(
			"multi_edit_prepare_stale",
			"Exact prepared multi-edit plan is unavailable or stale",
			false
		)
	end
	if #args.prepared_targets ~= #plan.targets then
		adapterError(
			"multi_edit_prepare_mismatch",
			"Prepared target count changed before apply",
			false
		)
	end
	for index, planTarget in ipairs(plan.targets) do
		local supplied = args.prepared_targets[index]
		local expected = copyPreparedMultiEditTarget(planTarget)
		if not preparedMultiEditTargetMatches(supplied, expected) then
			adapterError(
				"multi_edit_prepare_mismatch",
				"Prepared target receipt changed before apply",
				false
			)
		end
	end
	assertMultiEditMutationReceiptBound(plan, requestId, "apply")
	plan.status = "applying"

	local outcomes = {}
	local preflightSafe = true
	for index, planTarget in ipairs(plan.targets) do
		local outcome = newMultiEditTargetOutcome(planTarget)
		outcomes[index] = outcome
		if os.clock() > deadline then
			preflightSafe = false
			if planTarget.kind == "create" then
				outcome.observed_before_state = "unavailable"
				outcome.observed_after_state = "unavailable"
			else
				outcome.observed_before_sha256 = ""
				outcome.observed_after_sha256 = ""
			end
			for remaining = index + 1, #plan.targets do
				local remainingOutcome = newMultiEditTargetOutcome(
					plan.targets[remaining]
				)
				if plan.targets[remaining].kind == "create" then
					remainingOutcome.observed_before_state = "unavailable"
					remainingOutcome.observed_after_state = "unavailable"
				else
					remainingOutcome.observed_before_sha256 = ""
					remainingOutcome.observed_after_sha256 = ""
				end
				outcomes[remaining] = remainingOutcome
			end
			break
		end
		if planTarget.kind == "create" then
			local state, className, digest =
				observeMultiEditCreate(planTarget)
			outcome.observed_before_state = state
			outcome.observed_after_state = state
			outcome.observed_after_class_name = className
			outcome.observed_after_sha256 = digest
			if state ~= "absent" then
				preflightSafe = false
			end
		else
			local resolvedOk, resolved = pcall(function()
				return requireLuaSourceContainer(planTarget.path)
			end)
				local sourceOk, source = pcall(function()
					if not resolvedOk or resolved ~= planTarget.target then
						return nil
					end
					local currentSource = readEditorSource(resolved)
					if requireLuaSourceContainer(planTarget.path) ~= resolved then
						return nil
					end
					return currentSource
				end)
			if not sourceOk
				or type(source) ~= "string"
				or not validUtf8(source)
				or #source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
			then
				preflightSafe = false
				outcome.observed_before_sha256 = ""
				outcome.observed_after_sha256 = ""
			else
				local observed = sourceSha256(source)
				outcome.observed_before_sha256 = observed
				outcome.observed_after_sha256 = observed
				if observed ~= planTarget.prepared_sha256 then
					preflightSafe = false
				end
			end
		end
	end
	if not preflightSafe then
		plan.status = "aborted_preflight"
		return newMultiEditMutationReceipt(
			plan,
			requestId,
			"apply",
			"aborted_preflight",
			true,
			outcomes
		)
	end
	if os.clock() > deadline then
		plan.status = "rolled_back"
		return newMultiEditMutationReceipt(
			plan,
			requestId,
			"apply",
			"rolled_back",
			true,
			outcomes
		)
	end

	local appliedIndexes = {}
	local failureIndex = nil
	for index, planTarget in ipairs(plan.targets) do
		if os.clock() > deadline then
			failureIndex = index
			break
		end
		assertExpectedDocument()
		if planTarget.kind == "create" then
			local created,
				mutated,
				state,
				className,
				digest = applyMultiEditCreate(planTarget)
			outcomes[index].observed_after_state = state
			outcomes[index].observed_after_class_name = className
			outcomes[index].observed_after_sha256 = digest
			if created then
				outcomes[index].status = "created"
				table.insert(appliedIndexes, index)
			else
				failureIndex = index
				if mutated or state ~= "absent" then
					outcomes[index].status = "recovery_required"
					if mutated then
						table.insert(appliedIndexes, index)
					end
				else
					outcomes[index].status = "not_created"
				end
				break
			end
		else
			local resolvedOk, resolved = pcall(function()
				return requireLuaSourceContainer(planTarget.path)
			end)
			if not resolvedOk or resolved ~= planTarget.target then
				failureIndex = index
				outcomes[index].observed_before_sha256 = ""
				outcomes[index].observed_after_sha256 = ""
				outcomes[index].status = "recovery_required"
				break
			end
			local applied, observedAfter, uncertain = multiEditCasReplace(
				resolved,
				planTarget.path,
				planTarget.prepared_sha256,
				planTarget.planned_source,
				false
			)
			outcomes[index].observed_after_sha256 = observedAfter
			if applied then
				outcomes[index].status = "applied"
				table.insert(appliedIndexes, index)
			else
				failureIndex = index
				if uncertain then
					outcomes[index].status = "recovery_required"
				end
				break
			end
		end
	end
	if failureIndex == nil then
		plan.status = "applied"
		return newMultiEditMutationReceipt(
			plan,
			requestId,
			"apply",
			"applied",
			true,
			outcomes
		)
	end

	local recoveryRequired =
		outcomes[failureIndex].status == "recovery_required"
	for appliedIndex = #appliedIndexes, 1, -1 do
		local index = appliedIndexes[appliedIndex]
		local planTarget = plan.targets[index]
		if planTarget.kind == "create" then
			local restored,
				beforeState,
				afterState,
				afterClass,
				afterDigest = compensateMultiEditCreate(planTarget)
			outcomes[index].observed_before_state = beforeState
			outcomes[index].observed_after_state = afterState
			outcomes[index].observed_after_class_name = afterClass
			outcomes[index].observed_after_sha256 = afterDigest
			if restored then
				outcomes[index].status = "rolled_back"
			else
				outcomes[index].status = "recovery_required"
				recoveryRequired = true
			end
		else
			local resolvedOk, resolved = pcall(function()
				return requireLuaSourceContainer(planTarget.path)
			end)
			if resolvedOk and resolved == planTarget.target then
				local restored, observedAfter = multiEditCasReplace(
					resolved,
					planTarget.path,
					planTarget.planned_sha256,
					planTarget.original_source,
					true
				)
				outcomes[index].observed_after_sha256 = observedAfter
				if restored then
					outcomes[index].status = "rolled_back"
				else
					outcomes[index].status = "recovery_required"
					recoveryRequired = true
				end
			else
				outcomes[index].observed_after_sha256 = ""
				outcomes[index].status = "recovery_required"
				recoveryRequired = true
			end
		end
	end
	for index, planTarget in ipairs(plan.targets) do
		if planTarget.kind == "create" then
			if planTarget.created_instance ~= nil then
				local state, className, digest =
					observeMultiEditCreate(planTarget)
				outcomes[index].observed_after_state = state
				outcomes[index].observed_after_class_name = className
				outcomes[index].observed_after_sha256 = digest
				if state ~= "absent"
					or planTarget.created_instance.Parent ~= nil
				then
					outcomes[index].status = "recovery_required"
					recoveryRequired = true
				end
			end
		else
			local checkOk, source = pcall(function()
				local resolved = requireLuaSourceContainer(planTarget.path)
				if resolved ~= planTarget.target then
					return nil
				end
				local currentSource = readEditorSource(resolved)
				if requireLuaSourceContainer(planTarget.path) ~= resolved then
					return nil
				end
				return currentSource
			end)
			if not checkOk
				or type(source) ~= "string"
				or not validUtf8(source)
				or #source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
				or sourceSha256(source) ~= planTarget.prepared_sha256
			then
				outcomes[index].status = "recovery_required"
				if checkOk and type(source) == "string" then
					outcomes[index].observed_after_sha256 =
						sourceSha256(source)
				else
					outcomes[index].observed_after_sha256 = ""
				end
				recoveryRequired = true
			end
		end
	end
	recoveryRequired = false
	for _, targetOutcome in ipairs(outcomes) do
		if targetOutcome.status == "recovery_required" then
			recoveryRequired = true
			break
		end
	end
	plan.status = recoveryRequired
		and "recovery_required"
		or "rolled_back"
	return newMultiEditMutationReceipt(
		plan,
		requestId,
		"apply",
		recoveryRequired and "recovery_required" or "rolled_back",
		not recoveryRequired,
		outcomes
	)
end

local function recoverMultiEdit(args, requestId, deadlineMs)
	requireEditMode()
	local deadline = os.clock() + (deadlineMs / 1_000)
	local plan = peer.multi_edit_plan
	local planStatus = plan ~= nil and plan.status or ""
	local recoverableStatus = planStatus == "prepared"
		or planStatus == "applying"
		or planStatus == "applied"
		or planStatus == "rolled_back"
		or planStatus == "aborted_preflight"
		or planStatus == "recovery_required"
		or planStatus == "recovered"
	if plan == nil
		or plan.transaction_id ~= args.transaction_id
		or plan.document_epoch ~= DOCUMENT_EPOCH
		or plan.generation ~= peer.generation
		or not recoverableStatus
	then
		adapterError(
			"multi_edit_recovery_unavailable",
			"No exact same-generation multi-edit plan is recoverable",
			false
		)
	end
	assertMultiEditMutationReceiptBound(plan, requestId, "recover")
	local evidence = plan.safe_terminal_evidence
	if evidence ~= nil
		and evidence.status == planStatus
		and (
			planStatus == "aborted_preflight"
			or planStatus == "rolled_back"
			or planStatus == "recovered"
		)
		and type(evidence.targets) == "table"
		and #evidence.targets == #plan.targets
		and (
			evidence.outcome == "aborted_preflight"
			or evidence.outcome == "rolled_back"
			or evidence.outcome == "recovered"
		)
		and type(evidence.receipt_sha256) == "string"
		and #evidence.receipt_sha256 == 64
		and string.find(evidence.receipt_sha256, "[^0-9a-f]") == nil
	then
		local evidenceTargets =
			copyMultiEditTargetOutcomes(evidence.targets)
		plan.status = "recovered"
		return newMultiEditMutationReceipt(
			plan,
			requestId,
			"recover",
			"recovered",
			true,
			evidenceTargets,
			"cached_safe_terminal",
			evidence.outcome,
			evidence.receipt_sha256
		)
	end
	local outcomes = {}
	local recoveryRequired = false
	for index = #plan.targets, 1, -1 do
		assertExpectedDocument()
		local planTarget = plan.targets[index]
		local outcome = newMultiEditTargetOutcome(planTarget)
		outcomes[index] = outcome
		if os.clock() > deadline then
			if planTarget.kind == "create" then
				outcome.observed_before_state = "unavailable"
				outcome.observed_after_state = "unavailable"
			else
				outcome.observed_before_sha256 = ""
				outcome.observed_after_sha256 = ""
			end
			outcome.status = "recovery_required"
			recoveryRequired = true
			continue
		end
		if planTarget.kind == "create" then
			local state, className, digest =
				observeMultiEditCreate(planTarget)
			outcome.observed_before_state = state
			outcome.observed_after_state = state
			outcome.observed_after_class_name = className
			outcome.observed_after_sha256 = digest
			if planTarget.created_instance == nil then
				if state == "absent" then
					outcome.status = "rolled_back"
				else
					outcome.status = "recovery_required"
					recoveryRequired = true
				end
			elseif state == "absent"
				and planTarget.created_instance.Parent == nil
			then
				outcome.status = "rolled_back"
			elseif state == "created_exact" then
				local restored,
					beforeState,
					afterState,
					afterClass,
					afterDigest = compensateMultiEditCreate(planTarget)
				outcome.observed_before_state = beforeState
				outcome.observed_after_state = afterState
				outcome.observed_after_class_name = afterClass
				outcome.observed_after_sha256 = afterDigest
				if restored then
					outcome.status = "rolled_back"
				else
					outcome.status = "recovery_required"
					recoveryRequired = true
				end
			else
				outcome.status = "recovery_required"
				recoveryRequired = true
			end
		else
			local readOk, source, resolved = pcall(function()
				local exact = requireLuaSourceContainer(planTarget.path)
				if exact ~= planTarget.target then
					return nil, nil
				end
				local currentSource = readEditorSource(exact)
				if requireLuaSourceContainer(planTarget.path) ~= exact then
					return nil, nil
				end
				return currentSource, exact
			end)
			if not readOk
				or type(source) ~= "string"
				or resolved ~= planTarget.target
				or not validUtf8(source)
				or #source > DURABLE_BOUNDS.MAX_MULTI_EDIT_SOURCE_BYTES
			then
				outcome.observed_before_sha256 = ""
				outcome.observed_after_sha256 = ""
				outcome.status = "recovery_required"
				recoveryRequired = true
			else
				local currentSha256 = sourceSha256(source)
				outcome.observed_before_sha256 = currentSha256
				outcome.observed_after_sha256 = currentSha256
				if currentSha256 == planTarget.prepared_sha256 then
					outcome.status = "rolled_back"
				elseif currentSha256 == planTarget.planned_sha256 then
					local restored, observedAfter = multiEditCasReplace(
						resolved,
						planTarget.path,
						planTarget.planned_sha256,
						planTarget.original_source,
						true
					)
					outcome.observed_after_sha256 = observedAfter
					if restored then
						outcome.status = "rolled_back"
					else
						outcome.status = "recovery_required"
						recoveryRequired = true
					end
				else
					outcome.status = "recovery_required"
					recoveryRequired = true
				end
			end
		end
	end
	plan.status = recoveryRequired
		and "recovery_required"
		or "recovered"
	return newMultiEditMutationReceipt(
		plan,
		requestId,
		"recover",
		recoveryRequired and "recovery_required" or "recovered",
		not recoveryRequired,
		outcomes
	)
end

local function readScript(args)
	requireEditMode()
	local target = requireLuaSourceContainer(args.path)
	local source = readEditorSource(target)
	local maxChars = args.max_chars or DURABLE_BOUNDS.MAX_SOURCE_BYTES
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
		OPTIONAL_SERVICES.ScriptEditorService:UpdateSourceAsync(target, function(currentSource)
			if callbackInvoked then
				return currentSource
			end
			callbackInvoked = true
			if type(currentSource) ~= "string"
				or #currentSource > DURABLE_BOUNDS.MAX_SOURCE_BYTES
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
		return OPTIONAL_SERVICES.LogService:GetLogHistory()
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
				message = string.sub(message, 1, DURABLE_BOUNDS.MAX_CONSOLE_MESSAGE_BYTES),
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
	local deadline = os.clock() + DURABLE_BOUNDS.SCREENSHOT_READY_SECONDS
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
		return OPTIONAL_SERVICES.StudioCaptureService:CanCaptureScreenshot()
	end)
	if not canOk or canCapture ~= true then
		adapterError(
			"screenshot_permission_required",
			"Studio screenshot permission or context is unavailable",
			false
		)
	end
	local captureOk, capture = pcall(function()
		return OPTIONAL_SERVICES.StudioCaptureService:CaptureScreenshot({})
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
		or buffer.len(pngBuffer) > DURABLE_BOUNDS.MAX_SCREENSHOT_BYTES
	then
		adapterError(
			"screenshot_size_unsupported",
			"PNG screenshot buffer is empty or exceeds the response bound",
			false
		)
	end
	local encodeOk, encodedBuffer = pcall(function()
		return OPTIONAL_SERVICES.EncodingService:Base64Encode(pngBuffer)
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
		return listTree(request.args, request.request_id)
	elseif request.operation == "studio_search_scripts" then
		return searchScripts(request.args, request.request_id)
	elseif request.operation == "studio_grep_scripts" then
		return grepScripts(request.args, request.request_id)
	elseif request.operation == "studio_inspect_instance" then
		return inspectInstance(request.args, request.request_id)
	elseif request.operation == "studio_read_script" then
		return readScript(request.args)
	elseif request.operation == "studio_update_script" then
		return updateScript(request.args)
	elseif request.operation == "studio_multi_edit" then
		if request.args._phase == "prepare" then
			return prepareMultiEdit(
				request.args,
				request.request_id,
				request.deadline_ms
			)
		end
		return applyMultiEdit(
			request.args,
			request.request_id,
			request.deadline_ms
		)
	elseif request.operation == "studio_recover_multi_edit" then
		return recoverMultiEdit(
			request.args,
			request.request_id,
			request.deadline_ms
		)
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
