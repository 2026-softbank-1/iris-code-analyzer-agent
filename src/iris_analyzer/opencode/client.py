"""Small synchronous HTTP adapter with bounded, replay-aware transport handling."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable

import httpx
from jsonschema import Draft202012Validator

from iris_analyzer.contracts import AnalyzerError, digest

from .config import ModelConfig

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
REQUIRED_ENDPOINTS = {
    "/session": "post",
    "/session/status": "get",
    "/session/{sessionID}/prompt_async": "post",
    "/session/{sessionID}/message": "get",
    "/session/{sessionID}/abort": "post",
    "/provider": "get",
}

_ID_LOCK = threading.Lock()
_ID_TIMESTAMP = 0
_ID_COUNTER = 0


def new_message_id() -> str:
    """Use the pinned runtime's time-ordered identifier, preserving message order."""
    global _ID_TIMESTAMP, _ID_COUNTER
    with _ID_LOCK:
        stamp = int(time.time() * 1000)
        if stamp != _ID_TIMESTAMP:
            _ID_TIMESTAMP, _ID_COUNTER = stamp, 0
        _ID_COUNTER += 1
        prefix = ((stamp * 0x1000) + _ID_COUNTER) & ((1 << 48) - 1)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "msg_" + f"{prefix:012x}" + "".join(secrets.choice(alphabet) for _ in range(14))


