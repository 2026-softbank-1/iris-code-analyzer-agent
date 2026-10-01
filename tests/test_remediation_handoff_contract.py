"""Draft shape/transition checks; these do not simulate trusted dispatch."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts/remediation-handoff.v1.schema.json").read_text())
CASES = json.loads((ROOT / "evaluations/remediation-handoff-cases.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def test_draft_schema_and_examples_do_not_claim_runtime_dispatch():
    Draft202012Validator.check_schema(SCHEMA)
    assert CASES["runtimeDispatchImplemented"] is False
    for example in CASES["examples"]:
        VALIDATOR.validate(example["packet"])


@pytest.mark.parametrize(
    "change",
    [
        "no_logs",
        "raw_logs",
        "unredacted",
        "unknown_cause",
        "no_finding",
        "deploy",
        "push",
        "absolute_path",
        "traversal",
        "unbounded_attempts",
    ],
)
def test_unsafe_or_unverified_patch_packet_does_not_match_draft_contract(change):
    packet = copy.deepcopy(CASES["examples"][0]["packet"])
    failure, policy = packet["failure"], packet["repairPolicy"]
    if change == "no_logs":
        failure["logArtifacts"] = []
    elif change == "raw_logs":
        failure["rawLog"] = "Do not carry raw credentials in a handoff"
    elif change == "unredacted":
        failure["logArtifacts"][0]["redacted"] = False
    elif change == "unknown_cause":
        failure["causeStatus"] = "undetermined"
    elif change == "no_finding":
        failure["findingReceiptRefs"] = []
    elif change == "deploy":
        policy["deploymentAuthorized"] = True
    elif change == "push":
        policy["repositoryPushAuthorized"] = True
    elif change == "absolute_path":
        policy["allowedPaths"] = ["/etc/config"]
    elif change == "traversal":
        policy["allowedPaths"] = ["src/../../outside"]
    elif change == "unbounded_attempts":
        policy["maxPatchAttempts"] = 999
    assert not VALIDATOR.is_valid(packet)


@pytest.mark.parametrize(
    "classification", ["dockerfile_missing", "configuration_missing", "infrastructure_failure"]
)
def test_non_repair_routing_classifications_are_not_allowed_in_patch_contract(classification):
    packet = copy.deepcopy(CASES["examples"][0]["packet"])
    packet["failure"]["classification"] = classification
    assert not VALIDATOR.is_valid(packet)


def test_opaque_references_do_not_claim_to_authenticate_their_targets():
    packet = copy.deepcopy(CASES["examples"][0]["packet"])
    packet["analysis"]["verificationReceiptRef"] = "receipt:" + "9" * 64
    assert VALIDATOR.is_valid(packet)  # Valid shape; a trusted resolver must reject absent/foreign records.
    assert "does not authenticate" in SCHEMA["$comment"]
    packet["analysis"]["verificationReceiptRef"] = "https://untrusted.example/receipt"
    assert not VALIDATOR.is_valid(packet)
