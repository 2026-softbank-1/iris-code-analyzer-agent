"""Immutable repository inputs and bounded extraction without executing source."""

from __future__ import annotations

import gzip
import shutil
import tarfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..contracts import AnalyzerError


@dataclass(frozen=True)
class SourceLimits:
    max_archive_bytes: int = 64 * 1024 * 1024
    max_repository_bytes: int = 256 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_archive_entries: int = 30_000

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class RepositorySource:
    repository_id: str
    full_name: str
    url: str
    ref: str
    commit_sha: str
    source_root: Path
    coverage: dict


@dataclass
class InventoryResult:
    organization: str
    repositories: list[dict] = field(default_factory=list)
    limitations: list[dict] = field(default_factory=list)
    completeness: str = "complete"
    listed_count: int = 0

    @property
    def records(self) -> list[dict]:
        return self.repositories

    def to_dict(self) -> dict:
        return {
            "organization": self.organization,
            "repositories": self.repositories,
            "limitations": self.limitations,
            "completeness": self.completeness,
            "listedCount": self.listed_count,
            "scope": "repositories_visible_to_credentials",
            "atomicSnapshot": False,
        }


class _BoundedReader:
    def __init__(self, reader: gzip.GzipFile, limit: int) -> None:
        self.reader = reader
        self.remaining = limit

    def read(self, size: int = -1) -> bytes:
        size = min(size, self.remaining + 1) if size >= 0 else self.remaining + 1
        value = self.reader.read(size)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise AnalyzerError("SOURCE_TOTAL_TOO_LARGE", "Decompressed archive exceeds the byte limit.")
        return value


def unpack_repository_archive(
    archive: Path,
    destination: Path,
    *,
    limits: SourceLimits | None = None,
    remaining_total_bytes: int | None = None,
) -> dict:
    """Preserve regular-file bytes/modes, rejecting the entire unsafe archive.

    Extraction has no extension/language filter: Docker COPY inputs, lockfiles,
    templates and binary build assets remain available to subsequent stages.
    A fresh destination is required, and a failed extraction is removed.
    """
    limits = limits or SourceLimits()
    byte_limit = limits.max_repository_bytes
    if remaining_total_bytes is not None:
        byte_limit = min(byte_limit, remaining_total_bytes)
    if destination.exists() or destination.is_symlink():
        raise AnalyzerError("SOURCE_DESTINATION_EXISTS", "Source extraction requires a fresh directory.")
    if archive.stat().st_size > limits.max_archive_bytes:
        raise AnalyzerError("SOURCE_ARCHIVE_TOO_LARGE", "Compressed repository archive exceeds the limit.")
    destination.mkdir(parents=True, mode=0o700)
    prefix: str | None = None
    total_bytes = 0
    file_count = 0
    seen: set[str] = set()
    directory_modes: list[tuple[Path, int]] = []
    try:
        with (
            gzip.open(archive, "rb") as decoded,
            tarfile.open(
                fileobj=_BoundedReader(decoded, byte_limit + limits.max_archive_entries * 1024), mode="r|"
            ) as stream,
        ):
            for count, member in enumerate(stream, start=1):
                if count > limits.max_archive_entries:
                    raise AnalyzerError("SOURCE_ENTRY_LIMIT", "Repository archive has too many entries.")
                raw_path = member.name
                path = PurePosixPath(raw_path)
                if (
                    not raw_path
                    or "\x00" in raw_path
                    or "\\" in raw_path
                    or path.is_absolute()
                    or ".." in raw_path.split("/")
                    or not path.parts
                ):
                    raise AnalyzerError(
                        "SOURCE_ARCHIVE_INVALID", "Repository archive contains an unsafe path."
                    )
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise AnalyzerError(
                        "SOURCE_ARCHIVE_INVALID", "Repository archive contains a nonregular entry."
                    )
                if member.issparse():
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "Sparse source entries are unsupported.")
                prefix = prefix or path.parts[0]
                if path.parts[0] != prefix:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "Repository archive must have one root.")
                if len(path.parts) == 1:
                    if not member.isdir():
                        raise AnalyzerError(
                            "SOURCE_ARCHIVE_INVALID", "Repository archive root must be a directory."
                        )
                    continue
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                if relative in seen:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "Repository archive has duplicate paths.")
                seen.add(relative)
                target = destination.joinpath(*path.parts[1:])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    directory_modes.append((target, member.mode & 0o777))
                    continue
                if member.size < 0 or member.size > limits.max_file_bytes:
                    raise AnalyzerError("SOURCE_FILE_TOO_LARGE", "A repository file exceeds the limit.")
                total_bytes += member.size
                if total_bytes > byte_limit:
                    raise AnalyzerError("SOURCE_TOTAL_TOO_LARGE", "Extracted sources exceed the byte limit.")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source_file = stream.extractfile(member)
                if source_file is None:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "A repository file cannot be read.")
                written = 0
                with source_file, target.open("xb") as output:
                    while chunk := source_file.read(65_536):
                        written += len(chunk)
                        if written > member.size:
                            raise AnalyzerError(
                                "SOURCE_ARCHIVE_INVALID", "Repository file size is inconsistent."
                            )
                        output.write(chunk)
                if written != member.size:
                    raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "Repository file is truncated.")
                target.chmod(member.mode & 0o777)
                file_count += 1
        if prefix is None:
            raise AnalyzerError("SOURCE_ARCHIVE_INVALID", "Repository archive is empty.")
        for path, mode in sorted(directory_modes, key=lambda item: len(item[0].parts), reverse=True):
            path.chmod(mode)
    except (tarfile.TarError, EOFError, OSError, ValueError, zlib.error) as error:
        _remove_extraction(destination)
        raise AnalyzerError(
            "SOURCE_ARCHIVE_INVALID", "Repository archive could not be safely extracted."
        ) from error
    except BaseException:
        _remove_extraction(destination)
        raise
    return {"status": "complete", "fileCount": file_count, "unpackedBytes": total_bytes, "omittedFiles": []}


def _remove_extraction(destination: Path) -> None:
    # Restore owner permissions before cleanup of archives containing readonly dirs.
    for path in destination.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
    shutil.rmtree(destination)
