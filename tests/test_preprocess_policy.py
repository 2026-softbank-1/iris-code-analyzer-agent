"""Security boundaries, immutable expansion and hard budget behavior."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from iris_analyzer.contracts import AnalyzerError, Limits, canonical_bytes
from iris_analyzer.preprocess import compact_model_input, expand_context, prepare_context, release_snapshot
from iris_analyzer.preprocess import snapshot as snapshot_module
from iris_analyzer.preprocess.redaction import redact
from iris_analyzer.result import static_analysis


def write(repo: Path, path: str, text: str) -> None:
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def app(repo: Path):
    write(
        repo,
        "package.json",
        json.dumps(
            {"name": "api", "dependencies": {"express": "5"}, "scripts": {"start": "node src/index.js"}}
        ),
    )
    write(repo, "src/index.js", "import express from 'express';\nconst app=express();\napp.listen(3000);\n")


def test_secrets_agent_configs_symlinks_and_binaries_are_never_provided(tmp_path):
    app(tmp_path)
    for path in [
        ".env",
        ".env.production.local",
        "private.pem",
        ".npmrc",
        "AGENTS.md",
        "opencode.json",
        ".opencode/plugin.ts",
        "ops/secrets/cloudflared-token",
        "ops/.secrets/key",
    ]:
        write(tmp_path, path, "SYNTHETIC_MUST_NOT_BE_READ_OR_SENT")
    (tmp_path / "picture.png").write_bytes(b"\x89PNG\r\n\x00")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("OUTSIDE_MUST_NOT_BE_READ")
    (tmp_path / "linked.js").symlink_to(outside)
    (tmp_path / "outside-dir").symlink_to(tmp_path.parent, target_is_directory=True)
    bundle = prepare_context(tmp_path)
    model = canonical_bytes(compact_model_input(bundle)).decode()
    assert "SYNTHETIC_MUST_NOT_BE_READ_OR_SENT" not in model
    assert "OUTSIDE_MUST_NOT_BE_READ" not in model
    assert not any("secrets/" in item["path"] for item in compact_model_input(bundle)["availablePaths"])
    manifest = {item["path"]: item for item in bundle["manifest"]}
    assert manifest["linked.js"]["exclusionReason"] == "symbolic_link"
    assert manifest["outside-dir"]["exclusionReason"] == "symbolic_link"
    assert manifest[".env"]["digest"] is None
    assert manifest["AGENTS.md"]["eligible"] is False
    assert manifest["picture.png"]["eligible"] is False


@pytest.mark.parametrize(
    "requested_path",
    [
        ".env",
        "private.pem",
        "linked.js",
        "../outside-secret.txt",
        "/etc/passwd",
        "src/../package.json",
        "src\\index.js",
        "src//index.js",
        "missing.js",
    ],
)
def test_expansion_rejects_forbidden_and_missing_files(tmp_path, requested_path):
    app(tmp_path)
    write(tmp_path, ".env", "TOKEN=private")
    write(tmp_path, "private.pem", "private-key")
    (tmp_path / "linked.js").symlink_to(tmp_path / ".env")
    initial = prepare_context(tmp_path)
    snapshot = canonical_bytes(initial)
    expanded = expand_context(initial, [requested_path])
    assert expanded["revision"] == 2
    assert expanded["source"] == initial["source"]
    assert expanded["contextHash"] != initial["contextHash"]
    assert expanded["coverage"]["rejectedRequests"][0]["path"] == requested_path
    assert any(item["key"] == "requested_file" for item in expanded["unresolved"])
    assert canonical_bytes(initial) == snapshot


def test_expansion_reads_captured_bytes_after_checkout_mutation(tmp_path):
    app(tmp_path)
    write(tmp_path, "notes.ts", "// review\nexport const marker='ORIGINAL_CAPTURED_BYTES';\n")
    initial = prepare_context(tmp_path)
    write(tmp_path, "notes.ts", "export const marker='NEW_MUTABLE_BYTES';")
    expanded = expand_context(initial, ["notes.ts"])
    text = "\n".join(item["text"] for item in expanded["evidence"] if item["path"] == "notes.ts")
    assert "ORIGINAL_CAPTURED_BYTES" in text
    assert "NEW_MUTABLE_BYTES" not in text


def test_explicit_requested_file_provides_content_past_header(tmp_path):
    app(tmp_path)
    write(tmp_path, "notes.ts", "// irrelevant header\n" * 10 + "export const review='LINE_ELEVEN';\n")
    initial = prepare_context(tmp_path)
    expanded = expand_context(initial, ["notes.ts"])
    evidence = [item for item in expanded["evidence"] if item["path"] == "notes.ts"]
    assert any("LINE_ELEVEN" in item["text"] and item["endLine"] == 11 for item in evidence)
    assert not expanded["coverage"]["truncated"]


def test_expansion_limits_and_per_request_count(tmp_path):
    app(tmp_path)
    for index in range(3):
        write(tmp_path, f"notes{index}.ts", f"export const note={index};")
    limits = Limits(max_requested_files=2)
    initial = prepare_context(tmp_path, limits=limits)
    expanded = expand_context(initial, ["notes0.ts", "notes1.ts", "notes2.ts"], limits=limits)
    assert expanded["coverage"]["rejectedRequests"] == [
        {"path": "notes2.ts", "reason": "requested_file_limit"}
    ]
    next_expansion = expand_context(expanded, ["notes2.ts"], limits=limits)
    assert next_expansion["coverage"]["rejectedRequests"] == [
        {"path": "notes2.ts", "reason": "expansion_limit"}
    ]


def test_expansion_large_request_records_budget_denial_without_dropping_observed_facts(tmp_path):
    app(tmp_path)
    write(tmp_path, "notes.ts", "// a large requested source line\n" * 10_000)
    limits = Limits(max_bundle_bytes=20_000)
    initial = prepare_context(tmp_path, limits=limits)
    expanded = expand_context(initial, ["notes.ts"], limits=limits)
    assert expanded["facts"] == initial["facts"]
    assert expanded["coverage"]["rejectedRequests"] == [{"path": "notes.ts", "reason": "context_budget"}]
    assert len(canonical_bytes(expanded)) <= limits.max_bundle_bytes


def test_source_symlink_swap_during_expansion_cannot_change_snapshot(tmp_path):
    app(tmp_path)
    write(tmp_path, "notes.ts", "export const note='SAFE';")
    initial = prepare_context(tmp_path)
    (tmp_path / "notes.ts").unlink()
    write(tmp_path, ".env", "NEVER_VISIBLE_SECRET")
    (tmp_path / "notes.ts").symlink_to(tmp_path / ".env")
    expanded = expand_context(initial, ["notes.ts"])
    text = canonical_bytes(expanded).decode()
    assert "SAFE" in text
    assert "NEVER_VISIBLE_SECRET" not in text


def test_tampered_bundle_cannot_drive_expansion(tmp_path):
    app(tmp_path)
    initial = prepare_context(tmp_path)
    tampered = copy.deepcopy(initial)
    tampered["facts"][0]["value"] = "altered"
    with pytest.raises(AnalyzerError) as error:
        expand_context(tampered, [])
    assert error.value.code == "CONTEXT_HASH_INVALID"


def test_worker_can_release_immutable_snapshot_bytes(tmp_path):
    app(tmp_path)
    write(tmp_path, "ownership-marker.txt", "unique-worker-release-fixture")
    initial = prepare_context(tmp_path)
    release_snapshot(initial["source"]["snapshotId"])
    with pytest.raises(AnalyzerError) as error:
        expand_context(initial, [])
    assert error.value.code == "SNAPSHOT_UNAVAILABLE"


def test_identical_concurrent_jobs_release_independent_snapshot_owners(tmp_path):
    app(tmp_path)
    write(tmp_path, "ownership-marker.txt", "two-concurrent-jobs-fixture")
    write(tmp_path, "notes.ts", "export const note='shared immutable bytes';")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: prepare_context(tmp_path), range(2)))
    snapshot_id = first["source"]["snapshotId"]
    assert snapshot_id == second["source"]["snapshotId"]
    assert snapshot_module._REFERENCE_COUNTS[snapshot_id] == 2
    release_snapshot(snapshot_id)
    expanded = expand_context(second, ["notes.ts"])
    assert expanded["revision"] == 2
    assert snapshot_module._REFERENCE_COUNTS[snapshot_id] == 1
    release_snapshot(snapshot_id)
    assert snapshot_id not in snapshot_module._REGISTRY
    with pytest.raises(AnalyzerError) as error:
        expand_context(second, ["notes.ts"])
    assert error.value.code == "SNAPSHOT_UNAVAILABLE"


def test_failed_prepare_releases_only_its_acquisition(tmp_path):
    app(tmp_path)
    write(tmp_path, "ownership-marker.txt", "successful-owner-and-failed-prepare-fixture")
    initial = prepare_context(tmp_path)
    snapshot_id = initial["source"]["snapshotId"]
    with pytest.raises(AnalyzerError) as error:
        prepare_context(tmp_path, limits=Limits(max_bundle_bytes=100))
    assert error.value.code == "CONTEXT_BUDGET_EXCEEDED"
    assert snapshot_module._REFERENCE_COUNTS[snapshot_id] == 1
    assert expand_context(initial, [])["source"] == initial["source"]
    release_snapshot(snapshot_id)
    assert snapshot_id not in snapshot_module._REGISTRY


def test_failed_prepare_without_prior_owner_leaves_no_snapshot(tmp_path):
    app(tmp_path)
    write(tmp_path, "ownership-marker.txt", "failed-only-prepare-fixture")
    registry_before = set(snapshot_module._REGISTRY)
    references_before = dict(snapshot_module._REFERENCE_COUNTS)
    with pytest.raises(AnalyzerError):
        prepare_context(tmp_path, limits=Limits(max_bundle_bytes=100))
    assert set(snapshot_module._REGISTRY) == registry_before
    assert snapshot_module._REFERENCE_COUNTS == references_before


def test_expansion_retains_original_git_metadata_for_same_content_id(tmp_path, monkeypatch):
    app(tmp_path)
    write(tmp_path, "ownership-marker.txt", "same-content-different-git-metadata-fixture")
    commits = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(
        snapshot_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=next(commits), returncode=0),
    )
    first = prepare_context(tmp_path)
    second = prepare_context(tmp_path)
    assert first["source"]["snapshotId"] == second["source"]["snapshotId"]
    assert first["source"]["commit"] != second["source"]["commit"]
    assert expand_context(first, [])["source"] == first["source"]
    release_snapshot(first["source"]["snapshotId"])
    release_snapshot(second["source"]["snapshotId"])


def test_excluded_output_is_not_part_of_new_snapshot(tmp_path):
    app(tmp_path)
    initial = prepare_context(tmp_path, excluded_paths=["reports"])
    write(tmp_path, "reports/context.json", json.dumps(initial))
    again = prepare_context(tmp_path, excluded_paths=["reports"])
    assert initial["contextHash"] == again["contextHash"]


def test_file_size_limit_is_enforced_without_reading_bytes(tmp_path):
    app(tmp_path)
    write(tmp_path, "giant.ts", "SENSITIVE" * 500)
    initial = prepare_context(tmp_path, limits=Limits(max_file_bytes=1000))
    item = next(item for item in initial["manifest"] if item["path"] == "giant.ts")
    assert item["exclusionReason"] == "file_size_limit"
    assert item["digest"] is None


def test_hard_budget_impossible_manifest_raises(tmp_path):
    app(tmp_path)
    with pytest.raises(AnalyzerError) as error:
        prepare_context(tmp_path, limits=Limits(max_bundle_bytes=100))
    assert error.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_budget_omissions_are_explicit_and_have_no_dangling_evidence(tmp_path):
    app(tmp_path)
    write(
        tmp_path,
        "src/index.js",
        "import express from 'express';\nconst app=express();\n"
        + "\n".join(f"app.get('/route{number}', handler);" for number in range(100))
        + "\napp.listen(3000);",
    )
    bundle = prepare_context(tmp_path, limits=Limits(max_bundle_bytes=15_000))
    assert bundle["coverage"]["truncated"]
    assert "src/index.js" in bundle["coverage"]["omittedRelevantFiles"]
    assert any(item["key"] == "input_budget" for item in bundle["unresolved"])
    evidence_ids = {item["evidenceId"] for item in bundle["evidence"]}
    assert all(set(fact["evidenceIds"]) <= evidence_ids for fact in bundle["facts"])
    assert len(canonical_bytes(bundle)) <= 15_000
    assert static_analysis(bundle)["status"] == "needs_input"


def test_conservative_token_budget_applied_as_hard_upper_bound(tmp_path):
    app(tmp_path)
    bundle = prepare_context(tmp_path, limits=Limits(max_input_tokens=12_000))
    assert bundle["policy"]["tokenizer"] == "conservative_utf8_bytes_v1"
    assert len(canonical_bytes(bundle)) <= 12_000
    with pytest.raises(AnalyzerError) as error:
        prepare_context(tmp_path, limits=Limits(max_input_tokens=10))
    assert error.value.code == "CONTEXT_BUDGET_EXCEEDED"


@pytest.mark.parametrize(
    "source",
    [
        "const API_KEY='synthetic_credential_value';\n",
        "ENV NODE_ENV=production API_KEY=synthetic_credential_value PORT=3000\n",
        "environment:\n  - API_KEY=synthetic_credential_value\n",
        'CMD ["node", "server.js", "--token", "synthetic_credential_value"]\n',
        "node server.js --password synthetic_credential_value --port 3000\n",
        "node server.js --api-key=synthetic_credential_value\n",
        "const url='mongodb://user:synthetic_credential_value@db:27017/data';\n",
        "Authorization: Bearer synthetic_credential_value\n",
        "-----BEGIN PRIVATE KEY-----\nsynthetic_credential_value\n-----END PRIVATE KEY-----\n",
    ],
)
def test_redaction_masks_credentials_and_preserves_lines(source):
    redacted, modified = redact(source, "source.js")
    assert modified
    assert "synthetic_credential_value" not in redacted
    assert redacted.count("\n") == source.count("\n")


def test_command_fact_and_snippet_both_mask_cli_credentials(tmp_path):
    app(tmp_path)
    write(
        tmp_path,
        "Dockerfile",
        'FROM node:24\nENV NODE_ENV=production API_KEY=synthetic_credential_value PORT=3000\nCMD ["node", "src/index.js", "--token", "synthetic_command_secret"]\n',
    )
    bundle = prepare_context(tmp_path)
    text = canonical_bytes(compact_model_input(bundle)).decode()
    assert "synthetic_credential_value" not in text
    assert "synthetic_command_secret" not in text
    assert any(item["key"] == "runtime.port" and item["value"] == 3000 for item in bundle["facts"])


def test_frontend_credential_uri_cannot_leak_through_structured_fact(tmp_path):
    app(tmp_path)
    write(tmp_path, "src/api.js", "fetch('https://user:synthetic_password@api.example.test/items');\n")
    bundle = prepare_context(tmp_path)
    assert "synthetic_password" not in canonical_bytes(bundle).decode()
    assert not any(item["key"] == "frontend.connection" for item in bundle["facts"])
    assert any(item["key"] == "frontend.connection" for item in bundle["unresolved"])


def test_target_source_and_package_scripts_never_execute(tmp_path):
    marker = tmp_path / "MUST_NOT_EXIST"
    app(tmp_path)
    write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "dependencies": {"express": "5"},
                "scripts": {"start": f"touch {marker}", "preinstall": f"touch {marker}"},
            }
        ),
    )
    write(tmp_path, "src/index.js", f"require('node:fs').writeFileSync('{marker}', 'bad');\n")
    prepare_context(tmp_path)
    assert not marker.exists()


def test_same_context_two_dockerfiles_are_distinct_execution_units(tmp_path):
    write(tmp_path, "package.json", json.dumps({"workspaces": ["web", "api"]}))
    write(
        tmp_path,
        "web/package.json",
        json.dumps({"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}}),
    )
    write(
        tmp_path,
        "api/package.json",
        json.dumps(
            {"dependencies": {"express": "5"}, "scripts": {"build": "tsc", "start": "node dist/index.js"}}
        ),
    )
    write(tmp_path, "Dockerfile.web", "FROM nginx:1\nWORKDIR /app/web\nEXPOSE 80\n")
    write(
        tmp_path,
        "Dockerfile.api",
        'FROM node:24\nWORKDIR /app/api\nEXPOSE 3000\nCMD ["node", "dist/index.js"]\n',
    )
    write(
        tmp_path,
        "compose.yaml",
        "services:\n  web:\n    build: {context: ., dockerfile: Dockerfile.web}\n  api:\n    build: {context: ., dockerfile: Dockerfile.api}\n",
    )
    bundle = prepare_context(tmp_path)
    assert {(item["role"], tuple(item["componentRoots"])) for item in bundle["deploymentCandidates"]} == {
        ("static", ("web",)),
        ("api", ("api",)),
    }
    result = static_analysis(bundle)
    for service in result["services"]:
        if service["componentRoots"] == ["web"]:
            assert {item["value"] for item in service["ports"]} == {80}
            assert service["startCommand"]["status"] == "unknown"
        else:
            assert {item["value"] for item in service["ports"]} == {3000}
            assert service["startCommand"]["value"] == "node dist/index.js"
    assert result["status"] == "complete"
    assert all(not str(item.get("candidateId", "")).startswith("docker-") for item in bundle["facts"])


def test_ambiguous_shared_context_does_not_claim_complete(tmp_path):
    write(tmp_path, "package.json", json.dumps({"workspaces": ["web", "api"]}))
    write(
        tmp_path,
        "web/package.json",
        json.dumps({"devDependencies": {"vite": "1"}, "scripts": {"build": "vite build"}}),
    )
    write(
        tmp_path,
        "api/package.json",
        json.dumps({"dependencies": {"express": "5"}, "scripts": {"start": "node index.js"}}),
    )
    write(tmp_path, "Dockerfile", 'FROM node:24\nCOPY . /app\nEXPOSE 3000\nCMD ["node", "index.js"]\n')
    write(tmp_path, "compose.yaml", "services:\n  app:\n    build: .\n")
    bundle = prepare_context(tmp_path)
    assert any(item["key"] == "deployment.components" for item in bundle["unresolved"])
    assert static_analysis(bundle)["status"] == "needs_input"


def test_alternate_compose_runtime_configs_remain_conditional(tmp_path):
    app(tmp_path)
    write(tmp_path, "compose.yaml", "services:\n  app:\n    build: .\n    ports: ['8080:3000']\n")
    write(tmp_path, "compose.production.yaml", "services:\n  app:\n    build: .\n    ports: ['80:3000']\n")
    bundle = prepare_context(tmp_path)
    assert any(item["key"] == "deployment.compose_variant" for item in bundle["unresolved"])
    conditional_ports = [
        item for item in bundle["facts"] if item["key"] == "runtime.port" and item["value"] == 80
    ]
    assert conditional_ports[0]["condition"] == "when Compose file compose.production.yaml is selected"
    assert static_analysis(bundle)["status"] == "needs_input"
