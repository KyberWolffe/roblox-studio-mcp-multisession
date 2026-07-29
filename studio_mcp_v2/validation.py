from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Tuple

from .errors import ValidationError


_DOCUMENT_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_ARGUMENT_BYTES = 1_000_000
MAX_SCRIPT_BYTES = 256_000
MAX_TIMEOUT_MS = 120_000


def validate_studio_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("studio_id must be a canonical UUID string")
    if value != value.strip() or not value:
        raise ValidationError("studio_id must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("studio_id must be a canonical UUID string")
    canonical = str(parsed)
    if value != canonical:
        raise ValidationError("studio_id must use canonical lowercase UUID form")
    return canonical


def validate_client_instance_id(value: Any) -> str:
    try:
        return validate_studio_id(value)
    except ValidationError:
        raise ValidationError(
            "client_instance_id must be a canonical lowercase UUID"
        )


def validate_reconnect_id(value: Any) -> str:
    try:
        return validate_studio_id(value)
    except ValidationError:
        raise ValidationError(
            "reconnect_id must be a canonical lowercase UUID"
        )


def validate_transaction_id(value: Any) -> str:
    try:
        return validate_studio_id(value)
    except ValidationError:
        raise ValidationError(
            "transaction_id must be a canonical lowercase UUID"
        )


def validate_registration_secret(value: Any) -> str:
    if not isinstance(value, str) or not 32 <= len(value) <= 256:
        raise ValidationError(
            "registration_secret must be a 32-256 character secret"
        )
    return value


def validate_document_epoch(value: Any) -> str:
    if not isinstance(value, str) or not _DOCUMENT_EPOCH_RE.fullmatch(value):
        raise ValidationError(
            "document_epoch must be 1-128 ASCII routing-safe characters"
        )
    return value


def validate_generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError("generation must be a positive integer")
    return value


def validate_request_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValidationError("request_id must be a non-empty string up to 128 chars")
    return value


def validate_request_id_list(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 1000:
        raise ValidationError(
            "settled_request_ids must be an array of at most 1000 IDs"
        )
    normalized = tuple(validate_request_id(item) for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValidationError("settled_request_ids may not contain duplicates")
    return normalized


def validate_arguments(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("tool arguments must be an object")
    try:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ValidationError("tool arguments must be JSON-serializable")
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise ValidationError("tool arguments exceed the 1 MB limit")
    return value


def validate_timeout_ms(value: Any) -> int:
    if value is None:
        return 30_000
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_TIMEOUT_MS
    ):
        raise ValidationError(
            "timeout_ms must be an integer between 1 and 120000"
        )
    return value
