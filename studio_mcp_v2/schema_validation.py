from __future__ import annotations

import math
import re
import uuid
from typing import Any, Mapping, Sequence

from .errors import ValidationError


_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "pattern",
        "enum",
        "const",
        "format",
        "dependentRequired",
        "description",
        "title",
        "default",
        "examples",
    }
)
_SUPPORTED_TYPES = frozenset(
    {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
        "null",
    }
)
_MAX_SCHEMA_DEPTH = 64


def _schema_error(message: str) -> ValidationError:
    return ValidationError(
        "Catalog input schema is unsupported or invalid: " + message
    )


def validate_input_schema_definition(
    schema: Any,
    *,
    _path: str = "$",
    _depth: int = 0,
) -> None:
    """Validate the closed JSON-Schema subset used by tool inputs.

    Unknown schema shapes fail during catalog loading instead of silently
    weakening host enforcement. Output schemas intentionally use a broader
    review-only vocabulary and are not accepted by this runtime validator.
    """

    if _depth > _MAX_SCHEMA_DEPTH:
        raise _schema_error("schema nesting exceeds 64 at " + _path)
    if not isinstance(schema, dict):
        raise _schema_error(_path + " must be an object")
    unknown = set(schema) - _SUPPORTED_SCHEMA_KEYS
    if unknown:
        raise _schema_error(
            _path + " contains unsupported keywords: "
            + ",".join(sorted(unknown))
        )

    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str)
        or schema_type not in _SUPPORTED_TYPES
    ):
        raise _schema_error(_path + ".type is unsupported")

    properties = schema.get("properties")
    if properties is not None:
        if schema_type != "object" or not isinstance(
            properties, dict
        ):
            raise _schema_error(
                _path + ".properties requires object type"
            )
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str):
                raise _schema_error(
                    _path + ".properties keys must be strings"
                )
            validate_input_schema_definition(
                property_schema,
                _path=_path + ".properties." + property_name,
                _depth=_depth + 1,
            )

    required = schema.get("required")
    if required is not None:
        if (
            schema_type != "object"
            or not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
        ):
            raise _schema_error(_path + ".required is invalid")
        if properties is not None and any(
            item not in properties for item in required
        ):
            raise _schema_error(
                _path + ".required references an unknown property"
            )

    additional = schema.get("additionalProperties")
    if additional is not None and (
        schema_type != "object" or type(additional) is not bool
    ):
        raise _schema_error(
            _path + ".additionalProperties must be boolean"
        )

    items = schema.get("items")
    if items is not None:
        if schema_type != "array":
            raise _schema_error(_path + ".items requires array type")
        validate_input_schema_definition(
            items,
            _path=_path + ".items",
            _depth=_depth + 1,
        )

    for keyword in ("minItems", "maxItems"):
        value = schema.get(keyword)
        if value is not None and (
            schema_type != "array"
            or type(value) is not int
            or value < 0
        ):
            raise _schema_error(
                _path + "." + keyword + " is invalid"
            )
    if (
        schema.get("minItems") is not None
        and schema.get("maxItems") is not None
        and schema["minItems"] > schema["maxItems"]
    ):
        raise _schema_error(_path + " has inverted item bounds")

    for keyword in ("minLength", "maxLength"):
        value = schema.get(keyword)
        if value is not None and (
            schema_type != "string"
            or type(value) is not int
            or value < 0
        ):
            raise _schema_error(
                _path + "." + keyword + " is invalid"
            )
    if (
        schema.get("minLength") is not None
        and schema.get("maxLength") is not None
        and schema["minLength"] > schema["maxLength"]
    ):
        raise _schema_error(_path + " has inverted string bounds")

    for keyword in ("minimum", "maximum"):
        value = schema.get(keyword)
        if value is not None and (
            schema_type not in {"integer", "number"}
            or type(value) not in {int, float}
            or not math.isfinite(value)
        ):
            raise _schema_error(
                _path + "." + keyword + " is invalid"
            )
    if (
        schema.get("minimum") is not None
        and schema.get("maximum") is not None
        and schema["minimum"] > schema["maximum"]
    ):
        raise _schema_error(_path + " has inverted numeric bounds")

    pattern = schema.get("pattern")
    if pattern is not None:
        if schema_type != "string" or not isinstance(pattern, str):
            raise _schema_error(_path + ".pattern is invalid")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _schema_error(
                _path + ".pattern does not compile"
            ) from exc

    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, list) or not enum
    ):
        raise _schema_error(_path + ".enum is invalid")

    value_format = schema.get("format")
    if value_format is not None and (
        schema_type != "string" or value_format != "uuid"
    ):
        raise _schema_error(_path + ".format is unsupported")

    dependent = schema.get("dependentRequired")
    if dependent is not None:
        if schema_type != "object" or not isinstance(
            dependent, dict
        ):
            raise _schema_error(
                _path + ".dependentRequired is invalid"
            )
        for property_name, dependencies in dependent.items():
            if (
                not isinstance(property_name, str)
                or not isinstance(dependencies, list)
                or any(
                    not isinstance(item, str)
                    for item in dependencies
                )
                or len(set(dependencies)) != len(dependencies)
            ):
                raise _schema_error(
                    _path + ".dependentRequired is invalid"
                )
            if properties is not None and (
                property_name not in properties
                or any(
                    dependency not in properties
                    for dependency in dependencies
                )
            ):
                raise _schema_error(
                    _path
                    + ".dependentRequired references an unknown property"
                )


