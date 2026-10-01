import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from iris_analyzer.build.plan import BuildRequest, prepare_build
from iris_analyzer.build.prepare import prepare_source_build
from iris_analyzer.build.source import stage_local_source, unpack_build_source, verify_source
from iris_analyzer.contracts import AnalyzerError


def node_source(root):
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "test-app",
                "version": "1.0.0",
                "scripts": {"start": "node server.js"},
                "engines": {"node": "24"},
            }
        )
    )
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "test-app",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {"": {"name": "test-app", "version": "1.0.0"}},
            }
        )
    )
    (root / "server.js").write_text("require('node:http').createServer((q,r)=>r.end('ok')).listen(3000);\n")
    return root


def request(source, out, **kwargs):
    return {
        "schemaVersion": "iris.build-preparation-request.v1",
        "sourceRoot": str(source),
        "outputDirectory": str(out),
        "sourceSha": "a" * 40,
        "builder": "dockerfile",
        **kwargs,
    }


def test_existing_dockerfile_and_binary_bytes_preserved_and_credentials_excluded(tmp_path):
    source = node_source(tmp_path / "repo")
    dockerfile = b'FROM node:24-alpine\nWORKDIR /app\nCOPY . .\nCMD ["node","server.js"]\n'
    (source / "Dockerfile").write_bytes(dockerfile)
    asset = b"\x89PNG\x00\xff\x0a"
    (source / "asset.png").write_bytes(asset)
    (source / ".env").write_text("SESSION_SECRET=never-export-this\n")
    result = prepare_source_build(request(source, tmp_path / "out"))
    assert result["status"] == "ready" and result["dockerfileOrigin"] == "source"
    assert result["dockerfileSha256"] == hashlib.sha256(dockerfile).hexdigest()
    with tarfile.open(result["sourceArchive"]["path"]) as archive:
        assert archive.extractfile("source/Dockerfile").read() == dockerfile
        assert archive.extractfile("source/asset.png").read() == asset
        assert "source/.env" not in archive.getnames()
    assert "never-export-this" not in json.dumps(result)
    assert (
        result["sourceArchive"]["sha256"]
        == hashlib.sha256(Path(result["sourceArchive"]["path"]).read_bytes()).hexdigest()
    )
    assert result["executionAuthorized"] is False


def test_generation_uses_analysis_and_never_changes_original_source(tmp_path):
    source = node_source(tmp_path / "repo")
    result = prepare_source_build(request(source, tmp_path / "out"))
    assert result["status"] == "ready" and result["dockerfileOrigin"] == "controlled_template"
    assert result["analysisSourceSnapshotId"] == result["analysisResult"]["sourceSnapshotId"]
    assert result["analysisMode"] == "static"
    assert not (source / "Dockerfile").exists()
    assert not (tmp_path / "out/source/Dockerfile").exists()
    with tarfile.open(result["sourceArchive"]["path"]) as archive:
        generated = archive.extractfile("source/Dockerfile").read().decode()
        assert "FROM node:24-alpine" in generated
        assert 'CMD ["node", "server.js"]' in generated
    repeat = prepare_source_build(request(source, tmp_path / "repeat"))
    assert repeat["sourceManifestSha256"] == result["sourceManifestSha256"]
    assert repeat["sourceArchive"]["sha256"] == result["sourceArchive"]["sha256"]


def test_monorepo_selected_context_and_explicit_railpack_are_not_silently_changed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    child = node_source(root / "api")
    (child / "Dockerfile").write_text("FROM node:24-alpine\nCOPY . .\n")
    result = prepare_source_build(request(root, tmp_path / "out", rootDirectory="api"))
    assert result["rootDirectory"] == "api" and result["dockerfilePath"] == "Dockerfile"
    with pytest.raises(AnalyzerError, match="Railpack"):
        prepare_source_build(request(root, tmp_path / "other", builder="railpack"))


def test_disabled_generation_and_unsupported_profile_return_needs_input(tmp_path):
    source = node_source(tmp_path / "repo")
    result = prepare_source_build(request(source, tmp_path / "out", allowGeneration=False))
    assert result["status"] == "needs_input" and result["sourceArchive"] is None
    package = json.loads((source / "package.json").read_text())
    package["workspaces"] = ["packages/*"]
    (source / "package.json").write_text(json.dumps(package))
    result = prepare_source_build(request(source, tmp_path / "unsupported"))
    assert result["status"] == "needs_input"


def test_build_manifest_changes_and_parent_links_are_rejected(tmp_path):
    source = node_source(tmp_path / "repo")
    staged = tmp_path / "staged"
    manifest = stage_local_source(source, staged)
    (staged / "server.js").write_text("tampered")
    with pytest.raises(AnalyzerError, match="manifest"):
        prepare_build(staged, tmp_path / "out", manifest)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM scratch\n")
    (source / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AnalyzerError):
        stage_local_source(source, tmp_path / "linked-stage")


def test_preexisting_generated_symlink_cannot_redirect_output(tmp_path):
    source = node_source(tmp_path / "repo")
    staged = tmp_path / "staged"
    manifest = stage_local_source(source, staged)
    out = tmp_path / "out"
    out.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (out / "generated").symlink_to(external, target_is_directory=True)
    with pytest.raises(FileExistsError):
        prepare_build(staged, out, manifest)
    assert not (external / "Dockerfile").exists()


