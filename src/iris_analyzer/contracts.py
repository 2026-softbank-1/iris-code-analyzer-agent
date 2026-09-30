"""Versioned JSON contracts shared by preprocessing, transport and validation.

JSON objects are deliberately the public API: workers and CLI clients can save
them without a custom encoder. Schemas are also sent to the analysis model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator


class AnalyzerError(Exception):
    """An operational error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class Limits:
    max_bundle_bytes: int = 180_000
    max_input_tokens: int | None = None
    max_file_bytes: int = 1_000_000
    max_expansions: int = 1
    max_requested_files: int = 5

    def __post_init__(self) -> None:
        for name in ("max_bundle_bytes", "max_file_bytes", "max_requested_files"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_expansions) is not int or self.max_expansions < 0:
            raise ValueError("max_expansions must be a nonnegative integer")
        if self.max_input_tokens is not None and (
            type(self.max_input_tokens) is not int or self.max_input_tokens <= 0
        ):
            raise ValueError("max_input_tokens must be a positive integer or None")


def canonical_bytes(obj: Any) -> bytes:
    """Canonical v1 encoding; callers establish semantic list ordering."""
    try:
        return json.dumps(
            obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AnalyzerError("JSON_INVALID", "Payload is not valid finite UTF-8 JSON") from exc


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def _load_schema(name: str) -> dict:
    return json.loads(files("iris_analyzer.schemas").joinpath(name).read_text(encoding="utf-8"))


CONTEXT_BUNDLE_SCHEMA = _load_schema("context-bundle.schema.json")
ANALYSIS_RESULT_SCHEMA = _load_schema("analysis-result.schema.json")
MODEL_REPLY_SCHEMA = _load_schema("model-reply.schema.json")


def _validate(value: dict, schema: dict, code: str) -> dict:
    # jsonschema accepts some Python-only values; require serializable JSON first.
    canonical_bytes(value)
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    if error is not None:
        raise AnalyzerError(
            code,
            "JSON does not match the versioned contract",
            {"path": list(error.absolute_path), "reason": error.message},
        )
    return value


def validate_bundle(bundle: dict) -> dict:
    return _validate(bundle, CONTEXT_BUNDLE_SCHEMA, "CONTEXT_SCHEMA_INVALID")


def validate_reply(reply: dict) -> dict:
    return _validate(reply, MODEL_REPLY_SCHEMA, "RESULT_SCHEMA_INVALID")


def validate_result(result: dict) -> dict:
    return _validate(result, ANALYSIS_RESULT_SCHEMA, "RESULT_SCHEMA_INVALID")
