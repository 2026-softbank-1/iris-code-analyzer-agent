"""Resolve a GitHub revision and unpack source as data, without checkout hooks."""

from __future__ import annotations

import asyncio
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from ..contracts import AnalyzerError
from ..preprocess.snapshot import exclusion_reason

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_SOURCE_FILES = 2000
MAX_ARCHIVE_ENTRIES = 10000


@dataclass(frozen=True)
class GitHubSource:
    owner: str
    repository: str
    tree_ref: str | None = None

    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repository}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.name}"


def parse_github_url(value: str) -> GitHubSource:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AnalyzerError("GITHUB_URL_INVALID", "GitHub HTTPS 저장소 링크를 입력해 주세요.")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts[:2]
    ):
        raise AnalyzerError("GITHUB_URL_INVALID", "저장소 소유자와 이름을 확인해 주세요.")
    repository = parts[1].removesuffix(".git")
    if not repository or repository in {".", ".."}:
        raise AnalyzerError("GITHUB_URL_INVALID", "저장소 이름이 비어 있습니다.")
    if len(parts) > 2 and (parts[2] != "tree" or len(parts) < 4):
        raise AnalyzerError("GITHUB_URL_INVALID", "저장소 또는 tree/브랜치 링크를 사용해 주세요.")
    ref = "/".join(parts[3:]) if len(parts) > 2 else None
    if ref and (len(ref) > 300 or any(ord(c) < 32 for c in ref)):
        raise AnalyzerError("GITHUB_REF_INVALID", "브랜치 이름을 확인해 주세요.")
    return GitHubSource(parts[0], repository, ref)


async def gh_json(endpoint: str) -> dict | list:
    try:
        process = await asyncio.create_subprocess_exec(
            "gh", "api", endpoint, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as error:
        raise AnalyzerError("GITHUB_CLI_MISSING", "서버에 GitHub CLI가 필요합니다.") from error
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise AnalyzerError("GITHUB_ACCESS_FAILED", "저장소 접근 권한과 서버의 gh 로그인을 확인해 주세요.")
    return json.loads(stdout)


async def resolve_revision(source: GitHubSource, ref: str | None) -> tuple[str, str]:
    metadata = await gh_json(f"repos/{source.name}")
    selected = ref.strip() if ref else source.tree_ref or metadata["default_branch"]
    if not selected or len(selected) > 300 or any(ord(c) < 32 for c in selected):
        raise AnalyzerError("GITHUB_REF_INVALID", "브랜치·태그·커밋을 확인해 주세요.")
    if source.tree_ref and not ref:
        heads, tags = await asyncio.gather(
            gh_json(f"repos/{source.name}/git/matching-refs/heads/"),
            gh_json(f"repos/{source.name}/git/matching-refs/tags/"),
        )
        names = [item["ref"].split("/", 2)[2] for item in [*heads, *tags]]
        matches = [name for name in names if selected == name or selected.startswith(name + "/")]
        if matches:
            selected = max(matches, key=len)
    commit = await gh_json(f"repos/{source.name}/commits/{quote(selected, safe='')}")
    sha = commit.get("sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise AnalyzerError("GITHUB_REVISION_INVALID", "GitHub에서 고정 커밋을 확인하지 못했습니다.")
    return selected, sha


async def download_archive(source: GitHubSource, sha: str, archive: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        f"repos/{source.name}/tarball/{sha}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    count = 0
    try:
        async with asyncio.timeout(90):
            with archive.open("wb") as output:
                while chunk := await process.stdout.read(65536):
                    count += len(chunk)
                    if count > MAX_ARCHIVE_BYTES:
                        raise AnalyzerError(
                            "SOURCE_ARCHIVE_TOO_LARGE", "테스트 화면의 저장소 다운로드 한도를 초과했습니다."
                        )
                    output.write(chunk)
            await process.wait()
            if process.returncode:
                raise AnalyzerError("GITHUB_DOWNLOAD_FAILED", "고정 커밋의 소스를 가져오지 못했습니다.")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def unpack_source(archive: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    root_prefix = None
    declared_bytes = 0
    files = 0
    omitted = []
    try:
        with tarfile.open(archive, "r:gz") as stream:
            for entry_count, member in enumerate(stream, start=1):
                if entry_count > MAX_ARCHIVE_ENTRIES:
                    raise AnalyzerError(
                        "SOURCE_FILE_LIMIT", "테스트 화면의 아카이브 항목 한도를 초과했습니다."
                    )
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "안전하지 않은 소스 경로입니다.")
                root_prefix = root_prefix or path.parts[0]
                if path.parts[0] != root_prefix:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "소스 아카이브 루트가 일치하지 않습니다.")
                if len(path.parts) == 1:
                    continue
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                declared_bytes += member.size
                if declared_bytes > MAX_UNPACKED_BYTES:
                    raise AnalyzerError(
                        "SOURCE_ARCHIVE_TOO_LARGE", "압축을 푼 소스의 크기 한도를 초과했습니다."
                    )
                reason = exclusion_reason(relative)
                if not member.isfile():
                    if member.issym() or member.islnk():
                        omitted.append({"path": relative, "reason": "symbolic_link"})
                    continue
                if reason or member.size > 1_000_000:
                    omitted.append({"path": relative, "reason": reason or "file_size_limit"})
                    continue
                files += 1
                if files > MAX_SOURCE_FILES:
                    raise AnalyzerError("SOURCE_FILE_LIMIT", "테스트 화면의 파일 수 한도를 초과했습니다.")
                target = destination.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = stream.extractfile(member)
                if source_file is None:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "소스 파일을 읽지 못했습니다.")
                target.write_bytes(source_file.read())
    except (tarfile.TarError, EOFError) as error:
        raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "소스 아카이브를 읽지 못했습니다.") from error
    return {"fileCount": files, "omittedFiles": omitted}
