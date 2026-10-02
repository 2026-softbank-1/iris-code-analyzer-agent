"""Read an Organization and pin all selected repositories before downloading."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..contracts import AnalyzerError
from .sources import InventoryResult, RepositorySource, SourceLimits, unpack_repository_archive

_ORGANIZATION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA = re.compile(r"[a-fA-F0-9]{40}\Z")
_API = "https://api.github.com"
_ARCHIVE_HOSTS = {"api.github.com", "codeload.github.com"}


def parse_organization(value: str) -> str:
    value = value.strip()
    if "://" in value:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname == "github.com"
                and parsed.port is None
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise AnalyzerError(
                "ORGANIZATION_INVALID", "Use an Organization slug or GitHub HTTPS Organization URL."
            )
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 1:
            raise AnalyzerError(
                "ORGANIZATION_INVALID", "Provide the Organization URL, without a repository path."
            )
        value = parts[0]
    if not _ORGANIZATION.fullmatch(value):
        raise AnalyzerError("ORGANIZATION_INVALID", "The GitHub Organization name is invalid.")
    return value


def _matches(full_name: str, patterns: list[str]) -> bool:
    name = full_name.split("/", 1)[1]
    return any(
        fnmatch.fnmatchcase(full_name.lower(), pattern.lower())
        or fnmatch.fnmatchcase(name.lower(), pattern.lower())
        for pattern in patterns
    )


class GithubOrganizationClient:
    """Authenticated read-only GitHub access with explicit visibility limits.

    GITHUB_TOKEN/GH_TOKEN or the user's existing ``gh auth token`` supplies
    credentials. Tokens, local paths and signed archive URLs never enter the
    inventory. An injected HTTP client is retained and closed by its caller.
    ``installation_mode`` lists an App installation's accessible repositories;
    the default endpoint lists repositories visible to the credential in an org.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        token: str | None = None,
        installation_mode: bool = False,
        limits: SourceLimits | None = None,
        use_gh_auth: bool = True,
    ) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        self._owns_client = http_client is None
        self._token = (
            token if token is not None else os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        )
        self._credentials_checked = self._token is not None or not use_gh_auth
        self.installation_mode = installation_mode
        self.limits = limits or SourceLimits()

    async def __aenter__(self) -> GithubOrganizationClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _credentials(self) -> None:
        if self._credentials_checked:
            return
        self._credentials_checked = True
        try:
            process = await asyncio.create_subprocess_exec(
                "gh",
                "auth",
                "token",
                "--hostname",
                "github.com",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return  # Public Organizations also work without credentials.
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            return
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode == 0:
            self._token = stdout.decode("utf-8", errors="strict").strip() or None

    def _request(self, url: str, *, authenticated: bool) -> httpx.Request:
        request = self._client.build_request("GET", url, timeout=60)
        # Strip injected-client defaults, including cross-host authorization.
        for header in ("authorization", "proxy-authorization", "cookie"):
            request.headers.pop(header, None)
        request.headers["Accept"] = "application/vnd.github+json"
        request.headers["X-GitHub-Api-Version"] = "2022-11-28"
        if authenticated and self._token:
            request.headers["Authorization"] = f"Bearer {self._token}"
        return request

    async def _json(self, path: str, *, params: dict | None = None) -> tuple[dict | list, httpx.Headers]:
        await self._credentials()
        url = httpx.URL(_API + path, params=params)
        try:
            response = await self._client.send(
                self._request(str(url), authenticated=True),
                auth=None,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise AnalyzerError("GITHUB_ACCESS_FAILED", "GitHub could not be reached.") from error
        if response.status_code != 200:
            code = "GITHUB_RATE_LIMIT" if response.status_code in {403, 429} else "GITHUB_ACCESS_FAILED"
            raise AnalyzerError(
                code, "GitHub repository access failed.", {"httpStatus": response.status_code}
            )
        try:
            return response.json(), response.headers
        except ValueError as error:
            raise AnalyzerError("GITHUB_RESPONSE_INVALID", "GitHub returned an invalid response.") from error

    async def discover(
        self,
        organization: str,
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        refs: dict[str, str] | None = None,
        max_repositories: int = 100,
    ) -> InventoryResult:
        org = parse_organization(organization)
        if type(max_repositories) is not int or max_repositories <= 0:
            raise ValueError("max_repositories must be a positive integer")
        include, exclude, refs = include or [], exclude or [], refs or {}
        inventory = InventoryResult(organization=org)
        inventory.limitations.append(
            {
                "code": "credential_visibility",
                "scope": "inventory",
                "message": "Only repositories visible to the credentials are listed; hidden repositories cannot be counted.",
            }
        )
        inventory.limitations.append(
            {
                "code": "independent_revision_resolution",
                "scope": "snapshot",
                "message": "Each repository is pinned independently; this is not an atomic Organization-wide snapshot.",
            }
        )
        endpoint = "/installation/repositories" if self.installation_mode else f"/orgs/{org}/repos"
        page = 1
        seen: set[str] = set()
        matched_includes: set[str] = set()
        listing_count = 0
        while True:
            try:
                params = {"per_page": 100, "page": page}
                if not self.installation_mode:
                    params.update(sort="full_name", direction="asc")
                payload, headers = await self._json(endpoint, params=params)
            except AnalyzerError as error:
                if page == 1:
                    raise
                inventory.completeness = "partial"
                inventory.limitations.append({"code": error.code, "scope": "inventory", "page": page})
                break
            rows = (
                payload.get("repositories")
                if self.installation_mode and isinstance(payload, dict)
                else payload
            )
            if not isinstance(rows, list):
                raise AnalyzerError("GITHUB_RESPONSE_INVALID", "GitHub repository listing must be an array.")
            has_next = 'rel="next"' in headers.get("link", "")
            if not rows:
                break
            limited = False
            for data in rows:
                if not isinstance(data, dict):
                    raise AnalyzerError("GITHUB_RESPONSE_INVALID", "GitHub repository metadata is invalid.")
                full_name = data.get("full_name", "")
                if not isinstance(full_name, str) or not _REPOSITORY.fullmatch(full_name):
                    raise AnalyzerError("GITHUB_RESPONSE_INVALID", "GitHub repository identity is invalid.")
                if full_name.split("/", 1)[0].lower() != org.lower():
                    continue
                listing_count += 1
                if listing_count > max_repositories:
                    limited = True
                    break
                repository_id = str(data.get("id", ""))
                if not repository_id.isdigit() or repository_id == "0":
                    raise AnalyzerError("GITHUB_RESPONSE_INVALID", "GitHub repository ID is invalid.")
                if repository_id in seen:
                    inventory.completeness = "partial"
                    inventory.limitations.append({"code": "listing_changed", "repositoryId": repository_id})
                    continue
                seen.add(repository_id)
                record = {
                    "repositoryId": repository_id,
                    "fullName": full_name,
                    "url": f"https://github.com/{full_name}",
                    "ref": None,
                    "commitSha": None,
                    "status": "selected",
                    "archived": bool(data.get("archived")),
                    "fork": bool(data.get("fork")),
                    "template": bool(data.get("is_template")),
                    "private": bool(data.get("private")),
                    "coverage": {"status": "pending"},
                }
                explicit = bool(include) and _matches(full_name, include)
                for pattern in include:
                    if _matches(full_name, [pattern]):
                        matched_includes.add(pattern)
                reason = None
                if include and not explicit:
                    reason = "not_included"
                elif _matches(full_name, exclude):
                    reason = "excluded"
                elif not explicit:
                    reason = next((name for name in ("archived", "fork", "template") if record[name]), None)
                if reason:
                    record.update(status="skipped", reason=reason, coverage={"status": "excluded"})
                else:
                    ref = refs.get(
                        full_name, refs.get(full_name.split("/", 1)[1], data.get("default_branch"))
                    )
                    if (
                        not isinstance(ref, str)
                        or not ref.strip()
                        or len(ref) > 300
                        or any(ord(c) < 32 for c in ref)
                    ):
                        record.update(
                            status="failed",
                            errorCode="GITHUB_REF_INVALID",
                            coverage={"status": "unavailable"},
                        )
                    else:
                        record["ref"] = ref.strip()
                inventory.repositories.append(record)
            if limited or (listing_count >= max_repositories and has_next):
                inventory.completeness = "partial"
                inventory.limitations.append(
                    {
                        "code": "repository_limit",
                        "scope": "inventory",
                        "limit": max_repositories,
                    }
                )
                break
            if not has_next:
                break
            if page >= 100:
                inventory.completeness = "partial"
                inventory.limitations.append({"code": "page_limit", "scope": "inventory", "limit": 100})
                break
            page += 1
        inventory.listed_count = len(inventory.repositories)
        for pattern in include:
            if pattern not in matched_includes:
                inventory.completeness = "partial"
                inventory.limitations.append(
                    {"code": "requested_repository_not_visible", "selector": pattern}
                )
        # Resolve every chosen revision before downloading any source, even if one fails.
        for record in inventory.repositories:
            if record["status"] == "selected":
                try:
                    commit, _ = await self._json(
                        f"/repos/{record['fullName']}/commits/{quote(record['ref'], safe='')}"
                    )
                    sha = commit.get("sha", "") if isinstance(commit, dict) else ""
                    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                        raise AnalyzerError(
                            "GITHUB_REVISION_INVALID", "GitHub did not return a fixed commit SHA."
                        )
                    record["commitSha"] = sha.lower()
                    record["coverage"] = {"status": "pinned"}
                except AnalyzerError as error:
                    record.update(status="failed", errorCode=error.code, coverage={"status": "unavailable"})
            if record["status"] == "failed":
                inventory.completeness = "partial"
                inventory.limitations.append(
                    {
                        "code": record["errorCode"],
                        "scope": "repository",
                        "repositoryId": record["repositoryId"],
                    }
                )
        return inventory

    async def _download(self, full_name: str, sha: str, archive: Path) -> None:
        await self._credentials()
        if not _REPOSITORY.fullmatch(full_name) or not _SHA.fullmatch(sha):
            raise AnalyzerError(
                "GITHUB_REVISION_INVALID", "A validated repository and fixed SHA are required."
            )
        url = f"{_API}/repos/{full_name}/tarball/{sha}"
        written = 0
        try:
            async with asyncio.timeout(120):
                for redirect in range(4):
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname not in _ARCHIVE_HOSTS
                        or parsed.port is not None
                        or parsed.username
                        or parsed.password
                    ):
                        raise AnalyzerError(
                            "GITHUB_REDIRECT_INVALID", "GitHub redirected to an untrusted archive host."
                        )
                    response = await self._client.send(
                        self._request(url, authenticated=redirect == 0),
                        auth=None,
                        follow_redirects=False,
                        stream=True,
                    )
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect == 3:
                                raise AnalyzerError(
                                    "GITHUB_REDIRECT_INVALID", "GitHub archive redirect is invalid."
                                )
                            url = urljoin(url, location)
                            continue
                        if response.status_code != 200:
                            raise AnalyzerError(
                                "GITHUB_DOWNLOAD_FAILED", "The pinned GitHub source could not be downloaded."
                            )
                        length = response.headers.get("content-length")
                        if length and length.isdigit() and int(length) > self.limits.max_archive_bytes:
                            raise AnalyzerError(
                                "SOURCE_ARCHIVE_TOO_LARGE", "Compressed source exceeds the byte limit."
                            )
                        with archive.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                written += len(chunk)
                                if written > self.limits.max_archive_bytes:
                                    raise AnalyzerError(
                                        "SOURCE_ARCHIVE_TOO_LARGE",
                                        "Compressed source exceeds the byte limit.",
                                    )
                                output.write(chunk)
                        return
                    finally:
                        await response.aclose()
        except (httpx.HTTPError, TimeoutError) as error:
            raise AnalyzerError(
                "GITHUB_DOWNLOAD_FAILED", "Pinned source download failed or timed out."
            ) from error

    async def materialize(self, inventory: InventoryResult, destination: Path) -> list[RepositorySource]:
        destination.mkdir(parents=True, exist_ok=True)
        sources: list[RepositorySource] = []
        total_bytes = 0
        for record in inventory.repositories:
            if record["status"] != "selected":
                continue
            sha = record.get("commitSha")
            repository_id = record.get("repositoryId")
            if (
                not isinstance(sha, str)
                or not _SHA.fullmatch(sha)
                or not isinstance(repository_id, str)
                or not repository_id.isdigit()
            ):
                raise AnalyzerError(
                    "GITHUB_REVISION_INVALID", "Resolve every selected revision before downloading."
                )
        for record in inventory.repositories:
            if record["status"] != "selected":
                continue
            source_root = destination / f"{record['repositoryId']}-{record['commitSha']}"
            try:
                if total_bytes >= self.limits.max_total_bytes:
                    raise AnalyzerError(
                        "SOURCE_TOTAL_TOO_LARGE", "Organization sources exceed the total byte limit."
                    )
                with tempfile.TemporaryDirectory(prefix=".iris-source-", dir=destination) as scratch:
                    archive = Path(scratch) / "source.tar.gz"
                    await self._download(record["fullName"], record["commitSha"], archive)
                    coverage = unpack_repository_archive(
                        archive,
                        source_root,
                        limits=self.limits,
                        remaining_total_bytes=self.limits.max_total_bytes - total_bytes,
                    )
                total_bytes += coverage["unpackedBytes"]
                record["coverage"] = coverage
                record["status"] = "materialized"
                sources.append(
                    RepositorySource(
                        repository_id=record["repositoryId"],
                        full_name=record["fullName"],
                        url=record["url"],
                        ref=record["ref"],
                        commit_sha=record["commitSha"],
                        source_root=source_root,
                        coverage=coverage,
                    )
                )
            except AnalyzerError as error:
                record.update(status="failed", errorCode=error.code, coverage={"status": "unavailable"})
                inventory.completeness = "partial"
                inventory.limitations.append(
                    {
                        "code": error.code,
                        "scope": "repository",
                        "repositoryId": record["repositoryId"],
                    }
                )
        return sources


# Conventional spelling is also accepted by library callers.
GitHubOrganizationClient = GithubOrganizationClient