def _finite_json_number(value: Any) -> bool:
    # Python integers are exact and cannot represent NaN or infinity. Passing
    # a very large int through math.isfinite first coerces it to float and can
    # raise OverflowError, turning malicious input into an internal failure.
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) in {int, float} and type(right) in {
        int,
        float,
    }:
        return (
            _finite_json_number(left)
            and _finite_json_number(right)
            and left == right
        )
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict):
        return (
            set(left) == set(right)
            and all(
                _json_equal(left[key], right[key])
                for key in left
            )
        )
    return left == right


def _instance_type_matches(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return type(value) is int
    if schema_type == "number":
        return (
            type(value) in {int, float}
            and _finite_json_number(value)
        )
    if schema_type == "boolean":
        return type(value) is bool
    return schema_type == "null" and value is None


def _instance_error(path: str, message: str) -> ValidationError:
    return ValidationError(
        "Tool arguments do not match the selected catalog schema at "
        + path
        + ": "
        + message
    )


def validate_schema_instance(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str = "$",
    _depth: int = 0,
) -> None:
    """Validate one JSON value against a prevalidated input schema."""

    if _depth > _MAX_SCHEMA_DEPTH:
        raise _instance_error(path, "nesting exceeds 64")
    schema_type = schema.get("type")
    if schema_type is not None and not _instance_type_matches(
        value, schema_type
    ):
        raise _instance_error(path, "expected " + schema_type)

    if "const" in schema and not _json_equal(
        value, schema["const"]
    ):
        raise _instance_error(path, "does not equal the required constant")
    if "enum" in schema and not any(
        _json_equal(value, candidate)
        for candidate in schema["enum"]
    ):
        raise _instance_error(path, "is outside the permitted enum")

    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for property_name in required:
            if property_name not in value:
                raise _instance_error(
                    path, "missing required property " + property_name
                )
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise _instance_error(
                    path,
                    "contains unknown properties "
                    + ",".join(sorted(str(item) for item in unknown)),
                )
        for property_name, property_value in value.items():
            property_schema = properties.get(property_name)
            if property_schema is not None:
                validate_schema_instance(
                    property_value,
                    property_schema,
                    path=path + "." + property_name,
                    _depth=_depth + 1,
                )
        for trigger, dependencies in schema.get(
            "dependentRequired", {}
        ).items():
            if trigger in value:
                for dependency in dependencies:
                    if dependency not in value:
                        raise _instance_error(
                            path,
                            dependency
                            + " is required when "
                            + trigger
                            + " is present",
                        )

    elif schema_type == "array":
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise _instance_error(path, "contains too few items")
        if maximum is not None and len(value) > maximum:
            raise _instance_error(path, "contains too many items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_schema_instance(
                    item,
                    item_schema,
                    path=path + "[" + str(index) + "]",
                    _depth=_depth + 1,
                )

    elif schema_type == "string":
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise _instance_error(path, "is shorter than minLength")
        if maximum is not None and len(value) > maximum:
            raise _instance_error(path, "is longer than maxLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise _instance_error(path, "does not match pattern")
        if schema.get("format") == "uuid":
            try:
                parsed = uuid.UUID(value)
            except (ValueError, AttributeError, TypeError) as exc:
                raise _instance_error(
                    path, "is not a canonical UUID"
                ) from exc
            if str(parsed) != value:
                raise _instance_error(
                    path, "is not a canonical UUID"
                )

    elif schema_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise _instance_error(path, "is below minimum")
        if maximum is not None and value > maximum:
            raise _instance_error(path, "is above maximum")


def validate_tool_arguments(
    arguments: Any,
    input_schema: Mapping[str, Any],
) -> None:
    if not isinstance(input_schema, dict):
        raise _schema_error("selected input schema is not an object")
    validate_schema_instance(arguments, input_schema)