@pytest.mark.parametrize("member_name", ["../escape", "/escape", "a/../../escape"])
def test_uploaded_archive_traversal_is_rejected(tmp_path, member_name):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = 3
        archive.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(AnalyzerError):
        unpack_build_source(path, tmp_path / "unpacked", upload_id="upload-1")


def test_uploaded_source_keeps_asset_permissions_and_upload_identity(tmp_path):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in [
            ("public/font.woff2", b"\x00\xffasset"),
            ("run.sh", b"#!/bin/sh\n"),
            (".env", b"KEY=x"),
        ]:
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(content), 0o755
            archive.addfile(member, io.BytesIO(content))
    staged = tmp_path / "stage"
    manifest = unpack_build_source(path, staged, upload_id="upload-1")
    verify_source(staged, manifest)
    assert manifest["origin"]["uploadId"] == "upload-1"
    assert (staged / "public/font.woff2").read_bytes() == b"\x00\xffasset"
    assert (staged / "run.sh").stat().st_mode & 0o111
    assert not (staged / ".env").exists()


def test_cli_returns_sanitized_json_error_without_echoing_input(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "iris_analyzer.build.cli", "--request-stdin"],
        input='{"secret":"sk-never-echo-this"}',
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "BUILD_REQUEST_INVALID"
    assert "sk-never-echo-this" not in result.stdout + result.stderr


@pytest.mark.parametrize("path", ["../outside", "/outside", "a/../../b", "a\\b"])
def test_context_paths_rejected_before_any_output(tmp_path, path):
    source = node_source(tmp_path / "repo")
    out = tmp_path / "out"
    with pytest.raises(AnalyzerError):
        prepare_source_build(request(source, out, rootDirectory=path))
    assert not out.exists()


def test_explicit_custom_missing_dockerfile_is_a_configuration_error(tmp_path):
    source = node_source(tmp_path / "repo")
    with pytest.raises(AnalyzerError):
        prepare_source_build(request(source, tmp_path / "out", dockerfilePath="Customfile"))


def test_parent_link_in_prepare_build_rejected_even_with_unrelated_manifest(tmp_path):
    source = node_source(tmp_path / "repo")
    staged = tmp_path / "staged"
    manifest = stage_local_source(source, staged)
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM scratch\n")
    (staged / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AnalyzerError):
        prepare_build(
            staged, tmp_path / "out", manifest, BuildRequest(context="linked", dockerfile="linked/Dockerfile")
        )


def test_exact_node_declaration_is_not_replaced_by_floating_major(tmp_path):
    source = node_source(tmp_path / "repo")
    (source / ".nvmrc").write_text("24.21.0\n")
    result = prepare_source_build(request(source, tmp_path / "out"))
    with tarfile.open(result["sourceArchive"]["path"]) as archive:
        assert "FROM node:24.21.0-alpine" in archive.extractfile("source/Dockerfile").read().decode()


def vite_source(root):
    source = node_source(root)
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "web",
                "scripts": {"build": "vite build"},
                "devDependencies": {"vite": "8.3.0"},
            }
        )
    )
    (source / "index.html").write_text('<script type="module" src="/main.js"></script>')
    return source


def test_custom_vite_output_cannot_package_stale_dist(tmp_path):
    source = vite_source(tmp_path / "repo")
    (source / "vite.config.js").write_text("const outDir='site'; export default {build:{outDir}}")
    (source / "dist").mkdir()
    (source / "dist/index.html").write_text("stale-content")
    result = prepare_source_build(request(source, tmp_path / "out"))
    assert result["status"] == "needs_input" and result["sourceArchive"] is None


def test_vite_build_cleans_old_output_and_blocks_unconfigured_build_variables(tmp_path):
    source = vite_source(tmp_path / "repo")
    (source / "main.js").write_text("console.log(import.meta.env.VITE_API_URL);\n")
    result = prepare_source_build(request(source, tmp_path / "out"))
    assert result["status"] == "needs_input" and result["sourceArchive"] is None
    assert any("VITE_API_URL" in reason for reason in result["unresolvedInputs"])
    (source / "main.js").write_text("console.log('ok');\n")
    result = prepare_source_build(request(source, tmp_path / "ok"))
    assert result["status"] == "ready"
    with tarfile.open(result["sourceArchive"]["path"]) as archive:
        assert "rm -rf dist && npm run build" in archive.extractfile("source/Dockerfile").read().decode()


@pytest.mark.parametrize("directory", ["secrets", ".secrets", "credentials"])
def test_credential_directories_never_enter_generated_build_archive(tmp_path, directory):
    source = node_source(tmp_path / "repo")
    secret_dir = source / directory
    secret_dir.mkdir()
    (secret_dir / "cloudflared-token").write_text("synthetic-sensitive-fixture")
    result = prepare_source_build(request(source, tmp_path / "out"))
    with tarfile.open(result["sourceArchive"]["path"]) as archive:
        assert all(directory not in Path(name).parts for name in archive.getnames())
    assert "synthetic-sensitive-fixture" not in json.dumps(result)
