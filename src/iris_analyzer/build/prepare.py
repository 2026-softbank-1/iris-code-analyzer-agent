"""Worker boundary: analyze fixed source, recommend service-owned builders, package bytes.

Analysis snapshots deliberately omit binary assets and redact evidence. Build
archives use a separate byte-preserving manifest, never the redacted context.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

from ..contracts import AnalyzerError, digest
from ..deployment.dossier import prepare_readiness
from ..pipeline import ModelRunner, analyze_with_report
from .plan import BuildRequest, prepare_build
from .source import safe_relative, stage_local_source, verify_source

REQUEST_VERSION = "iris.build-preparation-request.v2"
RESULT_VERSION = "iris.build-preparation.v2"


def _request(document: dict) -> dict:
    allowed = {
        "schemaVersion",
        "sourceRoot",
        "outputDirectory",
        "rootDirectory",
        "dockerfilePath",
        "sourceSha",
        "platform",
        "builder",
    }
    if not isinstance(document, dict) or set(document) - allowed:
        raise AnalyzerError("BUILD_REQUEST_INVALID", "Unknown build preparation fields")
    if document.get("schemaVersion") != REQUEST_VERSION:
        raise AnalyzerError("BUILD_REQUEST_INVALID", "Unsupported preparation contract version")
    for key in ("sourceRoot", "outputDirectory"):
        value = document.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise AnalyzerError("BUILD_REQUEST_INVALID", "Worker directories must be absolute paths")
    sha = document.get("sourceSha")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise AnalyzerError("BUILD_REQUEST_INVALID", "Worker must supply its pinned 40-character commit SHA")
    builder = document.get("builder", "auto")
    if not isinstance(builder, str) or builder not in {"auto", "dockerfile", "railpack"}:
        raise AnalyzerError("BUILD_REQUEST_INVALID", "Builder must be auto, dockerfile, or railpack")
    platform = document.get("platform", "linux/amd64")
    if not isinstance(platform, str) or platform not in {"linux/amd64", "linux/arm64"}:
        raise AnalyzerError("BUILD_PLATFORM_UNSUPPORTED", "Only Linux amd64/arm64 platforms are supported")
    result = dict(document)
    result["rootDirectory"] = safe_relative(document.get("rootDirectory", "."), allow_dot=True)
    path = document.get("dockerfilePath")
    if path is not None and (not isinstance(path, str) or not path):
        raise AnalyzerError(
            "BUILD_REQUEST_INVALID", "Dockerfile path must be a nonempty relative path or null"
        )
    result["dockerfilePath"] = safe_relative(path) if path is not None else None
    result["builder"] = document.get("builder", "auto")
    return result


def _archive(source: Path, manifest: dict, output: Path) -> str:
    """Deterministic tar with a common root, compatible with CodeBuild strip-components=1."""
    verify_source(source, manifest)
    entries = {row["path"]: row for row in manifest["files"]}
    with output.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for name, row in sorted(entries.items()):
                data = (source / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise AnalyzerError("BUILD_SOURCE_CHANGED", "Source changed during archive assembly")
                info = tarfile.TarInfo("source/" + name)
                info.size, info.mtime = len(data), 0
                info.mode = 0o755 if row["executable"] else 0o644
                info.uid = info.gid = 0
                archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def prepare_source_build(document: dict, *, runner: ModelRunner | None = None) -> dict:
    """Prepare an isolated build artifact; caller attests the source's pinned Git SHA.

    A supplied model runner uses the existing schema/evidence/budget pipeline.
    Without one the mode is explicitly static. Ready attests source archive
    preparation, never Railpack support, an image build, or execution approval.
    """
    request = _request(document)
    source = Path(request["sourceRoot"]).resolve(strict=True)
    output = Path(request["outputDirectory"]).resolve()
    if output == source or source in output.parents:
        raise AnalyzerError("BUILD_OUTPUT_INVALID", "Worker output must be outside input source")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    staged = output / "source"
    manifest = stage_local_source(source, staged)
    manifest["origin"] = {
        "kind": "worker_pinned_commit",
        "revision": request["sourceSha"],
        "uploadId": None,
        "verification": "caller_attested_commit; content independently hashed by build manifest",
    }
    run = analyze_with_report(staged, runner=runner, out=output / "analysis")
    readiness = prepare_readiness(staged, analysis=run.result, out=output / "readiness")
    # Analysis and readiness must not alter the original build bytes.
    verify_source(staged, manifest)
    context = request["rootDirectory"]
    selected = request["dockerfilePath"]
    full_path = (PurePosixPath(context) / selected).as_posix() if selected else None
    config = BuildRequest(
        context=context,
        dockerfile=full_path,
        platform=request.get("platform", "linux/amd64"),
        builder=request["builder"],
        source_snapshot_id=run.result["sourceSnapshotId"],
    )
    prepared = prepare_build(staged, output / "plan", manifest, config)
    plan = prepared.plan
    relative = prepared.dockerfile.relative_to(staged / context).as_posix() if prepared.dockerfile else None
    archive = output / "source.tar.gz"
    archive_digest = _archive(staged, manifest, archive) if plan["status"] == "ready" else None
    evidence = [
        {"path": row["path"], "sha256": row["sha256"]}
        for row in manifest["files"]
        if row["path"]
        in {
            (PurePosixPath(context) / name).as_posix()
            for name in (
                "package.json",
                "package-lock.json",
                ".nvmrc",
                ".node-version",
                relative or "Dockerfile",
            )
        }
    ]
    result = {
        "schemaVersion": RESULT_VERSION,
        "status": plan["status"],
        "builder": plan["buildHandoff"]["recommendedBuilder"],
        "buildHandoff": plan["buildHandoff"],
        "rootDirectory": context,
        "platform": config.platform,
        "dockerfilePath": relative,
        "dockerfileOrigin": plan["build"]["dockerfileOrigin"],
        "dockerfileSha256": plan["build"]["dockerfileSha256"],
        "templateId": plan["build"]["templateId"],
        "sourceSha": request["sourceSha"],
        "sourceManifestSha256": manifest["sourceManifestSha256"],
        "analysisSourceSnapshotId": run.result["sourceSnapshotId"],
        "analysisContextHash": run.result["contextHash"],
        "analysisMode": run.report["mode"],
        "sourceArchive": {"path": str(archive), "sha256": archive_digest, "format": "tar.gz"}
        if archive_digest
        else None,
        "planDigest": plan["planDigest"],
        "analysisResult": run.result,
        "sourceReadiness": readiness,
        "evidence": evidence,
        "unresolvedInputs": plan["unresolvedInputs"],
        "executionAuthorized": False,
    }
    result["preparationDigest"] = digest(result)
    (output / "preparation-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result
