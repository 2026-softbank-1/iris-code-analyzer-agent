"""Byte-preserving build inputs, separate from redacted analysis snapshots.

Archives are treated as data: no checkout hooks, links, special files or path
traversal. Credential/host dependency files are excluded by explicit policy;
all other bytes (including binary assets) are retained and hashed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

from ..contracts import AnalyzerError, digest
from ..demo.github import download_archive, parse_github_url, resolve_revision

MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".aws",
    ".ssh",
    ".azure",
    ".kube",
    "secrets",
    ".secrets",
    "credentials",
}
_CREDENTIAL_FILES = {".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519", "credentials", "credentials.json"}


def excluded(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if any(part in _EXCLUDE_DIRS for part in parts):
        return "host_metadata_or_dependencies"
    name = parts[-1].lower()
    if name == ".env" or name.startswith(".env.") or name in _CREDENTIAL_FILES:
        return "credential_file_policy"
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return "credential_file_policy"
    return None


def safe_relative(value: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str):
        raise AnalyzerError("BUILD_PATH_INVALID", "Build paths must be strings")
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or any(ord(c) < 32 for c in value)
        or path.is_absolute()
        or ".." in path.parts
        or (not allow_dot and (str(path) == "."))
    ):
        raise AnalyzerError("BUILD_PATH_INVALID", "Build paths must stay inside the supplied source root")
    return path.as_posix()


def _new_root(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)


def _write(destination: Path, relative: str, data: bytes, mode: int) -> dict:
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise AnalyzerError("BUILD_SOURCE_DUPLICATE", "Source archive has duplicate paths")
    target.write_bytes(data)
    executable = bool(mode & 0o111)
    target.chmod(0o755 if executable else 0o644)
    return {
        "path": relative,
        "sizeBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "executable": executable,
    }


def _manifest(files: list[dict], omitted: list[dict], origin: dict) -> dict:
    files.sort(key=lambda item: item["path"])
    omitted.sort(key=lambda item: item["path"])
    return {
        "schemaVersion": "iris.build-source.v1",
        "origin": origin,
        "sourceManifestSha256": digest(files),
        "fileCount": len(files),
        "totalBytes": sum(item["sizeBytes"] for item in files),
        "files": files,
        "omittedFiles": omitted,
        "policy": "iris.build-source-exclusions.v1",
        "analysisSnapshotEquivalent": False,
    }


def stage_local_source(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not source.is_dir() or destination == source or source in destination.parents:
        raise AnalyzerError("BUILD_SOURCE_INVALID", "Build staging must be outside the source directory")
    _new_root(destination)
    files, omitted, count = [], [], 0
    for current, directories, names in os.walk(source, followlinks=False):
        directories.sort()
        names.sort()
        for name in list(directories):
            original = Path(current) / name
            relative = original.relative_to(source).as_posix()
            reason = excluded(relative)
            if reason:
                omitted.append({"path": relative, "reason": reason})
                directories.remove(name)
            elif original.is_symlink():
                raise AnalyzerError(
                    "BUILD_SOURCE_LINK_UNSUPPORTED",
                    "Build source links require an explicit packaging step",
                    {"path": relative},
                )
        for name in names:
            original = Path(current) / name
            relative = original.relative_to(source).as_posix()
            reason = excluded(relative)
            if reason:
                omitted.append({"path": relative, "reason": reason})
                continue
            fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise AnalyzerError(
                        "BUILD_SOURCE_SPECIAL_FILE", "Build source must contain only regular files"
                    )
                if before.st_size > MAX_FILE_BYTES:
                    raise AnalyzerError("BUILD_SOURCE_LIMIT", "Build source file exceeds the size limit")
                data = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
            if len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise AnalyzerError(
                    "BUILD_SOURCE_CHANGED", "Source changed while its build snapshot was being created"
                )
            count += len(data)
            if count > MAX_BYTES or len(files) >= MAX_FILES:
                raise AnalyzerError("BUILD_SOURCE_LIMIT", "Build source exceeds the bounded source limits")
            files.append(_write(destination, relative, data, before.st_mode))
    return _manifest(files, omitted, {"kind": "local", "revision": None, "uploadId": None})


def unpack_build_source(
    archive: Path,
    destination: Path,
    *,
    strip_root: bool = False,
    upload_id: str | None = None,
    origin: dict | None = None,
) -> dict:
    if upload_id is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", upload_id):
        raise AnalyzerError("BUILD_UPLOAD_ID_INVALID", "uploadId must be a valid immutable image tag")
    _new_root(destination)
    files, omitted, total, prefix = [], [], 0, None
    try:
        with tarfile.open(archive, "r:gz") as stream:
            for index, member in enumerate(stream, 1):
                if index > MAX_FILES * 3:
                    raise AnalyzerError("BUILD_SOURCE_LIMIT", "Build archive contains too many entries")
                relative = safe_relative(member.name, allow_dot=True)
                path = PurePosixPath(relative)
                if strip_root:
                    prefix = prefix or path.parts[0]
                    if path.parts[0] != prefix:
                        raise AnalyzerError("BUILD_ARCHIVE_INVALID", "Archive root prefixes differ")
                    if len(path.parts) == 1:
                        continue
                    relative = PurePosixPath(*path.parts[1:]).as_posix()
                if relative == "." or member.isdir():
                    continue
                reason = excluded(relative)
                if reason:
                    omitted.append({"path": relative, "reason": reason})
                    continue
                if not member.isfile():
                    raise AnalyzerError(
                        "BUILD_SOURCE_LINK_UNSUPPORTED",
                        "Build archives cannot contain links or special files",
                    )
                total += member.size
                if total > MAX_BYTES or member.size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                    raise AnalyzerError(
                        "BUILD_SOURCE_LIMIT", "Build archive exceeds the bounded source limits"
                    )
                source = stream.extractfile(member)
                if source is None:
                    raise AnalyzerError("BUILD_ARCHIVE_INVALID", "Cannot read an archive member")
                data = source.read(MAX_FILE_BYTES + 1)
                if len(data) != member.size:
                    raise AnalyzerError("BUILD_ARCHIVE_INVALID", "Archive member size mismatch")
                files.append(_write(destination, relative, data, member.mode))
    except (tarfile.TarError, EOFError) as error:
        raise AnalyzerError("BUILD_ARCHIVE_INVALID", "Invalid gzip tar source archive") from error
    return _manifest(files, omitted, origin or {"kind": "upload", "revision": None, "uploadId": upload_id})


async def fetch_github_source(url: str, ref: str | None, destination: Path) -> dict:
    """Resolve before download; requested branches never become build identities."""
    source = parse_github_url(url)
    selected, sha = await resolve_revision(source, ref)
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / (destination.name + ".tar.gz")
    if archive.exists():
        raise AnalyzerError("BUILD_SOURCE_EXISTS", "Refusing to overwrite a source archive")
    try:
        await download_archive(source, sha, archive)
        return await asyncio.to_thread(
            unpack_build_source,
            archive,
            destination,
            strip_root=True,
            origin={
                "kind": "github",
                "repositoryUrl": source.url,
                "requestedRef": selected,
                "revision": sha,
                "uploadId": None,
            },
        )
    finally:
        archive.unlink(missing_ok=True)


def verify_source(source: Path, manifest: dict) -> None:
    """Reject staged-source changes between plan validation and execution."""
    expected = manifest["files"]
    actual = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise AnalyzerError("BUILD_SOURCE_CHANGED", "Build snapshot now contains a symbolic link")
        if path.is_file():
            data = path.read_bytes()
            actual.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "sizeBytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
            )
    if actual != expected or digest(actual) != manifest["sourceManifestSha256"]:
        raise AnalyzerError("BUILD_SOURCE_CHANGED", "Build snapshot no longer matches its validated manifest")
