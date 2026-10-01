"""Supplemental checks never execute code or mistake missing context for a pass."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from iris_analyzer.contracts import AnalyzerError, Limits, digest
from iris_analyzer.preprocess import expand_context, prepare_context, release_snapshot
from iris_analyzer.readiness import build_readiness, validate_readiness


def bundle_from_files(
    files: dict[str, str],
    *,
    partial: dict[str, tuple[int, int]] | None = None,
    redacted: set[str] | None = None,
) -> dict:
    """Small contract-valid immutable context independent of core selection."""
    manifest, evidence, selected = [], [], []
    for index, (path, text) in enumerate(sorted(files.items())):
        raw = text.encode("utf-8")
        source_hash = hashlib.sha256(raw).hexdigest()
        lines = text.lstrip("\ufeff").splitlines() or [""]
        start, end = (partial or {}).get(path, (1, len(lines)))
        snippet = "\n".join(lines[start - 1 : end])
        masked = path in (redacted or set())
        if masked:
            snippet = snippet.replace("my-secret", "[REDACTED]")
        manifest.append(
            {
                "fileId": f"f{index}",
                "path": path,
                "size": len(raw),
                "digest": source_hash,
                "kind": "source",
                "eligible": True,
                "exclusionReason": None,
            }
        )
        evidence.append(
            {
                "evidenceId": f"e{index}",
                "path": path,
                "startLine": start,
                "endLine": end,
                "sourceDigest": source_hash,
                "contentDigest": hashlib.sha256(snippet.encode()).hexdigest(),
                "redacted": masked,
                "text": snippet,
            }
        )
        selected.append(
            {
                "fileId": f"f{index}",
                "path": path,
                "role": "source",
                "selectionReason": "Readiness fixture",
                "providedRanges": [{"startLine": start, "endLine": end}],
            }
        )
    bundle = {
        "schemaVersion": "1",
        "preprocessorVersion": "1",
        "policyVersion": "1",
        "profile": "deployment_v1",
        "revision": 1,
        "source": {"snapshotId": "a" * 64, "commit": None},
        "manifest": manifest,
        "componentRoots": ["."],
        "deploymentCandidates": [],
        "selectedFiles": selected,
        "facts": [],
        "relations": [],
        "unresolved": [],
        "coverage": {
            "providedEvidenceIds": [row["evidenceId"] for row in evidence],
            "omittedRelevantFiles": [],
            "unresolvedReferences": [],
            "truncated": False,
        },
        "policy": {
            "selectionVersion": "test",
            "excludedPaths": [],
            "requestedPaths": [],
            "expansionsUsed": 0,
            "maxBundleBytes": 180000,
            "maxInputTokens": None,
            "maxFileBytes": 1000000,
            "maxExpansions": 1,
            "maxRequestedFiles": 5,
            "tokenizer": None,
        },
        "evidence": evidence,
    }
    bundle["contextHash"] = digest(bundle)
    return bundle


def test_node_constraints_build_runtime_and_evidence_remain_separate():
    bundle = bundle_from_files(
        {
            "package.json": json.dumps({"engines": {"node": ">=22 <23", "npm": ">=10"}}),
            ".nvmrc": "22.4.0\n",
            "Dockerfile": "FROM node:22-alpine AS build\nRUN npm run build\nFROM nginx:1.27-alpine\n",
            "src/index.ts": "export const port: number = 3000;\n",
        }
    )
    report = build_readiness(bundle)
    versions = report["runtimeVersions"]
    assert {(row["runtime"], row["scope"], row["constraint"]) for row in versions} == {
        ("node", "source", ">=22 <23"),
        ("npm", "source", ">=10"),
        ("node", "source", "22.4.0"),
        ("node", "build", "22-alpine"),
        ("nginx", "runtime", "1.27-alpine"),
    }
    assert all(row["evidenceIds"] and row["status"] == "detected" for row in versions)
    assert report["languages"][0]["language"] == "TypeScript"
    assert report["coverage"]["targetCodeExecuted"] is False
    assert report["contextHash"] == bundle["contextHash"]


@pytest.mark.parametrize("text", ["const = ;\n", "const = ;\r\n", "\ufeffconst = ;\r\n"])
def test_complete_verified_js_syntax_error_has_line_and_source_evidence(text):
    report = build_readiness(bundle_from_files({"src/main.js": text}))
    assert report["coverage"]["syntaxCheckedFiles"] == ["src/main.js"]
    finding = report["findings"][0]
    assert finding["ruleId"] == "syntax.javascript_parser"
    assert finding["status"] == "detected" and finding["severity"] == "error"
    assert finding["startLine"] == 1 and finding["evidenceIds"]


def test_partial_and_redacted_source_are_not_parsed_as_complete():
    bundle = bundle_from_files(
        {
            "partial.ts": "export function thing() {\n  return 1;\n}\n",
            "secret.js": "const token = 'my-secret';\n",
        },
        partial={"partial.ts": (1, 1)},
        redacted={"secret.js"},
    )
    report = build_readiness(bundle)
    assert report["findings"] == []
    assert report["coverage"]["syntaxCheckedFiles"] == []
    assert {row["path"] for row in report["coverage"]["skippedFiles"]} == {"partial.ts", "secret.js"}
    assert report["coverage"]["status"] == "partial"
    assert "my-secret" not in json.dumps(report)


def test_invalid_authoritative_json_does_not_turn_jsonc_into_error():
    report = build_readiness(
        bundle_from_files({"package.json": '{"scripts": }\n', "tsconfig.json": "{// comments allowed\n}\n"})
    )
    assert report["coverage"]["jsonCheckedFiles"] == ["package.json"]
    assert report["findings"][0]["ruleId"] == "config.invalid_strict_json"
    assert report["findings"][0]["status"] == "detected"


def test_relative_import_check_resolves_typescript_js_extension_and_flags_missing():
    bundle = bundle_from_files(
        {
            "src/main.ts": "import './real.js';\nimport './missing.js';\nimport './asset.svg?raw';\n",
            "src/real.ts": "export const ok = 1;\n",
        }
    )
    report = build_readiness(bundle)
    findings = report["findings"]
    assert len(findings) == 1
    assert findings[0]["ruleId"] == "imports.unresolved_relative"
    assert findings[0]["startLine"] == 2
    assert findings[0]["status"] == "needs_review"
    assert "Generated" in findings[0]["reason"]


def test_omitted_or_excluded_import_inventory_is_not_claimed_missing():
    bundle = bundle_from_files({"src/main.ts": "import './generated.js';\n"})
    bundle["manifest"].append(
        {
            "fileId": "f-omitted",
            "path": "src/generated.js",
            "size": 100,
            "digest": None,
            "kind": "source",
            "eligible": False,
            "exclusionReason": "explicit_exclusion",
        }
    )
    bundle["contextHash"] = digest({key: value for key, value in bundle.items() if key != "contextHash"})
    assert not build_readiness(bundle)["findings"]


def test_direct_missing_command_target_is_cautious_about_generated_build_output():
    package = {
        "scripts": {
            "start": "node dist/index.js",
            "dev": "tsx src/main.ts",
            "complex": "cd server && node missing.js",
            "dynamic": "node $ENTRY",
        }
    }
    report = build_readiness(
        bundle_from_files({"package.json": json.dumps(package), "src/main.ts": "export const ok = 1;\n"})
    )
    assert len(report["findings"]) == 1
    item = report["findings"][0]
    assert item["ruleId"] == "commands.missing_literal_target"
    assert item["status"] == "needs_review"
    assert "generated" in item["reason"]


def test_incompatible_node_major_is_explicit_but_build_stage_can_differ():
    files = {
        "package.json": '{"engines":{"node":">=22 <23"}}',
        ".node-version": "20.9.0\n",
        "Dockerfile": "FROM node:20 AS build\nFROM node:22-alpine\n",
    }
    report = build_readiness(bundle_from_files(files))
    conflicts = [
        row for row in report["findings"] if row["ruleId"] == "runtime.incompatible_major_constraint"
    ]
    assert len(conflicts) == 1 and conflicts[0]["path"] == ".node-version"
    assert len(conflicts[0]["evidenceIds"]) == 2
    assert any(row["scope"] == "build" and row["status"] == "detected" for row in report["runtimeVersions"])


def test_unsupported_semver_does_not_create_guessed_conflicts():
    report = build_readiness(
        bundle_from_files({"package.json": '{"engines":{"node":">=22 || 20"}}', ".nvmrc": "20.1.0\n"})
    )
    assert not report["findings"]
    assert all(row["status"] == "detected" for row in report["runtimeVersions"])


def test_unpinned_image_and_alias_version_are_explicit_unknowns():
    report = build_readiness(bundle_from_files({"Dockerfile": "FROM node\n", ".nvmrc": "lts/*\n"}))
    assert all(row["constraint"] is None and row["status"] == "unknown" for row in report["runtimeVersions"])


def test_versioned_image_keeps_both_tag_and_immutable_digest():
    image = "node:24.21.0-alpine@sha256:" + "a" * 64
    report = build_readiness(bundle_from_files({"Dockerfile": f"FROM {image}\n"}))
    version = report["runtimeVersions"][0]
    assert version["constraint"] == "24.21.0-alpine"
    assert version["imageReference"] == image
    assert version["imageDigest"] == "sha256:" + "a" * 64
    assert version["versionKind"] == "image_tag"
    report = build_readiness(bundle_from_files({"Dockerfile": "FROM node:${NODE_VERSION}\n"}))
    assert report["runtimeVersions"][0]["constraint"] is None
    assert report["runtimeVersions"][0]["status"] == "unknown"


def test_partial_dockerfile_does_not_invent_final_stage_or_build_versions():
    report = build_readiness(
        bundle_from_files(
            {"Dockerfile": "FROM node:22\nRUN npm install\nFROM nginx:1.27\n"}, partial={"Dockerfile": (1, 1)}
        )
    )
    assert report["runtimeVersions"][0]["scope"] == "container_stage_unknown"
    assert report["coverage"]["requiredContextPaths"] == ["Dockerfile"]


def test_explicit_root_warning_only_for_final_stage_last_user():
    report = build_readiness(
        bundle_from_files({"Dockerfile": "FROM node:22 AS build\nUSER root\nFROM node:22\nUSER 0\n"})
    )
    assert report["findings"][0]["ruleId"] == "container.explicit_root_user"
    assert report["findings"][0]["startLine"] == 4
    report = build_readiness(bundle_from_files({"Dockerfile": "FROM node:22\nUSER root\nUSER app\n"}))
    assert not report["findings"]


def test_node_version_unknown_and_requested_runtime_hint_coverage():
    report = build_readiness(bundle_from_files({"package.json": '{"name":"application"}'}))
    assert report["runtimeVersions"][0]["constraint"] is None
    assert report["runtimeVersions"][0]["status"] == "unknown"
    bundle = bundle_from_files({"package.json": "{}", ".nvmrc": "24\n"})
    bundle["evidence"] = [row for row in bundle["evidence"] if row["path"] != ".nvmrc"]
    bundle["selectedFiles"] = [row for row in bundle["selectedFiles"] if row["path"] != ".nvmrc"]
    bundle["coverage"]["providedEvidenceIds"] = [row["evidenceId"] for row in bundle["evidence"]]
    bundle["contextHash"] = digest({key: value for key, value in bundle.items() if key != "contextHash"})
    assert build_readiness(bundle)["coverage"]["requiredContextPaths"] == [".nvmrc"]


def test_context_tampering_and_supplemental_schema_are_rejected():
    bundle = bundle_from_files({"package.json": "{}"})
    original = copy.deepcopy(bundle)
    report = build_readiness(bundle)
    assert bundle == original
    bundle["evidence"][0]["text"] = '{"evil":true}'
    with pytest.raises(AnalyzerError, match="unchanged"):
        build_readiness(bundle)
    report["extra"] = True
    with pytest.raises(AnalyzerError) as error:
        validate_readiness(report)
    assert error.value.code == "READINESS_SCHEMA_INVALID"


@pytest.mark.parametrize("mutation", ["duplicate", "source_digest", "range", "content"])
def test_evidence_is_checked_even_with_recomputed_context_hash(mutation):
    bundle = bundle_from_files({"main.js": "const ok = 1;\n"})
    if mutation == "duplicate":
        bundle["evidence"].append(copy.deepcopy(bundle["evidence"][0]))
    elif mutation == "source_digest":
        bundle["evidence"][0]["sourceDigest"] = "b" * 64
    elif mutation == "range":
        bundle["evidence"][0]["endLine"] += 1
    else:
        bundle["evidence"][0]["text"] = "const changed = 2;"
    bundle["contextHash"] = digest({key: value for key, value in bundle.items() if key != "contextHash"})
    with pytest.raises(AnalyzerError) as error:
        build_readiness(bundle)
    assert error.value.code == "READINESS_EVIDENCE_INVALID"


def test_compose_image_digest_and_concrete_warnings_have_line_evidence():
    text = (
        "services:\n  api:\n    image: node@sha256:" + "a" * 64 + "\n"
        "    privileged: true\n    network_mode: host\n"
        "    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n"
    )
    report = build_readiness(bundle_from_files({"compose.yaml": text}))
    version = report["runtimeVersions"][0]
    assert version["versionKind"] == "image_digest" and version["scope"] == "runtime"
    assert version["stage"] == "compose:api"
    assert {row["ruleId"] for row in report["findings"]} == {
        "container.compose_privileged",
        "container.compose_host_network",
        "container.compose_docker_socket",
    }
    assert all(row["evidenceIds"] and row["status"] == "needs_review" for row in report["findings"])
    assert report["coverage"]["containerCheckedFiles"] == ["compose.yaml"]


def test_compose_invalid_yaml_is_bounded_static_finding():
    report = build_readiness(bundle_from_files({"compose.yaml": "services: [bad\n"}))
    assert report["findings"][0]["ruleId"] == "config.invalid_compose_yaml"
    assert report["findings"][0]["status"] == "detected"


def test_strict_package_field_shapes_are_checked():
    report = build_readiness(bundle_from_files({"package.json": '{"engines":[]}'}))
    assert report["findings"][0]["ruleId"] == "config.package_field_not_object"


@pytest.mark.parametrize("name", ["Temp_log", "portpolio-production"])
def test_user_sample_contexts_have_provenance_and_no_capacity_claim(name):
    repo = Path(__file__).resolve().parents[2] / "tested_code" / name
    if not repo.is_dir():
        pytest.skip("User-provided assessment repository is not present")
    limits = Limits(max_bundle_bytes=400000, max_requested_files=8)
    bundle = prepare_context(repo, limits=limits)
    try:
        initial = build_readiness(bundle)
        requested = initial["coverage"]["requiredContextPaths"][: limits.max_requested_files]
        if requested:
            bundle = expand_context(bundle, requested, limits=limits)
        report = build_readiness(bundle)
        assert report["sourceSnapshotId"] == bundle["source"]["snapshotId"]
        assert report["coverage"]["targetCodeExecuted"] is False
        assert any(row["runtime"] == "node" for row in report["runtimeVersions"])
        assert any(row["scope"] == "runtime" for row in report["runtimeVersions"])
        assert report["coverage"]["containerCheckedFiles"]
        assert all(
            set(row["evidenceIds"]) <= set(bundle["coverage"]["providedEvidenceIds"])
            for key in ("languages", "runtimeVersions", "findings")
            for row in report[key]
        )
        assert "capacity" in " ".join(report["limitations"])
    finally:
        release_snapshot(bundle["source"]["snapshotId"])


def test_literal_rate_limit_is_configuration_review_not_measured_capacity():
    report = build_readiness(
        bundle_from_files(
            {
                "src/limits.ts": "import rateLimit from 'express-rate-limit';\nexport const apiLimiter = rateLimit({windowMs: 1 * 60 * 1000, max: 100});\n"
            }
        )
    )
    finding = next(f for f in report["findings"] if f["ruleId"] == "traffic.configured_rate_limit")
    assert finding["severity"] == "info" and finding["status"] == "needs_review"
    assert "limit=100" in finding["reason"]
    assert "not maximum server capacity" in finding["reason"]
    assert finding["evidenceIds"]


def test_unrelated_or_dynamic_rate_limit_does_not_invent_a_literal_ceiling():
    report = build_readiness(
        bundle_from_files(
            {
                "src/local.ts": "const rateLimit = x => x; const limiter = rateLimit({windowMs: 60000, max: 100});\n",
                "src/dynamic.ts": "import rateLimit from 'express-rate-limit'; const limiter = rateLimit({windowMs: 60000, max: process.env.LIMIT});\n",
            }
        )
    )
    assert not any(f["ruleId"] == "traffic.configured_rate_limit" for f in report["findings"])
