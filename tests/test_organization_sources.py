"""Organization source tests use synthetic GitHub responses, never live credentials."""

import asyncio
import io
import json
import stat
import tarfile
from pathlib import Path

import httpx
import pytest

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.organization.github import GithubOrganizationClient, parse_organization
from iris_analyzer.organization.sources import SourceLimits, unpack_repository_archive

SHA_A = "a" * 40
SHA_B = "b" * 40


def repository(name: str, identifier: int, **flags: object) -> dict:
    return {"id": identifier, "full_name": name, "default_branch": "main", **flags}


def archive_bytes(entries: list[tuple[str, bytes, int]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root = tarfile.TarInfo("repo-sha")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name, content, mode in entries:
            member = tarfile.TarInfo(f"repo-sha/{name}")
            member.mode = mode
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize("value", ["iris-org", "https://github.com/iris-org", "https://github.com/iris-org/"])
def test_organization_slug_and_url(value: str) -> None:
    assert parse_organization(value) == "iris-org"


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/iris-org/service",
        "https://github.com@evil.test/iris-org",
        "https://github.com/iris-org?token=private",
        "https://github.com:443/iris-org",
        "../iris-org",
        "https://github.com/%2e%2e",
        "https://github.com/iris-org#x",
    ],
)
def test_invalid_organization_rejected(value: str) -> None:
    with pytest.raises(AnalyzerError) as error:
        parse_organization(value)
    assert error.value.code == "ORGANIZATION_INVALID"


def test_paginated_listing_and_explicit_partial_coverage() -> None:
    visited = []

    def respond(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.path)
        if request.url.path == "/orgs/iris-org/repos":
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200,
                    json=[repository("iris-org/api", 1)],
                    headers={
                        "Link": '<https://api.github.com/orgs/iris-org/repos?page=2>; rel="next"',
                    },
                )
            return httpx.Response(200, json=[repository("iris-org/infra", 2), repository("iris-org/docs", 3)])
        return httpx.Response(200, json={"sha": SHA_A})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(http, token="test-only", use_gh_auth=False)
            result = await client.discover("iris-org", max_repositories=2)
        assert len(result.repositories) == 2
        assert result.completeness == "partial"
        assert any(item["code"] == "repository_limit" for item in result.limitations)
        assert result.to_dict()["atomicSnapshot"] is False
        assert result.repositories[1]["fullName"] == "iris-org/infra"
        assert result.repositories[1]["status"] == "selected"
        assert "test-only" not in json.dumps(result.to_dict())
        assert visited.count("/orgs/iris-org/repos") == 2

    asyncio.run(run())


def test_selected_refs_all_pinned_before_first_download_preserving_assets(tmp_path: Path) -> None:
    visited: list[str] = []
    binary = b"\x00\xff\x10\r\n"
    blob = archive_bytes([("assets/image.bin", binary, 0o644), ("scripts/start.sh", b"#!/bin/sh\n", 0o755)])

    def respond(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if request.url.path == "/orgs/iris-org/repos":
            return httpx.Response(200, json=[repository("iris-org/api", 1), repository("iris-org/web", 2)])
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA_A if "/api/" in request.url.path else SHA_B})
        return httpx.Response(200, content=blob)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(http, token="test-only", use_gh_auth=False)
            inventory = await client.discover("iris-org", refs={"web": "release/next"})
            sources = await client.materialize(inventory, tmp_path / "sources")
        assert len(sources) == 2
        commit_positions = [i for i, url in enumerate(visited) if "/commits/" in url]
        archive_positions = [i for i, url in enumerate(visited) if "/tarball/" in url]
        assert max(commit_positions) < min(archive_positions)
        assert any("release%2Fnext" in url for url in visited)
        assert all(SHA_A in url or SHA_B in url for url in visited if "/tarball/" in url)
        for source in sources:
            assert (source.source_root / "assets/image.bin").read_bytes() == binary
            assert stat.S_IMODE((source.source_root / "scripts/start.sh").stat().st_mode) == 0o755
            assert source.coverage["omittedFiles"] == []
        assert str(tmp_path) not in json.dumps(inventory.to_dict())

    asyncio.run(run())


def test_archive_authentication_is_not_forwarded_to_redirect(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    blob = archive_bytes([("Dockerfile", b"FROM scratch\n", 0o644)])

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"Location": f"https://codeload.github.com/iris-org/api/tar.gz/{SHA_A}"}
            )
        return httpx.Response(200, content=blob)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            headers={"Authorization": "default-secret", "Cookie": "secret"},
            follow_redirects=True,
        ) as http:
            client = GithubOrganizationClient(http, token="token-secret", use_gh_auth=False)
            await client._download("iris-org/api", SHA_A, tmp_path / "archive.tar.gz")
        assert requests[0].headers["Authorization"] == "Bearer token-secret"
        assert "Authorization" not in requests[1].headers
        assert "Cookie" not in requests[1].headers

    asyncio.run(run())


def test_untrusted_redirect_is_rejected_without_request(tmp_path: Path) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://attacker.test/source.tar.gz"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(http, token="secret", use_gh_auth=False)
            with pytest.raises(AnalyzerError) as error:
                await client._download("iris-org/api", SHA_A, tmp_path / "source.tar.gz")
        assert error.value.code == "GITHUB_REDIRECT_INVALID"
        assert calls == ["api.github.com"]

    asyncio.run(run())


