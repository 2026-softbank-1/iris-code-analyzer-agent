import asyncio
import io
import tarfile
from pathlib import Path

import httpx
import pytest

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.demo.app import create_app
from iris_analyzer.demo.github import parse_github_url, resolve_revision, unpack_source
from iris_analyzer.opencode import ModelConfig

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/separated-web-api"


def archive(path, files, links=()):
    with tarfile.open(path, "w:gz") as stream:
        for name, text in files.items():
            raw = text.encode()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            stream.addfile(member, io.BytesIO(raw))
        for name, destination in links:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = destination
            stream.addfile(member)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/team/repo",
        "https://evil.test/a/b",
        "https://github.com.evil.test/a/b",
        "https://user:pass@github.com/a/b",
        "https://github.com/a/../b",
        "https://github.com/a/b/blob/main/file",
        "https://github.com/a/b?token=secret",
    ],
)
def test_repository_url_rejects_unsafe_destinations(url):
    with pytest.raises(AnalyzerError):
        parse_github_url(url)


def test_repository_url_preserves_slash_branch():
    source = parse_github_url("https://github.com/team/repo/tree/test/feature")
    assert source.name == "team/repo"
    assert source.tree_ref == "test/feature"
    assert parse_github_url("https://github.com/team/repo.git").repository == "repo"


def test_revision_resolves_longest_branch_prefix(monkeypatch):
    async def api(endpoint):
        if endpoint.endswith("/heads/"):
            return [{"ref": "refs/heads/test"}, {"ref": "refs/heads/test/feature"}]
        if endpoint.endswith("/tags/"):
            return []
        if "/commits/" in endpoint:
            assert endpoint.endswith("test%2Ffeature")
            return {"sha": "a" * 40}
        return {"default_branch": "main"}

    monkeypatch.setattr("iris_analyzer.demo.github.gh_json", api)
    assert asyncio.run(
        resolve_revision(parse_github_url("https://github.com/team/repo/tree/test/feature/src"), None)
    ) == ("test/feature", "a" * 40)


def test_archive_does_not_extract_credentials_or_links(tmp_path):
    file = tmp_path / "source.tar.gz"
    archive(
        file,
        {
            "root/package.json": "{}",
            "root/.env": "TOKEN=synthetic",
            "root/node_modules/a/index.js": "ignored",
        },
        links=[("root/external", "/etc/passwd")],
    )
    info = unpack_source(file, tmp_path / "source")
    assert (tmp_path / "source/package.json").exists()
    assert not (tmp_path / "source/.env").exists()
    assert not (tmp_path / "source/external").exists()
    assert info["fileCount"] == 1
    assert len(info["omittedFiles"]) == 3


def test_archive_traversal_is_rejected(tmp_path):
    file = tmp_path / "source.tar.gz"
    archive(file, {"root/../outside": "unsafe"})
    with pytest.raises(AnalyzerError) as error:
        unpack_source(file, tmp_path / "source")
    assert error.value.code == "SOURCE_ARCHIVE_INVALID"
    assert not (tmp_path / "outside").exists()


def test_archive_entry_limit_includes_excluded_files(tmp_path, monkeypatch):
    monkeypatch.setattr("iris_analyzer.demo.github.MAX_ARCHIVE_ENTRIES", 2)
    file = tmp_path / "source.tar.gz"
    archive(file, {f"root/node_modules/{index}.js": "" for index in range(3)})
    with pytest.raises(AnalyzerError) as error:
        unpack_source(file, tmp_path / "source")
    assert error.value.code == "SOURCE_FILE_LIMIT"


def test_review_http_failure_is_sanitized(tmp_path, monkeypatch):
    async def fail(source, ref):
        raise RuntimeError("secret synthetic-provider-token /private/local/path")

    monkeypatch.setattr("iris_analyzer.demo.app.resolve_revision", fail)
    app = create_app(model_config=ModelConfig(api_key=None), artifact_root=tmp_path)

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/api/reviews", json={"repository_url": "https://github.com/team/repo", "use_ai": False}
                )
                id = response.json()["id"]
                for _ in range(100):
                    job = (await client.get("/api/reviews/" + id)).json()
                    if job["state"] == "failed":
                        break
                    await asyncio.sleep(0.01)
                assert job["error"]["code"] == "REVIEW_FAILED"
                assert "synthetic-provider-token" not in str(job)
                assert "/private/local/path" not in str(job)

    asyncio.run(check())