class OpenCodeClient:
    def __init__(
        self,
        config: ModelConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not config.server_url:
            raise AnalyzerError(
                "MODEL_CONFIG_INVALID", "Start an isolated OpenCode server or set OPENCODE_URL"
            )
        self.config = config
        self._sleep = sleep
        self._owns_http = http_client is None
        self.http = http_client or httpx.Client(
            base_url=config.server_url.rstrip("/"),
            transport=transport,
            auth=(config.server_username, config.server_password) if config.server_password else None,
            timeout=config.request_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )
        self.doc: dict = {}
        self.server_version: str | None = None
        self.schema_hash: str | None = None
        self.attempts = 0
        self._pending_messages: dict[str, str] = {}
        self.remote_retry_attempts: dict[str, int] = {}
        self.effective_model_policy: dict | None = None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> OpenCodeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        deadline: float | None = None,
        retry: bool = True,
    ) -> Any:
        for attempt in range(self.config.max_retries + 1):
            remaining = (
                (deadline - time.monotonic()) if deadline is not None else self.config.request_timeout_seconds
            )
            if remaining <= 0:
                raise AnalyzerError("MODEL_TIMEOUT", "OpenCode execution exceeded the time budget")
            self.attempts += 1
            try:
                response = self.http.request(
                    method,
                    path,
                    json=payload,
                    timeout=min(self.config.request_timeout_seconds, remaining),
                )
            except httpx.TimeoutException as exc:
                if not retry or attempt == self.config.max_retries:
                    raise AnalyzerError("MODEL_TIMEOUT", "OpenCode HTTP request timed out") from exc
            except httpx.TransportError as exc:
                if not retry or attempt == self.config.max_retries:
                    raise AnalyzerError(
                        "MODEL_CONNECTION_ERROR", "OpenCode connection was interrupted"
                    ) from exc
            else:
                if response.status_code in {401, 403}:
                    raise AnalyzerError("MODEL_AUTH_FAILED", "OpenCode or model authentication failed")
                if response.status_code in {400, 404, 422}:
                    if (
                        response.status_code == 400
                        and method == "GET"
                        and path.endswith("/message?limit=1")
                        and "Expected OutputFormatJsonSchema" in response.text
                    ):
                        # 1.18.33 encodes a stored user format as a Schema.Class;
                        # it is plain JSON after persistence. An assistant-only
                        # limit avoids it once inference starts. Do not replay.
                        raise AnalyzerError("OPENCODE_PENDING_FORMAT", "Structured user message is pending")
                    raise AnalyzerError(
                        "MODEL_REQUEST_INVALID",
                        "OpenCode rejected the request",
                        {"httpStatus": response.status_code},
                    )
                if response.status_code >= 400:
                    if (
                        response.status_code not in TRANSIENT_STATUS
                        or not retry
                        or attempt == self.config.max_retries
                    ):
                        raise AnalyzerError(
                            "MODEL_SERVER_ERROR",
                            "OpenCode request failed",
                            {"httpStatus": response.status_code},
                        )
                else:
                    if response.status_code == 204 or not response.content:
                        return None
                    try:
                        return response.json()
                    except (ValueError, UnicodeError) as exc:
                        raise AnalyzerError(
                            "OPENCODE_RESPONSE_INVALID", "OpenCode returned invalid JSON"
                        ) from exc
            delay = self.config.retry_delay_seconds * (2**attempt)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise AnalyzerError("MODEL_TIMEOUT", "OpenCode retry exceeded the time budget")
            self._sleep(delay)
        raise AssertionError("unreachable")

    def verify(self, deadline: float) -> None:
        """Validate version and the running server's /doc rather than documentation examples."""
        health = self.request("GET", "/global/health", deadline=deadline)
        if not isinstance(health, dict) or health.get("healthy") is not True:
            raise AnalyzerError("OPENCODE_RESPONSE_INVALID", "OpenCode health response is invalid")
        self.server_version = health.get("version")
        if self.server_version != self.config.expected_version:
            raise AnalyzerError(
                "OPENCODE_VERSION_MISMATCH",
                "OpenCode version differs from the tested pin",
                {
                    "expectedVersion": self.config.expected_version,
                    "actualVersion": self.server_version,
                },
            )
        doc = self.request("GET", "/doc", deadline=deadline)
        if not isinstance(doc, dict) or not str(doc.get("openapi", "")).startswith("3."):
            raise AnalyzerError("OPENCODE_SCHEMA_INVALID", "OpenCode /doc is not an OpenAPI specification")
        paths = doc.get("paths", {})
        for path, method in REQUIRED_ENDPOINTS.items():
            if method not in paths.get(path, {}):
                raise AnalyzerError(
                    "OPENCODE_SCHEMA_INVALID", "OpenCode /doc is missing a required operation", {"path": path}
                )
        self.doc = doc
        self.schema_hash = digest(doc)
        effective = self.request("GET", "/config", deadline=deadline)
        permissions = {"*": "deny", "StructuredOutput": "allow"}
        agent = effective.get("agent", {}).get("iris-analyzer", {}) if isinstance(effective, dict) else {}
        if (
            not isinstance(effective, dict)
            or effective.get("permission") != permissions
            or agent.get("permission") != permissions
            or agent.get("steps") != self.config.max_inference_steps
            or effective.get("plugin", [])
            or effective.get("mcp", {})
            or effective.get("instructions", [])
        ):
            raise AnalyzerError(
                "OPENCODE_ISOLATION_INVALID",
                "OpenCode configuration does not enforce the platform analysis policy",
            )
        providers = self.request("GET", "/provider", deadline=deadline)
        if not isinstance(providers, dict):
            raise AnalyzerError("OPENCODE_RESPONSE_INVALID", "OpenCode provider discovery is invalid")
        provider = next((p for p in providers.get("all", []) if p.get("id") == self.config.provider), None)
        if provider is None or self.config.model not in provider.get("models", {}):
            raise AnalyzerError(
                "MODEL_NOT_FOUND",
                "Configured provider/model was not discovered by OpenCode",
                {
                    "provider": self.config.provider,
                    "model": self.config.model,
                },
            )
        if self.config.provider not in providers.get("connected", []):
            raise AnalyzerError("MODEL_AUTH_MISSING", "Configured provider has no connected credentials")
        selected_model = provider["models"][self.config.model]
        if not isinstance(selected_model, dict):
            raise AnalyzerError("OPENCODE_POLICY_INVALID", "Effective model policy is missing")
        limits = selected_model.get("limit")
        capabilities = selected_model.get("capabilities")
        if not isinstance(limits, dict) or not isinstance(capabilities, dict):
            raise AnalyzerError(
                "OPENCODE_POLICY_INVALID", "Effective model limits or capabilities are missing"
            )
        limit = limits.get("output")
        options = selected_model.get("options")
        temperature = agent.get("temperature")
        expected_format = {"type": "json_object"} if self.config.native_json_mode else None
        if (
            type(limit) is not int
            or not 0 < limit <= self.config.max_output_tokens
            or not isinstance(options, dict)
            or set(options) - {"reasoningEffort", "response_format"}
            or options.get("reasoningEffort") != self.config.reasoning_effort
            or options.get("response_format") != expected_format
            or capabilities.get("temperature") is not True
            or type(temperature) not in {int, float}
            or temperature != 0
        ):
            raise AnalyzerError(
                "OPENCODE_POLICY_INVALID",
                "Effective model controls do not match the declared inference and budget policy",
            )
        self.effective_model_policy = {
            "maxOutputTokens": limit,
            "temperature": temperature,
            "reasoningEffort": options.get("reasoningEffort"),
            "responseFormat": options.get("response_format"),
        }

    def validate_request(self, path: str, payload: dict) -> None:
        pointer = path.replace("~", "~0").replace("/", "~1")
        reference = f"#/paths/{pointer}/post/requestBody/content/application~1json/schema"
        schema = {**self.doc, "$ref": reference}
        try:
            error = next(iter(Draft202012Validator(schema).iter_errors(payload)), None)
        except Exception as exc:
            raise AnalyzerError(
                "OPENCODE_SCHEMA_INVALID", "Cannot resolve the running OpenCode request schema"
            ) from exc
        if error is not None:
            raise AnalyzerError(
                "OPENCODE_SCHEMA_INVALID",
                "Payload does not match the running OpenCode schema",
                {"path": list(error.absolute_path)},
            )

    def create_session(self, deadline: float) -> str:
        payload = {
            "title": "Iris deployment analysis",
            "permission": [
                {"permission": "*", "pattern": "*", "action": "deny"},
                {"permission": "StructuredOutput", "pattern": "*", "action": "allow"},
            ],
        }
        self.validate_request("/session", payload)
        # Creating a session is not idempotent. Do not replay if its response is lost.
        response = self.request("POST", "/session", payload=payload, deadline=deadline, retry=False)
        if not isinstance(response, dict) or not isinstance(response.get("id"), str):
            raise AnalyzerError("OPENCODE_RESPONSE_INVALID", "OpenCode session response is invalid")
        return response["id"]

    def abort(self, session_id: str) -> bool:
        """Best effort remote cancellation with its own short bounded timeout."""
        try:
            return (
                self.request(
                    "POST", f"/session/{session_id}/abort", deadline=time.monotonic() + 3, retry=False
                )
                is True
            )
        except AnalyzerError:
            return False

    def status(self, session_id: str, deadline: float) -> str:
        statuses = self.request("GET", "/session/status", deadline=deadline)
        if not isinstance(statuses, dict):
            raise AnalyzerError("OPENCODE_RESPONSE_INVALID", "OpenCode session status is invalid")
        status = statuses.get(session_id, {})
        if status.get("type") == "retry":
            self.remote_retry_attempts[session_id] = max(
                self.remote_retry_attempts.get(session_id, 0), status.get("attempt", 0)
            )
            if status.get("attempt", 0) > self.config.max_remote_retries:
                raise AnalyzerError(
                    "MODEL_RETRY_LIMIT_EXCEEDED", "Remote model retry exceeded the configured limit"
                )
        return status.get("type", "idle")

    def messages(self, session_id: str, deadline: float) -> list[dict]:
        try:
            messages = self.request("GET", f"/session/{session_id}/message?limit=1", deadline=deadline)
        except AnalyzerError as exc:
            if exc.code != "OPENCODE_PENDING_FORMAT":
                raise
            return [{"info": {"id": self._pending_messages.get(session_id), "role": "user"}, "parts": []}]
        if not isinstance(messages, list):
            raise AnalyzerError("OPENCODE_RESPONSE_INVALID", "OpenCode messages response is invalid")
        return messages

    def prompt(
        self,
        session_id: str,
        payload: dict,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        payload = {**payload, "messageID": payload.get("messageID") or new_message_id()}
        self._pending_messages[session_id] = payload["messageID"]
        self.validate_request("/session/{sessionID}/prompt_async", payload)
        path = f"/session/{session_id}/prompt_async"
        # A failed POST may have started inference. Observe status and messages before any replay.
        for attempt in range(self.config.max_retries + 1):
            try:
                self.request("POST", path, payload=payload, deadline=deadline, retry=False)
                break
            except AnalyzerError as exc:
                if exc.code not in {"MODEL_CONNECTION_ERROR", "MODEL_SERVER_ERROR", "MODEL_TIMEOUT"}:
                    raise
                state = self.status(session_id, deadline)
                messages = self.messages(session_id, deadline)
                if state in {"busy", "retry"} or any(
                    m.get("info", {}).get("id") == payload["messageID"] for m in messages
                ):
                    break
                if attempt == self.config.max_retries:
                    raise
                self._sleep(self.config.retry_delay_seconds * (2**attempt))
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AnalyzerError("MODEL_CANCELLED", "OpenCode execution was cancelled")
            if time.monotonic() >= deadline:
                raise AnalyzerError("MODEL_TIMEOUT", "OpenCode execution exceeded the time budget")
            messages = self.messages(session_id, deadline)
            for message in reversed(messages):
                info = message.get("info", {})
                if info.get("role") == "assistant" and info.get("parentID") == payload["messageID"]:
                    if info.get("time", {}).get("completed") and (
                        self.config.output_mode == "json_text" or self.status(session_id, deadline) == "idle"
                    ):
                        return message
                    if info.get("error") and self.status(session_id, deadline) == "idle":
                        return message
            # A newly accepted async prompt can be idle briefly before its worker starts.
            self.status(session_id, deadline)
            self._sleep(min(self.config.poll_interval_seconds, max(0, deadline - time.monotonic())))
