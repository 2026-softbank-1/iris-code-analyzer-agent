"""Transport scenarios use the request schemas captured from OpenCode 1.18.33 /doc."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from importlib.resources import files

import httpx
import pytest

from iris_analyzer.contracts import AnalyzerError, digest
from iris_analyzer.opencode import ModelConfig, OpenCodeRunner, model_input


def bundle(revision: int = 1) -> dict:
    value = {
        "schemaVersion": "1",
        "preprocessorVersion": "1",
        "policyVersion": "1",
        "profile": "deployment_v1",
        "revision": revision,
        "source": {"snapshotId": "a" * 64, "commit": None},
        "manifest": [
            {
                "fileId": "f-1",
                "path": "package.json",
                "size": 2,
                "digest": "b" * 64,
                "kind": "manifest",
                "eligible": True,
                "exclusionReason": None,
            },
            {
                "fileId": "f-2",
                "path": "src/server.ts",
                "size": 12,
                "digest": "c" * 64,
                "kind": "source",
                "eligible": True,
                "exclusionReason": None,
            },
            {
                "fileId": "f-3",
                "path": ".env",
                "size": 12,
                "digest": None,
                "kind": "secret",
                "eligible": False,
                "exclusionReason": "secret",
            },
        ],
        "componentRoots": [],
        "deploymentCandidates": [],
        "facts": [],
        "relations": [],
        "unresolved": [],
        "evidence": [],
        "selectedFiles": [
            {
                "fileId": "f-1",
                "path": "package.json",
                "role": "manifest",
                "selectionReason": "runtime",
                "providedRanges": [],
            }
        ],
        "coverage": {
            "providedEvidenceIds": [],
            "omittedRelevantFiles": [],
            "unresolvedReferences": [],
            "truncated": False,
        },
    }
    value["contextHash"] = digest(value)
    return value


REPLY = {"kind": "needs_files", "requestedPaths": ["src/server.ts"], "reason": "Verify entrypoint"}
POLICY = {
    "permission": {"*": "deny", "StructuredOutput": "allow"},
    "agent": {
        "iris-analyzer": {
            "permission": {"*": "deny", "StructuredOutput": "allow"},
            "steps": 2,
            "temperature": 0,
        },
    },
    "plugin": [],
    "mcp": {},
    "instructions": [],
}


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.sessions: dict[str, dict] = {}
        self.doc = json.loads(files("iris_analyzer.opencode").joinpath("api-contract.json").read_text())
        self.policy = POLICY
        self.version = "1.18.33"
        self.connected = ["hive-ai"]
        self.model = "zai-org/glm-5.3-flash"
        self.error: dict | None = None
        self.reply = REPLY
        self.output_text: str | None = None
        self.prompt_failures: list[str] = []
        self.accept_on_failure = False
        self.health_failures = 0
        self.hang = False
        self.cancel_event: threading.Event | None = None
        self.assistant_provider = "hive-ai"
        self.pending_format_reads = 0
        self.message_disconnect = False
        self.retry_attempt = 0
        self.usage = {"input": 12, "output": 7}
        self.model_settings = {
            "limit": {"output": 8192},
            "capabilities": {"temperature": True},
            "options": {"reasoningEffort": "low"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        data = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, data))
        if path == "/global/health":
            if self.health_failures:
                self.health_failures -= 1
                return httpx.Response(503, json={"error": "transient"})
            return httpx.Response(200, json={"healthy": True, "version": self.version})
        if path == "/doc":
            return httpx.Response(200, json=self.doc)
        if path == "/config":
            return httpx.Response(200, json=self.policy)
        if path == "/provider":
            return httpx.Response(
                200,
                json={
                    "all": [{"id": "hive-ai", "models": {self.model: self.model_settings}}],
                    "connected": self.connected,
                },
            )
        if path == "/session" and request.method == "POST":
            session_id = "ses_" + str(len(self.sessions) + 1)
            self.sessions[session_id] = {"accepted": False}
            return httpx.Response(200, json={"id": session_id})
        if path == "/session/status":
            if self.retry_attempt:
                return httpx.Response(
                    200, json={s: {"type": "retry", "attempt": self.retry_attempt} for s in self.sessions}
                )
            return httpx.Response(
                200,
                json={s: {"type": "busy"} for s, v in self.sessions.items() if v["accepted"] and self.hang},
            )
        session_id = path.split("/")[2]
        if path.endswith("/abort"):
            return httpx.Response(200, json=True)
        session = self.sessions[session_id]
        if path.endswith("/prompt_async"):
            session["payload"] = data
            if self.cancel_event:
                self.cancel_event.set()
            if self.prompt_failures:
                action = self.prompt_failures.pop(0)
                session["accepted"] = self.accept_on_failure
                if action == "disconnect":
                    raise httpx.ReadError("secret-key-must-not-be-reported", request=request)
                return httpx.Response(int(action), json={"message": "secret-key-must-not-be-reported"})
            session["accepted"] = True
            return httpx.Response(204)
        if path.endswith("/message"):
            if self.message_disconnect:
                raise httpx.ReadError("interrupted", request=request)
            if self.pending_format_reads:
                self.pending_format_reads -= 1
                return httpx.Response(
                    400,
                    json={
                        "name": "BadRequest",
                        "data": {
                            "message": "Expected OutputFormatJsonSchema, got a stored JSON object",
                        },
                    },
                )
            if not session["accepted"]:
                return httpx.Response(200, json=[])
            parent = session["payload"]["messageID"]
            messages = [{"info": {"id": parent, "role": "user"}, "parts": []}]
            if not self.hang:
                info = {
                    "id": "msg_reply",
                    "parentID": parent,
                    "role": "assistant",
                    "providerID": self.assistant_provider,
                    "modelID": self.model,
                    "time": {"completed": 123},
                    "tokens": self.usage,
                    "cost": 0,
                    "structured": self.reply,
                }
                if self.error:
                    info["error"] = self.error
                messages.append({"info": info, "parts": [{"type": "text", "text": self.output_text or ""}]})
            return httpx.Response(200, json=messages)
        raise AssertionError((request.method, path))

    def runner(self, **changes: object) -> OpenCodeRunner:
        changes.setdefault("output_mode", "structured")
        config = ModelConfig(
            server_url="http://opencode.test",
            api_key="secret-key-must-not-be-reported",
            retry_delay_seconds=0,
            poll_interval_seconds=0.005,
            **changes,
        )
        if config.native_json_mode:
            self.model_settings["options"]["response_format"] = {"type": "json_object"}
        else:
            self.model_settings["options"].pop("response_format", None)
        return OpenCodeRunner(
            config, transport=httpx.MockTransport(self.handler), cancel_event=self.cancel_event
        )


def test_structured_prompt_matches_real_doc_and_revision_gets_fresh_session() -> None:
    server = FakeServer()
    with server.runner() as runner:
        assert runner.invoke_model(bundle()) == REPLY
        assert runner.invoke_model(bundle(2)) == REPLY
        assert runner.calls[0]["sessionId"] != runner.calls[1]["sessionId"]
        assert runner.calls[0]["contextHash"] != runner.calls[1]["contextHash"]
        assert runner.calls[0]["usage"] == {"input": 12, "output": 7}
        assert runner.calls[0]["serverVersion"] == "1.18.33"
        assert "secret-key-must-not-be-reported" not in json.dumps(runner.calls)
        from iris_analyzer.opencode.runner import MODEL_REVIEW_SCHEMA

        expected = copy.deepcopy(MODEL_REVIEW_SCHEMA)
        assert runner.last_request["format"]["schema"] == expected
        assert runner.last_request["format"]["retryCount"] == 0
        assert runner.last_request["agent"] == "iris-analyzer"
        assert runner.calls[-1]["requestPayloadDigest"] == digest(runner.last_request)
        session_requests = [
            data for method, path, data in server.requests if method == "POST" and path == "/session"
        ]
        assert session_requests[0]["permission"] == [
            {"permission": "*", "pattern": "*", "action": "deny"},
            {"permission": "StructuredOutput", "pattern": "*", "action": "allow"},
        ]


@pytest.mark.parametrize(
    "status, code",
    [("401", "MODEL_AUTH_FAILED"), ("403", "MODEL_AUTH_FAILED"), ("404", "MODEL_REQUEST_INVALID")],
)
def test_auth_or_invalid_model_http_is_never_replayed(status: str, code: str) -> None:
    server = FakeServer()
    server.prompt_failures = [status]
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == code
    assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 1
    assert any(path.endswith("/abort") for _, path, _ in server.requests)
    assert "secret-key-must-not-be-reported" not in str(caught.value)


@pytest.mark.parametrize("accepted", [True, False])
def test_lost_prompt_response_is_observed_before_replay(accepted: bool) -> None:
    server = FakeServer()
    server.prompt_failures = ["disconnect"]
    server.accept_on_failure = accepted
    with server.runner() as runner:
        assert runner.invoke_model(bundle()) == REPLY
    paths = [path for _, path, _ in server.requests]
    prompt_indexes = [i for i, path in enumerate(paths) if path.endswith("/prompt_async")]
    assert len(prompt_indexes) == (1 if accepted else 2)
    assert paths[prompt_indexes[0] + 1] == "/session/status"
    assert paths[prompt_indexes[0] + 2].endswith("/message")


def test_transient_safe_get_retry_and_prompt_retry_are_bounded() -> None:
    server = FakeServer()
    server.health_failures = 1
    server.prompt_failures = ["503"] * 3
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "MODEL_SERVER_ERROR"
    assert sum(path == "/global/health" for _, path, _ in server.requests) == 2
    assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 3


@pytest.mark.parametrize("cancel", [False, True])
def test_deadline_or_cancellation_aborts_remote_execution(cancel: bool) -> None:
    server = FakeServer()
    server.hang = True
    if cancel:
        server.cancel_event = threading.Event()
    with (
        server.runner(timeout_seconds=0.08 if not cancel else 5) as runner,
        pytest.raises(AnalyzerError) as caught,
    ):
        runner.invoke_model(bundle())
    assert caught.value.code == ("MODEL_CANCELLED" if cancel else "MODEL_TIMEOUT")
    assert runner.calls[0]["remoteAbortRequested"] is True
    assert runner.calls[0]["remoteAbortConfirmed"] is True


@pytest.mark.parametrize(
    "error, code",
    [
        (
            {"name": "ProviderAuthError", "data": {"message": "secret-key-must-not-be-reported"}},
            "MODEL_AUTH_FAILED",
        ),
        (
            {"name": "APIError", "data": {"statusCode": 404, "message": "secret-key-must-not-be-reported"}},
            "MODEL_NOT_FOUND",
        ),
        ({"name": "StructuredOutputError", "data": {}}, "MODEL_STRUCTURED_OUTPUT_FAILED"),
    ],
)
def test_provider_response_error_codes_and_no_credential_leak(error: dict, code: str) -> None:
    server = FakeServer()
    server.error = error
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == code
    assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 1
    assert "secret-key-must-not-be-reported" not in json.dumps(runner.calls)
    assert "secret-key-must-not-be-reported" not in str(caught.value)


@pytest.mark.parametrize(
    "change, code",
    [
        ("version", "OPENCODE_VERSION_MISMATCH"),
        ("provider", "MODEL_AUTH_MISSING"),
        ("model", "MODEL_NOT_FOUND"),
        ("permission", "OPENCODE_ISOLATION_INVALID"),
        ("steps", "OPENCODE_ISOLATION_INVALID"),
        ("doc", "OPENCODE_SCHEMA_INVALID"),
    ],
)
def test_runtime_discovery_rejects_unsafe_or_wrong_configuration(change: str, code: str) -> None:
    server = FakeServer()
    if change == "version":
        server.version = "0.0.0"
    elif change == "provider":
        server.connected = []
    elif change == "model":
        server.model = "missing-model"
    elif change == "permission":
        server.policy = {**POLICY, "permission": {"*": "allow"}}
    elif change == "steps":
        server.policy = {
            **POLICY,
            "agent": {
                "iris-analyzer": {
                    **POLICY["agent"]["iris-analyzer"],
                    "steps": 3,
                }
            },
        }
    else:
        server.doc["paths"].pop("/session/status")
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == code
    assert not any(path.endswith("/prompt_async") for _, path, _ in server.requests)


def test_invalid_model_object_and_wrong_model_identity_are_rejected() -> None:
    server = FakeServer()
    server.reply = {**REPLY, "extra": "not accepted"}
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "RESULT_SCHEMA_INVALID"
    server.assistant_provider = "different-provider"
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "MODEL_IDENTITY_MISMATCH"


def test_json_text_mode_is_explicit_and_still_requires_exact_schema() -> None:
    server = FakeServer()
    server.output_text = json.dumps(REPLY)
    with server.runner(output_mode="json_text") as runner:
        assert runner.invoke_model(bundle()) == REPLY
        assert "format" not in runner.last_request
    server.output_text = "```json\n" + json.dumps(REPLY) + "\n```"
    with server.runner(output_mode="json_text") as runner:
        assert runner.invoke_model(bundle()) == REPLY
        assert runner.calls[0]["formatRecovery"] is True
        assert runner.last_response["parts"][0]["text"] == server.output_text


def test_compact_payload_hides_excluded_paths_and_is_deterministic() -> None:
    payload = model_input(bundle())
    assert [item["path"] for item in payload["manifest"]] == ["package.json"]
    assert payload["availablePaths"] == [{"path": "src/server.ts", "kind": "source"}]
    assert ".env" not in json.dumps(payload)
    assert model_input(bundle()) == model_input(bundle())


def test_configuration_repr_hides_keys_and_dotenv_does_not_mutate_environment(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HIVE_AI=dotenv-secret\nHIVE_MODEL=fixture-model\n")
    monkeypatch.setenv("HIVE_AI", "environment-secret")
    cfg = ModelConfig.from_env(env_file)
    assert cfg.api_key == "environment-secret"
    assert cfg.model == "fixture-model"
    assert "environment-secret" not in repr(cfg)
    with pytest.raises(AnalyzerError, match="placeholder"):
        replace(cfg, api_key="xxx")


def test_pinned_structured_user_encoder_bug_waits_for_latest_assistant_without_replay() -> None:
    server = FakeServer()
    server.pending_format_reads = 1
    with server.runner() as runner:
        assert runner.invoke_model(bundle()) == REPLY
    assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 1


def test_unsupported_hive_structured_tool_request_is_an_explicit_failure() -> None:
    server = FakeServer()
    server.error = {"name": "APIError", "data": {"statusCode": 400, "isRetryable": False}}
    with server.runner(output_mode="structured") as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "MODEL_EXECUTION_FAILED"
    assert runner.calls[0]["outputMode"] == "structured"
    assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 1


def test_message_ids_match_pinned_time_ordered_scheme() -> None:
    from iris_analyzer.opencode.client import new_message_id

    first, second = new_message_id(), new_message_id()
    assert len(first) == 30 and len(second) == 30
    assert first.startswith("msg_")
    assert first < second


def test_failure_after_a_previous_invocation_does_not_reuse_request_payload() -> None:
    server = FakeServer()
    with server.runner() as runner:
        runner.invoke_model(bundle())
        assert runner.last_request is not None
        server.version = "0.0.0"
        with pytest.raises(AnalyzerError):
            runner.invoke_model(bundle(2))
        assert runner.last_request is None


def test_call_and_token_budgets_block_before_a_paid_prompt() -> None:
    server = FakeServer()
    with server.runner(max_model_calls=1) as runner:
        runner.invoke_model(bundle())
        with pytest.raises(AnalyzerError) as caught:
            runner.invoke_model(bundle(2))
        assert caught.value.code == "MODEL_BUDGET_EXCEEDED"
        assert runner.model_calls == 1
        assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 1
    server = FakeServer()
    with server.runner(max_total_tokens=1000) as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "MODEL_BUDGET_EXCEEDED"
    assert not any(path.endswith("/prompt_async") for _, path, _ in server.requests)
    assert runner.last_request is None


def test_incurred_usage_and_hive_cost_estimate_survive_schema_failure() -> None:
    server = FakeServer()
    server.reply = {"invalid": "reply"}
    with server.runner(output_mode="json_text") as runner, pytest.raises(AnalyzerError):
        runner.invoke_model(bundle())
    call = runner.calls[0]
    assert runner.total_tokens == 19
    assert call["cumulativeTokensUpperBound"] == 19
    assert call["cost"] is None
    assert call["estimatedCostUsd"] == pytest.approx((12 * 0.05 + 7 * 0.17) / 1_000_000)
    assert call["pricing"]["source"] == "https://thehive.ai/models/zai-org/glm-5.3-flash"
    assert runner.last_response["info"]["structured"] == {"invalid": "reply"}


def test_interrupted_usage_keeps_conservative_token_reservation() -> None:
    server = FakeServer()
    server.message_disconnect = True
    with server.runner() as runner, pytest.raises(AnalyzerError):
        runner.invoke_model(bundle())
    call = runner.calls[0]
    assert call["usage"] is None
    assert call["cumulativeTokensUpperBound"] == call["reservedTokensUpperBound"]
    assert call["estimatedCostUsd"] is None


def test_default_zero_remote_retries_aborts_before_runtime_replay() -> None:
    server = FakeServer()
    server.hang = True
    server.retry_attempt = 1
    with server.runner() as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "MODEL_RETRY_LIMIT_EXCEEDED"
    assert runner.calls[0]["remoteAbortConfirmed"] is True
    assert runner.calls[0]["remoteRetryAttempts"] == 1
    assert sum(path.endswith("/prompt_async") for _, path, _ in server.requests) == 1


def test_native_error_body_is_never_persisted_in_last_response() -> None:
    server = FakeServer()
    server.error = {
        "name": "APIError",
        "data": {
            "statusCode": 400,
            "message": "secret-key-must-not-be-reported",
            "responseBody": "provider request echo",
        },
    }
    with server.runner() as runner, pytest.raises(AnalyzerError):
        runner.invoke_model(bundle())
    assert runner.last_response["info"]["error"] == {"name": "APIError"}
    assert "secret-key-must-not-be-reported" not in json.dumps(runner.last_response)
    assert "provider request echo" not in json.dumps(runner.last_response)


def test_response_template_has_exact_identity_and_valid_schema() -> None:
    from iris_analyzer.contracts import validate_reply
    from iris_analyzer.opencode.runner import response_template

    template = response_template(bundle())
    validate_reply(template)
    assert template["result"]["contextHash"] == bundle()["contextHash"]
    assert template["result"]["services"] == []


def test_pinned_usage_estimate_counts_reasoning_and_cache_without_double_count() -> None:
    server = FakeServer()
    server.usage = {"input": 12, "output": 7, "reasoning": 3, "cache": {"write": 2, "read": 4}}
    with server.runner() as runner:
        runner.invoke_model(bundle())
    assert runner.total_tokens == 28
    expected = ((12 + 2) * 0.05 + (7 + 3) * 0.17 + 4 * 0.01) / 1_000_000
    assert runner.calls[0]["estimatedCostUsd"] == pytest.approx(expected)


def test_reasoning_default_is_hive_glm_specific_and_env_override_is_explicit(monkeypatch) -> None:
    assert ModelConfig().reasoning_effort == "low"
    assert ModelConfig(model="deepseek-ai/deepseek-v4.1-flash").reasoning_effort is None
    assert ModelConfig(provider="other-provider").reasoning_effort is None
    monkeypatch.setenv("OPENCODE_REASONING_EFFORT", "high")
    assert ModelConfig.from_env().reasoning_effort == "high"
    with pytest.raises(AnalyzerError) as caught:
        ModelConfig(reasoning_effort="disabled")
    assert caught.value.code == "MODEL_CONFIG_INVALID"


@pytest.mark.parametrize(
    "text",
    [
        "Reply: " + json.dumps(REPLY) + "\nFinished.",
        "Prose mentions {ordinary braces}.\n" + json.dumps(REPLY),
    ],
)
def test_unambiguous_json_recovery_preserves_raw_output_and_records_format_issue(text: str) -> None:
    server = FakeServer()
    server.output_text = text
    with server.runner(output_mode="json_text") as runner:
        assert runner.invoke_model(bundle()) == REPLY
        assert runner.calls[0]["formatRecovery"] is True
        assert runner.last_response["parts"][0]["text"] == text


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(REPLY) + '\n{"another":"object"}',
        json.dumps(REPLY) + '\n{"incomplete":',
        'Broken {"kind":"analysis","result": ' + json.dumps(REPLY),
        "[ " + json.dumps(REPLY) + " ]",
    ],
)
def test_json_recovery_rejects_multiple_partial_and_nested_ambiguous_objects(text: str) -> None:
    server = FakeServer()
    server.output_text = text
    with server.runner(output_mode="json_text") as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "RESULT_SCHEMA_INVALID"


def test_completed_json_prefix_is_rejected_when_provider_reports_truncation() -> None:
    from iris_analyzer.opencode.runner import parse_json_reply

    with pytest.raises(AnalyzerError) as caught:
        parse_json_reply(json.dumps(REPLY), finish_reason="length")
    assert caught.value.code == "RESULT_SCHEMA_INVALID"


def test_model_schema_forbids_detected_reemission_without_changing_public_schema() -> None:
    from iris_analyzer.contracts import MODEL_REPLY_SCHEMA, validate_reply
    from iris_analyzer.opencode.runner import response_template, validate_model_proposal

    reply = response_template(bundle())
    reply["result"]["environmentKeys"] = [
        {
            "value": "DATABASE_URL",
            "status": "detected",
            "scope": "source",
            "evidenceIds": ["existing-observation"],
            "reason": "Observed environment key",
        }
    ]
    validate_reply(reply)
    assert "detected" in MODEL_REPLY_SCHEMA["$defs"]["field"]["properties"]["status"]["enum"]
    with pytest.raises(AnalyzerError) as caught:
        validate_model_proposal(reply)
    assert caught.value.code == "RESULT_SCHEMA_INVALID"
    reply["result"]["environmentKeys"][0]["status"] = "suggested"
    validate_model_proposal(reply)


def test_json_recovery_does_not_relax_detected_field_policy() -> None:
    from iris_analyzer.opencode.runner import response_template

    server = FakeServer()
    reply = response_template(bundle())
    reply["result"]["environmentKeys"] = [
        {
            "value": "DATABASE_URL",
            "status": "detected",
            "scope": "source",
            "evidenceIds": ["invented"],
            "reason": "Incorrect proposal",
        }
    ]
    server.output_text = json.dumps(reply) + "\nUnsolicited commentary."
    with server.runner(output_mode="json_text") as runner, pytest.raises(AnalyzerError) as caught:
        runner.invoke_model(bundle())
    assert caught.value.code == "RESULT_SCHEMA_INVALID"
    assert runner.calls[0]["formatRecovery"] is True
    assert runner.last_response["parts"][0]["text"] == server.output_text


def test_native_json_mode_defaults_to_verified_identity_and_env_can_disable(monkeypatch) -> None:
    assert ModelConfig().native_json_mode is True
    assert ModelConfig(output_mode="structured").native_json_mode is False
    assert ModelConfig(model="other-model").native_json_mode is False
    monkeypatch.setenv("OPENCODE_NATIVE_JSON_MODE", "false")
    assert ModelConfig.from_env(native_json_mode=None).native_json_mode is False


@pytest.mark.parametrize(
    "mismatch",
    [
        "oversized_limit",
        "missing_limit",
        "null_limit",
        "missing_options",
        "effort",
        "missing_effort",
        "native_format",
        "missing_native_format",
        "temperature_capability",
        "missing_temperature_capability",
        "null_capabilities",
        "agent_temperature",
        "missing_agent_temperature",
        "raw_output_override",
    ],
)
def test_external_server_policy_mismatch_fails_before_a_paid_prompt(mismatch: str) -> None:
    server = FakeServer()
    with server.runner(output_mode="json_text") as runner:
        settings = server.model_settings
        if mismatch == "oversized_limit":
            settings["limit"]["output"] = runner.config.max_output_tokens + 1
        elif mismatch == "missing_limit":
            settings.pop("limit")
        elif mismatch == "null_limit":
            settings["limit"] = None
        elif mismatch == "missing_options":
            settings.pop("options")
        elif mismatch == "effort":
            settings["options"]["reasoningEffort"] = "max"
        elif mismatch == "missing_effort":
            settings["options"].pop("reasoningEffort")
        elif mismatch == "native_format":
            settings["options"]["response_format"] = {"type": "json_schema"}
        elif mismatch == "missing_native_format":
            settings["options"].pop("response_format")
        elif mismatch == "temperature_capability":
            settings["capabilities"]["temperature"] = False
        elif mismatch == "missing_temperature_capability":
            settings["capabilities"].pop("temperature")
        elif mismatch == "null_capabilities":
            settings["capabilities"] = None
        elif mismatch in {"agent_temperature", "missing_agent_temperature"}:
            server.policy = json.loads(json.dumps(POLICY))
            agent = server.policy["agent"]["iris-analyzer"]
            if mismatch == "agent_temperature":
                agent["temperature"] = 1
            else:
                agent.pop("temperature")
        else:
            # Arbitrary provider options would override SDK max_tokens despite
            # an apparently safe model.limit.output, defeating the reservation.
            settings["options"]["max_tokens"] = 100_000
        with pytest.raises(AnalyzerError) as caught:
            runner.invoke_model(bundle())
    assert caught.value.code == "OPENCODE_POLICY_INVALID"
    assert not any(path.endswith("/prompt_async") for _, path, _ in server.requests)


def test_lower_external_model_cap_is_accepted_and_effective_options_are_recorded() -> None:
    server = FakeServer()
    with server.runner(output_mode="json_text") as runner:
        server.model_settings["limit"]["output"] = 4096
        server.output_text = json.dumps(REPLY)
        assert runner.invoke_model(bundle()) == REPLY
    assert runner.calls[0]["effectiveInferenceOptions"] == {
        "maxOutputTokens": 4096,
        "temperature": 0,
        "reasoningEffort": "low",
        "responseFormat": {"type": "json_object"},
    }
