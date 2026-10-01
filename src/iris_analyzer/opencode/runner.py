"""Platform prompt, fresh sessions and strict model reply extraction."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from importlib.resources import files
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from iris_analyzer.contracts import (
    MODEL_REPLY_SCHEMA,
    AnalyzerError,
    canonical_bytes,
    digest,
    validate_bundle,
    validate_reply,
)
from iris_analyzer.preprocess import compact_model_input

from .client import OpenCodeClient, new_message_id
from .config import ModelConfig
from .pricing import pricing_for, usage_cost

PROMPT_VERSION = "deployment_v1.3"
MODEL_PROPOSAL_SCHEMA = copy.deepcopy(MODEL_REPLY_SCHEMA)
MODEL_PROPOSAL_SCHEMA["$defs"]["field"]["properties"]["status"]["enum"] = ["suggested", "unknown"]
MODEL_PROPOSAL_SCHEMA["$comment"] = (
    "Supplemental proposals only. Detected observations are produced and merged by static code."
)
_PROPOSAL_VALIDATOR = Draft202012Validator(MODEL_PROPOSAL_SCHEMA)
HIVE_GLM_PRICING = {
    "inputPerMillionUsd": 0.05,
    "outputPerMillionUsd": 0.17,
    "cachedInputPerMillionUsd": 0.01,
    "source": "https://thehive.ai/models/zai-org/glm-5.3-flash",
    "observedDate": "2026-10-01",
    "usageConventionSource": "https://github.com/anomalyco/opencode/blob/v1.18.33/packages/opencode/src/session/session.ts#L338",
}


def _usage_count(usage: dict | None) -> int | None:
    # Pinned Session.getUsage separates uncached input, cache read/write,
    # non-reasoning output and reasoning. Sum them only if total is unavailable.
    if not isinstance(usage, dict):
        return None
    if isinstance(usage.get("total"), (int, float)):
        return int(usage["total"])
    cache = usage.get("cache", {})
    return int(
        sum(usage.get(key, 0) for key in ("input", "output", "reasoning"))
        + cache.get("read", 0)
        + cache.get("write", 0)
    )


def _estimate_cost(usage: dict | None, config: ModelConfig) -> float | None:
    # Pinned OpenCode subtracts reasoning from tokens.output, so add it back
    # before applying the provider's billed output price. This is an estimate.
    prices = pricing_for(config.provider, config.model)
    if not isinstance(usage, dict) or prices is None:
        return None
    amount = usage_cost(usage, prices)
    return round(amount, 9) if amount is not None else None


def model_input(bundle: dict) -> dict:
    """Keep full inventory local; expose selected metadata and eligible path hints."""
    return compact_model_input(bundle)


def response_template(bundle: dict) -> dict:
    """A compact supplemental reply; the result merger computes final coverage."""
    return {
        "kind": "analysis",
        "result": {
            "schemaVersion": "1",
            "status": "needs_input",
            "sourceSnapshotId": bundle["source"]["snapshotId"],
            "contextHash": bundle["contextHash"],
            "services": [],
            "dependencies": [],
            "apiRoutes": [],
            "environmentKeys": [],
            "connections": [],
            "questions": [],
            "coverage": {"completeForProfile": False, "limitations": []},
        },
    }


def _model_error(error: dict) -> AnalyzerError:
    name = error.get("name", "")
    data = error.get("data", {})
    status = data.get("statusCode") if isinstance(data, dict) else None
    if name in {"ProviderAuthError", "AuthenticationError"} or status in {401, 403}:
        return AnalyzerError("MODEL_AUTH_FAILED", "Model provider rejected authentication")
    if name in {"ModelNotFoundError", "ProviderModelNotFoundError"} or status == 404:
        return AnalyzerError("MODEL_NOT_FOUND", "Model provider rejected the selected model")
    if name == "StructuredOutputError":
        return AnalyzerError(
            "MODEL_STRUCTURED_OUTPUT_FAILED", "Model did not return the required structured output"
        )
    if name in {"MessageAbortedError", "AbortError"}:
        return AnalyzerError("MODEL_CANCELLED", "Remote model execution was aborted")
    # Do not propagate provider error text: some providers echo credentials/request data.
    return AnalyzerError(
        "MODEL_EXECUTION_FAILED", "Model execution failed", {"errorType": name, "httpStatus": status}
    )


def parse_json_reply(text: str, *, finish_reason: str | None = None) -> tuple[dict, dict | None]:
    """Recover one unambiguous object while rejecting multiple or partial JSON."""
    if finish_reason in {"length", "max_tokens"}:
        raise AnalyzerError("RESULT_SCHEMA_INVALID", "Model output was truncated at its output limit")
    try:
        return json.loads(text), None
    except (ValueError, TypeError):
        pass
    if not isinstance(text, str):
        raise AnalyzerError("RESULT_SCHEMA_INVALID", "Model text is not a JSON object")
    decoder = json.JSONDecoder()
    candidates = []
    position = 0
    while match := re.search(r"[\{\[]", text[position:]):
        start = position + match.start()
        try:
            candidate, end = decoder.raw_decode(text, start)
        except ValueError as exc:
            following = text[start + 1 :].lstrip()
            looks_json = following.startswith('"') or (
                text[start] == "[" and following[:1] in {"{", "[", "]", '"'}
            )
            if looks_json:
                raise AnalyzerError(
                    "RESULT_SCHEMA_INVALID", "Model response contains incomplete or malformed JSON"
                ) from exc
            position = start + 1
            continue
        if not isinstance(candidate, dict):
            raise AnalyzerError(
                "RESULT_SCHEMA_INVALID", "Model response contains an unexpected JSON collection"
            )
        candidates.append((candidate, start, end))
        position = end
    if len(candidates) != 1:
        raise AnalyzerError(
            "RESULT_SCHEMA_INVALID", "Model response does not contain one unambiguous JSON object"
        )
    candidate, start, end = candidates[0]
    return candidate, {
        "strategy": "single_complete_json_object",
        "prefixCharacters": start,
        "suffixCharacters": len(text) - end,
    }


def validate_model_proposal(reply: dict) -> dict:
    validate_reply(reply)
    error = next(iter(_PROPOSAL_VALIDATOR.iter_errors(reply)), None)
    if error is not None:
        raise AnalyzerError(
            "RESULT_SCHEMA_INVALID",
            "Model output violates the supplemental proposal policy",
            {
                "path": list(error.absolute_path),
                "policy": "suggested_or_unknown_fields_only",
            },
        )
    return reply


class OpenCodeRunner:
    response_schema = MODEL_PROPOSAL_SCHEMA
    prompt_version = PROMPT_VERSION

    def _validate_input(self, bundle: dict) -> None:
        validate_bundle(bundle)

    def _model_context(self, bundle: dict) -> dict:
        return model_input(bundle)

    def _response_schema(self, bundle: dict) -> dict:
        schema = copy.deepcopy(self.response_schema)
        if self.response_schema is MODEL_PROPOSAL_SCHEMA:
            schema["$defs"]["analysis"]["properties"]["sourceSnapshotId"]["const"] = bundle["source"][
                "snapshotId"
            ]
            schema["$defs"]["analysis"]["properties"]["contextHash"]["const"] = bundle["contextHash"]
        return schema

    def _request_document(self, bundle: dict) -> dict:
        return {
            "responseSchema": self._response_schema(bundle),
            "contextBundle": self._model_context(bundle),
            "responseTemplate": response_template(bundle),
        }

    def _system_prompt(self) -> str:
        return files("iris_analyzer.opencode").joinpath("prompts/deployment.txt").read_text(encoding="utf-8")

    def _validate_output(self, reply: dict) -> dict:
        return validate_model_proposal(reply)

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: OpenCodeClient | None = None,
        transport: httpx.BaseTransport | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenCodeClient(config, transport=transport)
        self._owns_client = client is None
        self.cancel_event = cancel_event or threading.Event()
        self.calls: list[dict] = []
        self.last_request: dict | None = None
        self.last_response: dict | None = None
        self.model_calls = 0
        self.total_tokens = 0
        self.estimated_cost_usd = 0.0
        self._reserved_or_used_tokens = 0
        self._session_id: str | None = None

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._session_id:
            self.client.abort(self._session_id)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> OpenCodeRunner:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def invoke_model(self, bundle: dict) -> dict:
        self.last_request = None
        self.last_response = None
        self._validate_input(bundle)
        started = time.monotonic()
        deadline = started + self.config.timeout_seconds
        record: dict = {
            "snapshotId": bundle["source"]["snapshotId"],
            "contextHash": bundle["contextHash"],
            "revision": bundle["revision"],
            "sessionId": None,
            "messageId": None,
            "responseMessageId": None,
            "provider": self.config.provider,
            "model": self.config.model,
            "promptVersion": self.prompt_version,
            "outputMode": self.config.output_mode,
            "serverVersion": None,
            "apiSchemaHash": None,
            "latencySeconds": None,
            "usage": None,
            "cost": None,
            "error": None,
            "remoteAbortRequested": False,
            "remoteAbortConfirmed": None,
            "httpAttempts": 0,
            "remoteRetryAttempts": 0,
            "maxOutputTokens": self.config.max_output_tokens,
            "maxRemoteRetries": self.config.max_remote_retries,
            "maxInferenceSteps": self.config.max_inference_steps,
            "inferenceOptions": {
                "temperature": self.config.inference_temperature,
                "reasoningEffort": self.config.reasoning_effort,
                "maxOutputTokens": self.config.max_output_tokens,
                "responseFormat": {"type": "json_object"} if self.config.native_json_mode else None,
            },
            "formatRecovery": False,
            "usageCompleteForCall": self.config.output_mode == "json_text",
            "estimatedCostUsd": None,
            "pricing": HIVE_GLM_PRICING
            if self.config.provider == "hive-ai" and self.config.model == "zai-org/glm-5.3-flash"
            else None,
            "reservedTokensUpperBound": None,
        }
        self.calls.append(record)
        if self.config.provider == "openai":
            record["pricing"] = pricing_for(self.config.provider, self.config.model)
        initial_attempts = self.client.attempts
        reservation = 0
        execution_reservation = 0
        step_reservation = 0
        try:
            if self.model_calls >= self.config.max_model_calls:
                raise AnalyzerError("MODEL_BUDGET_EXCEEDED", "The configured model call budget is exhausted")
            if self.cancel_event.is_set():
                raise AnalyzerError("MODEL_CANCELLED", "Model execution was cancelled before starting")
            self.client.verify(deadline)
            record["serverVersion"] = self.client.server_version
            record["apiSchemaHash"] = self.client.schema_hash
            record["effectiveInferenceOptions"] = self.client.effective_model_policy
            self._session_id = self.client.create_session(deadline)
            record["sessionId"] = self._session_id
            message_id = new_message_id()
            record["messageId"] = message_id
            prompt = self._system_prompt()
            payload = {
                "messageID": message_id,
                "model": {"providerID": self.config.provider, "modelID": self.config.model},
                "agent": "iris-analyzer",
                "system": prompt,
                "parts": [
                    {
                        "type": "text",
                        "text": canonical_bytes(self._request_document(bundle)).decode("utf-8"),
                    }
                ],
            }
            if self.config.output_mode == "structured":
                payload["format"] = {
                    "type": "json_schema",
                    "schema": self._response_schema(bundle),
                    "retryCount": 0,
                }
            # UTF-8 bytes conservatively bound ordinary text tokens. Add a
            # platform framing allowance and reserve output for every permitted
            # remote retry. Unknown interrupted usage keeps its full reservation.
            step_reservation = len(canonical_bytes(payload)) + 4096 + self.config.max_output_tokens
            history_growth = (
                self.config.max_output_tokens
                * self.config.max_inference_steps
                * (self.config.max_inference_steps - 1)
                // 2
            )
            execution_reservation = step_reservation * self.config.max_inference_steps + history_growth
            reservation = execution_reservation * (1 + self.config.max_remote_retries)
            if self._reserved_or_used_tokens + reservation > self.config.max_total_tokens:
                reservation = 0
                raise AnalyzerError(
                    "MODEL_BUDGET_EXCEEDED",
                    "The conservative request/output token reservation exceeds the remaining budget",
                )
            self._reserved_or_used_tokens += reservation
            self.model_calls += 1
            record["reservedTokensUpperBound"] = reservation
            self.last_request = payload
            record["modelInputDigest"] = digest(self._model_context(bundle))
            record["modelPromptDigest"] = digest(json.loads(payload["parts"][0]["text"]))
            record["requestPayloadDigest"] = digest(payload)
            response = self.client.prompt(
                self._session_id, payload, deadline=deadline, cancel_event=self.cancel_event
            )
            info = response.get("info", {})
            safe_info = {
                key: info[key]
                for key in (
                    "id",
                    "parentID",
                    "role",
                    "providerID",
                    "modelID",
                    "time",
                    "tokens",
                    "finish",
                    "structured",
                )
                if key in info
            }
            if info.get("error"):
                safe_info["error"] = {"name": info["error"].get("name")}
            self.last_response = {
                "info": safe_info,
                "parts": [
                    {"type": "text", "text": part.get("text", "")}
                    for part in response.get("parts", [])
                    if part.get("type") == "text"
                ],
            }
            record["finishReason"] = info.get("finish")
            record["responseTextBytes"] = sum(
                len(part.get("text", "").encode("utf-8")) for part in self.last_response["parts"]
            )
            record["responseMessageId"] = info.get("id")
            record["usage"] = info.get("tokens")
            used = _usage_count(record["usage"])
            if used is not None:
                remote_retries = self.client.remote_retry_attempts.get(self._session_id, 0)
                unknown_steps = (
                    self.config.max_inference_steps - 1 if self.config.output_mode == "structured" else 0
                )
                unknown_retry_tokens = (
                    execution_reservation * remote_retries + step_reservation * unknown_steps
                )
                self._reserved_or_used_tokens += used + unknown_retry_tokens - reservation
                self.total_tokens += used
                record["estimatedCostUsd"] = _estimate_cost(record["usage"], self.config)
                if record["estimatedCostUsd"] is not None:
                    self.estimated_cost_usd += record["estimatedCostUsd"]
            # The custom Hive model has no registered pricing. OpenCode fills 0
            # for unknown prices, which must not be reported as a free request.
            record["cost"] = info.get("cost") if self.config.provider not in {"hive-ai", "openai"} else None
            record["serverEstimatedCostUsd"] = info.get("cost")
            actual_provider = info.get("providerID")
            actual_model = info.get("modelID")
            if actual_provider != self.config.provider or actual_model != self.config.model:
                raise AnalyzerError("MODEL_IDENTITY_MISMATCH", "OpenCode returned a different provider/model")
            if info.get("error"):
                raise _model_error(info["error"])
            if self.config.output_mode == "structured":
                reply = info.get("structured")
                if reply is None:
                    raise AnalyzerError(
                        "MODEL_STRUCTURED_OUTPUT_FAILED", "OpenCode response has no structured object"
                    )
            else:
                text = "".join(
                    p.get("text", "") for p in response.get("parts", []) if p.get("type") == "text"
                )
                reply, recovery = parse_json_reply(text, finish_reason=info.get("finish"))
                if recovery:
                    record["formatRecovery"] = True
                    record["formatRecoveryDetails"] = recovery
            return self._validate_output(reply)
        except (AnalyzerError, KeyboardInterrupt) as exc:
            record["error"] = exc.code if isinstance(exc, AnalyzerError) else "MODEL_CANCELLED"
            if self._session_id:
                record["remoteAbortRequested"] = True
                record["remoteAbortConfirmed"] = self.client.abort(self._session_id)
            if isinstance(exc, KeyboardInterrupt):
                raise AnalyzerError("MODEL_CANCELLED", "Model execution interrupted by the user") from exc
            raise
        finally:
            record["latencySeconds"] = round(time.monotonic() - started, 6)
            record["httpAttempts"] = self.client.attempts - initial_attempts
            record["cumulativeReportedTokens"] = self.total_tokens
            record["cumulativeTokensUpperBound"] = self._reserved_or_used_tokens
            record["cumulativeEstimatedCostUsd"] = round(self.estimated_cost_usd, 9)
            if self._session_id:
                record["remoteRetryAttempts"] = self.client.remote_retry_attempts.pop(self._session_id, 0)
                self.client._pending_messages.pop(self._session_id, None)
            self._session_id = None
