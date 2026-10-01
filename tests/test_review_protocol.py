"""Model deltas cannot copy server-owned status, identity or approvals."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.opencode.review_protocol import canonicalize_review, review_template
from iris_analyzer.opencode.runner import MODEL_REVIEW_SCHEMA
from iris_analyzer.preprocess import prepare_context, release_snapshot


@pytest.fixture
def source(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node index.js"},"dependencies":{"express":"5"}}'
    )
    (tmp_path / "index.js").write_text(
        "const express=require('express');\nconst app=express();\napp.listen(3000);\n"
    )
    b = prepare_context(tmp_path)
    yield b
    release_snapshot(b["source"]["snapshotId"])


def test_delta_review_is_bound_to_actual_request_context(source):
    wire = review_template()
    result = canonicalize_review(wire, source)
    assert result["result"]["sourceSnapshotId"] == source["source"]["snapshotId"]
    assert result["result"]["contextHash"] == source["contextHash"]
    assert result["result"]["services"] == []
    assert "contextHash" not in wire and "status" not in wire


@pytest.mark.parametrize(
    "key", ["contextHash", "sourceSnapshotId", "status", "result", "deploymentAuthorized", "verification"]
)
def test_model_cannot_supply_trusted_envelope_fields(key):
    wire = review_template()
    wire[key] = "untrusted"
    assert not Draft202012Validator(MODEL_REVIEW_SCHEMA).is_valid(wire)


def test_route_value_must_be_structured_and_detected_is_forbidden():
    wire = review_template()
    field = {
        "value": "GET /health",
        "status": "suggested",
        "scope": "source",
        "evidenceIds": ["e-test"],
        "reason": "test",
    }
    wire["changes"] = [{"target": "apiRoutes", "serviceId": None, "field": field}]
    validator = Draft202012Validator(MODEL_REVIEW_SCHEMA)
    assert not validator.is_valid(wire)
    field["value"] = {"method": "GET", "path": "/health", "component": "."}
    assert validator.is_valid(wire)
    field["status"] = "detected"
    assert not validator.is_valid(wire)


def test_unknown_service_and_duplicate_scalar_delta_rejected(source):
    wire = review_template()
    field = {
        "value": "node",
        "status": "suggested",
        "scope": "source",
        "evidenceIds": [source["evidence"][0]["evidenceId"]],
        "reason": "declared",
    }
    wire["changes"] = [{"target": "services.runtime", "serviceId": "invented", "field": field}]
    with pytest.raises(AnalyzerError):
        canonicalize_review(wire, source)
    wire["changes"][0]["serviceId"] = source["deploymentCandidates"][0]["candidateId"]
    wire["changes"].append(copy.deepcopy(wire["changes"][0]))
    with pytest.raises(AnalyzerError):
        canonicalize_review(wire, source)


def test_response_from_another_parent_cannot_be_bound_to_source():
    from test_opencode_transport import FakeServer, bundle

    server = FakeServer()
    with server.runner(output_mode="json_text") as runner:
        runner.client.prompt = lambda *args, **kwargs: {
            "info": {
                "id": "wrong-message",
                "parentID": "another-parent",
                "role": "assistant",
                "providerID": runner.config.provider,
                "modelID": runner.config.model,
                "tokens": {"input": 3, "output": 3},
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": json.dumps(review_template())}],
        }
        with pytest.raises(AnalyzerError) as error:
            runner.invoke_model(bundle())
        assert error.value.code == "MODEL_CONTEXT_MISMATCH"


def test_monetary_reservation_includes_full_review_input(source):
    from test_opencode_transport import FakeServer

    from iris_analyzer.contracts import canonical_bytes

    with FakeServer().runner(output_mode="json_text") as runner:
        request_bytes = len(canonical_bytes(runner._request_document(source)))
        prompt_bytes = len(runner._system_prompt().encode())
        assert runner.input_token_upper_bound(source) >= request_bytes + prompt_bytes + 4096
