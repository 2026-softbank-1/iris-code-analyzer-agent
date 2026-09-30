"""Explicit model settings; credentials never enter repr or run reports."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from iris_analyzer.contracts import AnalyzerError

OPENCODE_VERSION = "1.18.33"
HIVE_BASE_URL = "https://api-cdn.thehive.ai/api/v3"
HIVE_MODEL = "zai-org/glm-5.3-flash"


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "hive-ai"
    model: str = HIVE_MODEL
    server_url: str | None = None
    hive_base_url: str = HIVE_BASE_URL
    api_key: str | None = field(default=None, repr=False)
    server_password: str | None = field(default=None, repr=False)
    server_username: str = "opencode"
    timeout_seconds: float = 180.0
    request_timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_delay_seconds: float = 0.25
    poll_interval_seconds: float = 0.2
    max_output_tokens: int = 8192
    max_remote_retries: int = 0
    max_model_calls: int = 8
    max_total_tokens: int = 500_000
    # A ceiling of 1 injects OpenCode's MAX_STEPS summary into the first prompt.
    # Two avoids that injection; JSON text has no tools and finishes in one step.
    max_inference_steps: int = 2
    reasoning_effort: str | None = None
    native_json_mode: bool | None = None
    # The verified Hive GLM endpoint rejects the tool_choice=required request
    # used by StructuredOutput. JSON text still passes strict app-side schema.
    output_mode: str = "json_text"
    expected_version: str = OPENCODE_VERSION

    def __post_init__(self) -> None:
        if self.reasoning_effort is None and self.provider == "hive-ai" and self.model == HIVE_MODEL:
            object.__setattr__(self, "reasoning_effort", "low")
        if self.reasoning_effort not in {None, "low", "high", "max"}:
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Reasoning effort must be low, high or max")
        if self.native_json_mode is None:
            object.__setattr__(
                self,
                "native_json_mode",
                (self.provider == "hive-ai" and self.model == HIVE_MODEL and self.output_mode == "json_text"),
            )
        if type(self.native_json_mode) is not bool or (
            self.native_json_mode and self.output_mode != "json_text"
        ):
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Native JSON mode requires JSON text output")
        if not self.provider or not self.model or "/" in self.provider:
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Explicit provider and model IDs are required")
        if self.output_mode not in {"structured", "json_text"}:
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Unknown output mode")
        for value in (self.timeout_seconds, self.request_timeout_seconds, self.poll_interval_seconds):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise AnalyzerError("MODEL_CONFIG_INVALID", "Timeouts and polling interval must be positive")
        if (
            type(self.max_retries) is not int
            or self.max_retries < 0
            or self.max_retries > 5
            or not isinstance(self.retry_delay_seconds, (int, float))
            or not math.isfinite(self.retry_delay_seconds)
            or self.retry_delay_seconds < 0
        ):
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Retry configuration is out of bounds")
        if type(self.max_remote_retries) is not int or not 0 <= self.max_remote_retries <= 5:
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Remote retry limit must be between zero and five")
        if type(self.max_output_tokens) is not int or not 256 <= self.max_output_tokens <= 32_768:
            raise AnalyzerError("MODEL_CONFIG_INVALID", "Output token limit must be between 256 and 32768")
        for value in (self.max_model_calls, self.max_total_tokens):
            if type(value) is not int or value <= 0:
                raise AnalyzerError(
                    "MODEL_CONFIG_INVALID", "Model call and total token budgets must be positive integers"
                )
        if self.max_inference_steps != 2:
            raise AnalyzerError(
                "MODEL_CONFIG_INVALID", "The pinned analyzer requires a two-step inference ceiling"
            )
        for value in (self.server_url, self.hive_base_url):
            if value is not None:
                parsed = urlparse(value)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise AnalyzerError(
                        "MODEL_CONFIG_INVALID", "A valid HTTP URL without credentials is required"
                    )
        if self.api_key in {"", "xxx", "<YOUR_SECRET_KEY>"}:
            raise AnalyzerError(
                "MODEL_AUTH_MISSING", "HIVE_AI contains a placeholder instead of a credential"
            )

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None, **overrides: object) -> ModelConfig:
        """Read one explicit dotenv file plus environment without modifying os.environ."""
        values = dict(dotenv_values(dotenv_path)) if dotenv_path is not None else {}
        values.update(os.environ)
        fields: dict[str, object] = {
            "provider": values.get("OPENCODE_PROVIDER") or "hive-ai",
            "model": values.get("OPENCODE_MODEL") or values.get("HIVE_MODEL") or HIVE_MODEL,
            "server_url": values.get("OPENCODE_URL") or None,
            "hive_base_url": values.get("HIVE_BASE_URL") or HIVE_BASE_URL,
            "api_key": values.get("HIVE_AI") or None,
            "server_password": values.get("OPENCODE_SERVER_PASSWORD") or None,
            "server_username": values.get("OPENCODE_SERVER_USERNAME") or "opencode",
            "output_mode": values.get("OPENCODE_OUTPUT_MODE") or "json_text",
            "reasoning_effort": values.get("OPENCODE_REASONING_EFFORT") or None,
        }
        for variable, field_name in (
            ("OPENCODE_MAX_OUTPUT_TOKENS", "max_output_tokens"),
            ("OPENCODE_MAX_REMOTE_RETRIES", "max_remote_retries"),
            ("OPENCODE_MAX_MODEL_CALLS", "max_model_calls"),
            ("OPENCODE_MAX_TOTAL_TOKENS", "max_total_tokens"),
        ):
            if values.get(variable):
                try:
                    fields[field_name] = int(values[variable])
                except (ValueError, TypeError) as exc:
                    raise AnalyzerError("MODEL_CONFIG_INVALID", f"{variable} must be an integer") from exc
        fields.update({key: value for key, value in overrides.items() if value is not None})
        if values.get("OPENCODE_NATIVE_JSON_MODE") and overrides.get("native_json_mode") is None:
            native_mode = values["OPENCODE_NATIVE_JSON_MODE"].lower()
            if native_mode not in {"true", "false", "1", "0"}:
                raise AnalyzerError("MODEL_CONFIG_INVALID", "OPENCODE_NATIVE_JSON_MODE must be true or false")
            fields["native_json_mode"] = native_mode in {"true", "1"}
        return cls(**fields)
