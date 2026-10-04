"""Database init scripts mounted at ``/docker-entrypoint-initdb.d`` in Compose.

Official postgres/mysql/mongo images run these files once, when the data
directory is empty. The gate only reports path, kind, hash and size; file
contents are hashed in memory and never returned. Symlinks, paths outside the
checkout or the requested root, and non-regular files are ignored.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from pathlib import Path

from ..preprocess.extractors.execution import safe_repository_path
from .scan import within

INIT_DIRECTORY = "/docker-entrypoint-initdb.d"
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_HASH_BYTES = 64 * 1024 * 1024
MAX_SCRIPTS = 20
# Longest suffix first. ``kind`` is the contract value.
_SUFFIXES = ((".sql.gz", "sql.gz"), (".sql", "sql"), (".js", "js"), (".sh", "sh"))
_SUPPORTED_KINDS = {
    "postgres": {"sql", "sql.gz", "sh"},
    "mysql": {"sql", "sql.gz", "sh"},
    "mongodb": {"js", "sh"},
}


def volume_mounts(volumes: object) -> list[tuple[str, str]]:
    """``(source, target)`` pairs of bind-like entries in short or long syntax."""
    result = []
    if not isinstance(volumes, list):
        return result
    for entry in volumes:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 2:
                result.append((parts[0], parts[1]))
        elif isinstance(entry, dict) and entry.get("type", "bind") == "bind":
            source, target = entry.get("source"), entry.get("target")
            if isinstance(source, str) and isinstance(target, str):
                result.append((source, target))
    return result


def script_kind(name: str) -> str | None:
    lowered = name.lower()
    for suffix, kind in _SUFFIXES:
        if lowered.endswith(suffix):
            return kind
    return None


def _safe_regular(repo_root: Path, path: str) -> os.stat_result | None:
    """lstat of a repository file with no symlink component, or None."""
    current = repo_root
    result = None
    for part in path.split("/"):
        current = current / part
        try:
            result = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(result.st_mode):
            return None
    return result


def mounted_files(
    repo_root: Path, scope: str, compose_dir: str, volumes: list[tuple[str, str]]
) -> dict[str, str]:
    """Container file name -> repository path for everything mounted into the init directory."""
    files: dict[str, str] = {}
    for source, target in volumes:
        target = posixpath.normpath(target)
        if target != INIT_DIRECTORY and posixpath.dirname(target) != INIT_DIRECTORY:
            continue
        if not source.startswith("."):
            continue  # named volume, absolute host path or dynamic value
        path = safe_repository_path(compose_dir, source)
        if path is None or path == "." or not within(path, scope):
            continue
        info = _safe_regular(repo_root, path)
        if info is None:
            continue
        if stat.S_ISREG(info.st_mode) and target != INIT_DIRECTORY:
            files[posixpath.basename(target)] = path
        elif stat.S_ISDIR(info.st_mode) and target == INIT_DIRECTORY:
            try:
                with os.scandir(repo_root.joinpath(*path.split("/"))) as entries:
                    for entry in entries:
                        if entry.name.startswith(".") or entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            files[entry.name] = f"{path}/{entry.name}"
            except OSError:
                continue
    return files


def _digest(repo_root: Path, path: str, size: int) -> str | None:
    if size > MAX_HASH_BYTES:
        return None
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(repo_root.joinpath(*path.split("/")), flags), "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def describe_scripts(repo_root: Path, engine: str, files: dict[str, str]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Ordered ``initScripts`` rows plus ``(code, path)`` problems."""
    kinds = _SUPPORTED_KINDS.get(engine)
    if not kinds:
        return [], []
    rows: list[dict] = []
    problems: list[tuple[str, str]] = []
    for name in sorted(files):
        kind = script_kind(name)
        if kind not in kinds:
            continue  # the image ignores it as well
        path = files[name]
        info = _safe_regular(repo_root, path)
        if info is None or not stat.S_ISREG(info.st_mode):
            continue
        row = {
            "path": path,
            "kind": kind,
            "sha256": _digest(repo_root, path, info.st_size),
            "size": info.st_size,
            "order": len(rows),
            "supported": True,
        }
        if kind == "sh":
            row["supported"] = False
            problems.append(("init_script_unsupported", path))
        elif info.st_size > MAX_FILE_BYTES:
            row["supported"] = False
            problems.append(("init_script_too_large", path))
        rows.append(row)
    if len(rows) > MAX_SCRIPTS:
        for row in rows[MAX_SCRIPTS:]:
            if row["supported"]:
                row["supported"] = False
                problems.append(("init_script_too_large", row["path"]))
        rows = rows[:MAX_SCRIPTS]
    supported = [row for row in rows if row["supported"]]
    if sum(row["size"] for row in supported) > MAX_TOTAL_BYTES:
        for row in supported:
            row["supported"] = False
            problems.append(("init_script_too_large", row["path"]))
    return rows, problems
