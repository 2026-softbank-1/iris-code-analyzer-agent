"""Immutable, non-executing source capture and file access policy.

Excluded secrets and symbolic links are inventoried without opening their bytes.
Snapshots intentionally live outside the JSON bundle; expansion cannot reread a
mutable checkout or follow a link added after capture.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from iris_analyzer.contracts import AnalyzerError, Limits, digest

POLICY_VERSION = "1"
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".cache",
        ".next",
        ".nuxt",
        ".venv",
        "venv",
        "__pycache__",
        "artifacts",
        ".opencode",
        ".openai",
        ".idea",
        ".vscode",
        "vendor",
        "target",
        ".pytest_cache",
        ".ruff_cache",
        "playwright-report",
        "test-results",
        ".turbo",
        "secrets",
        ".secrets",
        ".ssh",
        ".aws",
        "credentials",
    }
)
BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".ico",
        ".pdf",
        ".mp4",
        ".mov",
        ".mp3",
        ".wav",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".zip",
        ".gz",
        ".tar",
        ".7z",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".sqlite",
        ".db",
        ".wasm",
        ".pyc",
        ".map",
    }
)
SOURCE_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"})


def normalize_request(path: str) -> str:
    """Accept only literal relative POSIX paths present in our manifest."""
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise AnalyzerError("FILE_REQUEST_REJECTED", "Invalid relative file path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"..", "."} for part in path.split("/")):
        raise AnalyzerError("FILE_REQUEST_REJECTED", "Path traversal is forbidden")
    if path.endswith("/") or "//" in path or ":" in path:
        raise AnalyzerError("FILE_REQUEST_REJECTED", "Invalid relative file path")
    return pure.as_posix()


def is_env_example(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return bool(
        name in {".env.example", ".env.sample", ".env.template", "env.example.txt", "env.sample.txt"}
        or name.startswith(".env.")
        and name.endswith((".example", ".sample", ".template"))
    )


def exclusion_reason(path: str, patterns: tuple[str, ...] = ()) -> str | None:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if any(
        fnmatch.fnmatchcase(path, pattern)
        or path == pattern.rstrip("/")
        or path.startswith(pattern.rstrip("/") + "/")
        for pattern in patterns
    ):
        return "explicit_exclusion"
    if any(part in EXCLUDED_DIRECTORIES for part in pure.parts):
        return "generated_or_configuration_directory"
    if name.startswith(".env") and not is_env_example(path):
        return "secret_environment_file"
    if (
        name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore"))
        or name
        in {
            "id_rsa",
            "id_dsa",
            "id_ed25519",
            "credentials",
            ".npmrc",
            ".pypirc",
        }
        or name.startswith("credentials.")
    ):
        return "credential_file"
    if name in {"agents.md", "opencode.json", "opencode.jsonc", "opencode.md"}:
        return "repository_agent_configuration"
    if pure.suffix.lower() in BINARY_EXTENSIONS:
        return "binary_asset"
    return None


def file_kind(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    if is_env_example(path):
        return "environment_example"
    if name == "package.json" or name in {"pnpm-workspace.yaml", "lerna.json"}:
        return "manifest"
    if name in {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    }:
        return "lockfile"
    if (
        name.startswith("dockerfile")
        or name in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
        or name.startswith(("compose.", "docker-compose."))
    ):
        return "container"
    if name.startswith(("vite.config.", "tsconfig")) or name.endswith(".conf"):
        return "build_config"
    if PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS:
        return "source"
    if name.startswith("readme"):
        return "documentation"
    return "text"


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    commit: str | None
    files: Mapping[str, bytes]
    manifest: tuple[dict, ...]
    excluded_paths: tuple[str, ...]


_REGISTRY: dict[str, Snapshot] = {}
_REFERENCE_COUNTS: dict[str, int] = {}
_REGISTRY_LOCK = threading.RLock()


def capture(repo: str | Path, limits: Limits, excluded_paths: list[str] | None = None) -> Snapshot:
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise AnalyzerError("SOURCE_NOT_FOUND", "Repository directory does not exist")
    patterns = tuple(sorted(set(excluded_paths or [])))
    entries: list[dict] = []
    content: dict[str, bytes] = {}

    def walk(directory_fd: int, prefix: str = "") -> None:
        try:
            children = sorted(os.scandir(directory_fd), key=lambda child: child.name)
        except OSError as exc:
            raise AnalyzerError("SOURCE_READ_FAILED", "Cannot inventory repository") from exc
        for child in children:
            relative = prefix + child.name
            try:
                metadata = child.stat(follow_symlinks=False)
                reason = exclusion_reason(relative, patterns)
                if child.is_symlink():
                    reason = "symbolic_link"
                elif child.is_dir(follow_symlinks=False):
                    if reason is None:
                        next_fd = os.open(
                            child.name,
                            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=directory_fd,
                        )
                        try:
                            walk(next_fd, relative + "/")
                        finally:
                            os.close(next_fd)
                    # Pruned directories need no individual child inventory.
                    continue
                elif not child.is_file(follow_symlinks=False):
                    reason = "not_regular_file"
                if reason is None and metadata.st_size > limits.max_file_bytes:
                    reason = "file_size_limit"
                raw: bytes | None = None
                if reason is None:
                    # O_NOFOLLOW closes the stat/open race for leaf symlinks.
                    fd = os.open(child.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                    try:
                        with os.fdopen(fd, "rb") as stream:
                            raw = stream.read(limits.max_file_bytes + 1)
                    except Exception:
                        # fdopen owns and closes fd after successful entry.
                        raise
                    if len(raw) > limits.max_file_bytes:
                        reason = "file_size_limit"
                    elif b"\x00" in raw:
                        reason = "binary_content"
                    else:
                        try:
                            raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            reason = "non_utf8_content"
                if reason is None and raw is not None:
                    content[relative] = raw
                entries.append(
                    {
                        "fileId": "f-" + hashlib.sha256(relative.encode()).hexdigest()[:16],
                        "path": relative,
                        "size": metadata.st_size if raw is None else len(raw),
                        "digest": hashlib.sha256(raw).hexdigest()
                        if reason is None and raw is not None
                        else None,
                        "kind": file_kind(relative),
                        "eligible": reason is None,
                        "exclusionReason": reason,
                    }
                )
            except OSError as exc:
                raise AnalyzerError("SOURCE_READ_FAILED", f"Cannot safely capture {relative}") from exc

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        walk(root_fd)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda entry: entry["path"])
    snapshot_id = digest(
        {"policyVersion": POLICY_VERSION, "excludedPaths": list(patterns), "manifest": entries}
    )
    commit = None
    try:
        completed = subprocess.run(
            ["git", "--no-pager", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        candidate = completed.stdout.strip()
        if (
            completed.returncode == 0
            and len(candidate) in {40, 64}
            and all(c in "0123456789abcdef" for c in candidate)
        ):
            commit = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
    snapshot = Snapshot(snapshot_id, commit, MappingProxyType(content), tuple(entries), patterns)
    with _REGISTRY_LOCK:
        _REGISTRY[snapshot_id] = snapshot
        _REFERENCE_COUNTS[snapshot_id] = _REFERENCE_COUNTS.get(snapshot_id, 0) + 1
    return snapshot


def get_snapshot(snapshot_id: str) -> Snapshot:
    with _REGISTRY_LOCK:
        try:
            return _REGISTRY[snapshot_id]
        except KeyError as exc:
            raise AnalyzerError(
                "SNAPSHOT_UNAVAILABLE", "Snapshot must be prepared in the current process before expansion"
            ) from exc


def release_snapshot(snapshot_id: str) -> None:
    """Release one capture/prepare ownership reference.

    Every successful prepare owns one reference until its caller releases it;
    expansion reuses that reference. Identical concurrent jobs share immutable
    bytes and remain independent owners. Releasing an already absent ID is safe.
    """
    with _REGISTRY_LOCK:
        remaining = _REFERENCE_COUNTS.get(snapshot_id, 0) - 1
        if remaining > 0:
            _REFERENCE_COUNTS[snapshot_id] = remaining
        else:
            _REFERENCE_COUNTS.pop(snapshot_id, None)
            _REGISTRY.pop(snapshot_id, None)
