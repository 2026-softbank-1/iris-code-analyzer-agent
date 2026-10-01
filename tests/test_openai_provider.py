import json

import httpx
import pytest

from iris_analyzer.budget import BudgetedRunner
from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.demo.app import create_app
from iris_analyzer.opencode import IsolatedOpenCodeServer, ModelConfig
from iris_analyzer.opencode.pricing import pricing_for, usage_cost


@pytest.fixture(autouse=True)
def clean_provider_environment(monkeypatch):
    for key in [
        "HIVE_AI",
        "HIVE_MODEL",
        "OPENAI_API",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENCODE_PROVIDER",
        "OPENCODE_MODEL",
        "OPENCODE_REASONING_EFFORT",
        "OPENCODE_URL",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_openai_alias_selects_its_own_credential_and_luna_defaults(tmp_path):
    env = tmp_path / "settings.env"
    env.write_text("HIVE_AI=hive-fixture\nOPENAI_API=openai-fixture\nHIVE_MODEL=fixture-hive-model\n")
    selected = ModelConfig.from_env(env, provider="openai")
    assert selected.provider == "openai" and selected.model == "gpt-6-luna"
    assert selected.api_key == "openai-fixture"
    assert selected.reasoning_effort == "low" and selected.inference_temperature is None
    assert not selected.native_json_mode
    assert "openai-fixture" not in repr(selected)
    assert ModelConfig.from_env(env).api_key == "hive-fixture"


def test_openai_only_auto_selection_and_standard_key_environment_precedence(tmp_path, monkeypatch):
    env = tmp_path / "settings.env"
    env.write_text("OPENAI_API=file-fixture\n")
    assert ModelConfig.from_env(env).provider == "openai"
    monkeypatch.setenv("OPENAI_API_KEY", "environment-fixture")
    assert ModelConfig.from_env(env).api_key == "environment-fixture"


def test_openai_configuration_excludes_hive_options_and_sampling_for_luna():
    config = ModelConfig(provider="openai", model="gpt-6-luna", api_key="fixture")
    document = IsolatedOpenCodeServer(config)._configuration()
    provider = document["provider"]["openai"]
    assert provider["npm"] == "@ai-sdk/openai"
    assert provider["options"]["baseURL"] == "https://api.openai.com/v1"
    assert provider["options"]["apiKey"] == "{env:OPENAI_API_KEY}"
    assert "temperature" not in document["agent"]["iris-analyzer"]
    assert provider["models"]["gpt-6-luna"]["options"] == {"store": False, "reasoningEffort": "low"}
    assert "hive-ai" not in document["provider"]
    assert "fixture" not in json.dumps(document)


def test_selected_openai_key_missing_does_not_use_hive_key(tmp_path):
    env = tmp_path / "settings.env"
    env.write_text("HIVE_AI=fixture-hive\n")
    config = ModelConfig.from_env(env, provider="openai")
    assert config.api_key is None
    executable = tmp_path / "opencode"
    executable.write_text("not executed")
    with pytest.raises(AnalyzerError) as error:
        IsolatedOpenCodeServer(config, executable=executable).start()
    assert error.value.code == "MODEL_AUTH_MISSING"


def test_luna_none_reasoning_can_use_temperature_and_unsupported_mini_effort_rejects():
    assert (
        ModelConfig(provider="openai", model="gpt-6-luna", reasoning_effort="none").inference_temperature == 0
    )
    with pytest.raises(AnalyzerError):
        ModelConfig(provider="openai", model="gpt-4.1-mini", reasoning_effort="low")


def test_luna_usage_includes_cache_writes_reasoning_and_long_context():
    prices = pricing_for("openai", "gpt-6-luna")
    usage = {"input": 100, "output": 50, "reasoning": 25, "cache": {"read": 20, "write": 30}}
    assert usage_cost(usage, prices) == pytest.approx((100 * 0.125 + 30 * 0.125 + 20 * 0.01 + 75 * 0.5) / 1e6)
    long = {"input": 300000, "output": 50, "reasoning": 25, "cache": {"read": 20, "write": 30}}
    assert usage_cost(long, prices) == pytest.approx(
        ((300000 * 0.125 + 30 * 0.125 + 20 * 0.01) * 2 + 75 * 0.75) / 1e6
    )
    assert usage_cost({"input": 0, "output": 0}, prices) is None


def test_malformed_optional_reservation_rate_rejected_before_ledger_write(tmp_path):
    class Runner:
        config = ModelConfig(provider="openai", model="gpt-6-luna")

    prices = pricing_for("openai", "gpt-6-luna")
    prices["reservationInput"] = float("nan")
    with pytest.raises(ValueError):
        BudgetedRunner(Runner(), tmp_path / "ledger", pricing=prices)
    assert not (tmp_path / "ledger").exists()


def test_demo_provider_metadata_is_public_without_keys(tmp_path):
    import asyncio

    env = tmp_path / "settings.env"
    env.write_text("HIVE_AI=fixture-hive\nOPENAI_API=fixture-openai\n")
    app = create_app(env_file=env, artifact_root=tmp_path / "artifacts")

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            result = (await client.get("/api/config")).json()
            assert {p["id"] for p in result["providers"]} == {"hive-ai", "openai"}
            assert "fixture-openai" not in json.dumps(result)
            assert "fixture-hive" not in json.dumps(result)

    asyncio.run(check())


def test_explicit_provider_switch_uses_its_own_defaults(tmp_path):
    env = tmp_path / "settings.env"
    env.write_text(
        "OPENCODE_PROVIDER=hive-ai\nOPENCODE_MODEL=hive-only-model\n"
        "OPENCODE_OUTPUT_MODE=json_text\nOPENCODE_NATIVE_JSON_MODE=true\n"
        "OPENCODE_REASONING_EFFORT=high\nOPENAI_API=fixture\n"
    )
    selected = ModelConfig.from_env(env, provider="openai")
    assert selected.model == "gpt-6-luna"
    assert selected.output_mode == "structured"
    assert selected.reasoning_effort == "low"
    assert selected.native_json_mode is False


def test_source_schema_binds_identity_and_rejects_other_snapshot():
    from jsonschema import Draft202012Validator

    from iris_analyzer.opencode.runner import MODEL_PROPOSAL_SCHEMA, OpenCodeRunner

    bundle = {"source": {"snapshotId": "a" * 64}, "contextHash": "b" * 64}
    runner = OpenCodeRunner(
        ModelConfig(provider="openai", model="gpt-6-luna", server_url="http://unused.test")
    )
    # The archived v1 evaluation path retains exact identity constraints. v2
    # binds metadata through the validated transport parent instead of echoing it.
    runner.response_schema = MODEL_PROPOSAL_SCHEMA
    schema = runner._response_schema(bundle)
    identities = schema["$defs"]["analysis"]["properties"]
    for key, value in {
        "sourceSnapshotId": bundle["source"]["snapshotId"],
        "contextHash": bundle["contextHash"],
    }.items():
        validator = Draft202012Validator(identities[key])
        assert validator.is_valid(value)
        assert not validator.is_valid("c" * 64)
        assert "const" not in MODEL_PROPOSAL_SCHEMA["$defs"]["analysis"]["properties"][key]