def test_archived_forks_and_templates_skip_unless_explicitly_included() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orgs/iris-org/repos":
            return httpx.Response(
                200,
                json=[
                    repository("iris-org/archive", 1, archived=True),
                    repository("iris-org/fork", 2, fork=True),
                    repository("iris-org/template", 3, is_template=True),
                    repository("iris-org/docs", 4),
                ],
            )
        return httpx.Response(200, json={"sha": SHA_A})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(http, token="", use_gh_auth=False)
            default = await client.discover("iris-org")
            included = await client.discover(
                "iris-org", include=["archive", "template", "docs"], exclude=["template"]
            )
        assert [row["status"] for row in default.repositories] == [
            "skipped",
            "skipped",
            "skipped",
            "selected",
        ]
        assert included.repositories[0]["status"] == "selected"
        assert included.repositories[2]["reason"] == "excluded"

    asyncio.run(run())


def test_failed_repository_does_not_hide_other_sources(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orgs/iris-org/repos":
            return httpx.Response(200, json=[repository("iris-org/api", 1), repository("iris-org/web", 2)])
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA_A})
        if "/api/tarball/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=archive_bytes([("README.md", b"web", 0o644)]))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(http, token="", use_gh_auth=False)
            inventory = await client.discover("iris-org")
            sources = await client.materialize(inventory, tmp_path / "sources")
        assert [source.full_name for source in sources] == ["iris-org/web"]
        assert inventory.repositories[0]["status"] == "failed"
        assert inventory.completeness == "partial"
        assert inventory.repositories[0]["commitSha"] == SHA_A

    asyncio.run(run())


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE])
def test_nonregular_archive_entries_reject_entire_source(tmp_path: Path, kind: bytes) -> None:
    archive_path = tmp_path / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("repo-sha/link")
        member.type = kind
        member.linkname = "../../outside"
        archive.addfile(member)
    destination = tmp_path / "unpacked"
    with pytest.raises(AnalyzerError) as error:
        unpack_repository_archive(archive_path, destination)
    assert error.value.code == "SOURCE_ARCHIVE_INVALID"
    assert not destination.exists()


@pytest.mark.parametrize("path", ["../outside", "/outside", "repo-sha/../../outside", "repo-sha/dir\\file"])
def test_archive_path_escape_rejected(tmp_path: Path, path: str) -> None:
    archive_path = tmp_path / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo(path)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(AnalyzerError):
        unpack_repository_archive(archive_path, tmp_path / "unpacked")
    assert not (tmp_path / "unpacked").exists()


def test_file_and_total_limits_reject_without_partial_checkout(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    archive_path.write_bytes(archive_bytes([("a.bin", b"abcdefgh", 0o644), ("b.bin", b"abcdefgh", 0o644)]))
    with pytest.raises(AnalyzerError) as error:
        unpack_repository_archive(
            archive_path, tmp_path / "file-limit", limits=SourceLimits(max_file_bytes=4)
        )
    assert error.value.code == "SOURCE_FILE_TOO_LARGE"
    with pytest.raises(AnalyzerError) as error:
        unpack_repository_archive(archive_path, tmp_path / "total-limit", remaining_total_bytes=12)
    assert error.value.code == "SOURCE_TOTAL_TOO_LARGE"
    assert not (tmp_path / "file-limit").exists()
    assert not (tmp_path / "total-limit").exists()


def test_installation_listing_filters_other_organizations() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        repository("other-org/api", 1),
                        repository("iris-org/api", 2),
                    ]
                },
            )
        return httpx.Response(200, json={"sha": SHA_A})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(
                http, token="test-only", use_gh_auth=False, installation_mode=True
            )
            inventory = await client.discover("iris-org", max_repositories=1)
        assert inventory.completeness == "complete"
        assert [row["fullName"] for row in inventory.repositories] == ["iris-org/api"]

    asyncio.run(run())


def test_existing_gh_authentication_is_used_without_exposing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    command: list[str] = []

    class AuthProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, None]:
            return b"existing-credential\n", None

    async def spawn(*args: str, **_: object) -> AuthProcess:
        command.extend(args)
        return AuthProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer existing-credential"
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(http)
            inventory = await client.discover("iris-org")
        assert command == ["gh", "auth", "token", "--hostname", "github.com"]
        assert "existing-credential" not in json.dumps(inventory.to_dict())

    asyncio.run(run())


def test_archive_download_limit_is_enforced_on_stream_bytes(tmp_path: Path) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"source-data", headers={"Content-Length": "1"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = GithubOrganizationClient(
                http, token="", use_gh_auth=False, limits=SourceLimits(max_archive_bytes=4)
            )
            with pytest.raises(AnalyzerError) as error:
                await client._download("iris-org/api", SHA_A, tmp_path / "source.tar.gz")
        assert error.value.code == "SOURCE_ARCHIVE_TOO_LARGE"

    asyncio.run(run())


def test_duplicate_paths_rejected_instead_of_overwriting_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    archive_path.write_bytes(archive_bytes([("same", b"first", 0o644), ("same", b"second", 0o644)]))
    with pytest.raises(AnalyzerError) as error:
        unpack_repository_archive(archive_path, tmp_path / "unpacked")
    assert error.value.code == "SOURCE_ARCHIVE_INVALID"
    assert not (tmp_path / "unpacked").exists()