def test_review_http_source_to_result_and_evidence(tmp_path, monkeypatch):
    async def resolve(source, ref):
        assert source.tree_ref == "test/feature"
        return "test/feature", "b" * 40

    async def download(source, sha, destination):
        files = {
            "root/" + p.relative_to(FIXTURE).as_posix(): p.read_text()
            for p in FIXTURE.rglob("*")
            if p.is_file()
        }
        archive(destination, files)

    monkeypatch.setattr("iris_analyzer.demo.app.resolve_revision", resolve)
    monkeypatch.setattr("iris_analyzer.demo.app.download_archive", download)
    app = create_app(model_config=ModelConfig(api_key=None), artifact_root=tmp_path)

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                home = await client.get("/")
                assert home.status_code == 200 and "Repository review" in home.text
                blocked = await client.post(
                    "/api/reviews",
                    headers={"Origin": "https://evil.test"},
                    json={"repository_url": "https://github.com/team/repo", "use_ai": False},
                )
                assert blocked.status_code == 403
                missing_key = await client.post(
                    "/api/reviews", json={"repository_url": "https://github.com/team/repo", "use_ai": True}
                )
                assert missing_key.status_code == 422
                response = await client.post(
                    "/api/reviews",
                    json={
                        "repository_url": "https://github.com/team/repo/tree/test/feature",
                        "use_ai": False,
                    },
                )
                assert response.status_code == 202
                id = response.json()["id"]
                for _ in range(100):
                    job = (await client.get("/api/reviews/" + id)).json()
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    await asyncio.sleep(0.02)
                assert job["state"] == "succeeded", job
                assert job["sourceSha"] == "b" * 40
                assert job["result"]["analysisStatus"] == "complete"
                assert job["deploymentDossier"]["deploymentPlan"]["status"] == "needs_input"
                assert job["deploymentDossier"]["execution"]["status"] == "blocked"
                assert not job["deploymentDossier"]["deploymentPlan"]["deploymentAuthorized"]
                assert len(job["result"]["analysisResult"]["services"]) == 2
                evidence_id = job["result"]["analysisResult"]["services"][0]["root"]["evidenceIds"][0]
                evidence = await client.get(f"/api/reviews/{id}/evidence/{evidence_id}")
                assert evidence.status_code == 200 and evidence.json()["text"]
                download = await client.get(f"/api/reviews/{id}/result")
                assert download.status_code == 200
                assert download.headers["content-disposition"] == 'attachment; filename="iris-review.json"'
                assert download.json()["sourceSha"] == "b" * 40
                assert download.json()["analysisResult"] == job["result"]["analysisResult"]
                assert str(tmp_path) not in str(job)
                original = job["result"]
                replan = await client.post(
                    f"/api/reviews/{id}/plan",
                    json={
                        "planning_request": {
                            "schemaVersion": "iris.planning-request.v1",
                            "target": {
                                "stack": "gcp_gke",
                                "cloud": "gcp",
                                "region": "asia-northeast3",
                                "environment": "test",
                            },
                        },
                        "use_ai": False,
                    },
                )
                assert replan.status_code == 202
                for _ in range(100):
                    changed = (await client.get("/api/reviews/" + id)).json()
                    if changed["state"] in {"succeeded", "failed"}:
                        break
                    await asyncio.sleep(0.02)
                assert changed["state"] == "succeeded", changed
                assert changed["result"] == original
                assert changed["deploymentDossier"]["deploymentPlan"]["status"] == "unsupported"
                assert (
                    changed["deploymentDossier"]["deploymentPlan"]["recommendations"]["target"]["value"][
                        "stack"
                    ]
                    == "gcp_gke"
                )
                dossier_download = await client.get(f"/api/reviews/{id}/plan")
                assert dossier_download.status_code == 200
                assert dossier_download.json() == changed["deploymentDossier"]

                restarted = create_app(model_config=ModelConfig(api_key=None), artifact_root=tmp_path)
                async with restarted.router.lifespan_context(restarted):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=restarted), base_url="http://testserver"
                    ) as after:
                        loaded = (await after.get("/api/reviews/" + id)).json()
                        assert loaded["result"] == original
                        assert loaded["deploymentDossier"] == changed["deploymentDossier"]
                        assert (
                            await after.get(f"/api/reviews/{id}/evidence/{evidence_id}")
                        ).status_code == 200

    asyncio.run(check())
