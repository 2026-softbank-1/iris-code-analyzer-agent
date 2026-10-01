"""Controlled Dockerfile selection and portable, non-executing build plans."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..contracts import AnalyzerError, digest
from .source import safe_relative, verify_source


@dataclass(frozen=True)
class EcrTarget:
    account_id: str
    region: str
    repository: str

    def __post_init__(self):
        if not re.fullmatch(r"\d{12}", self.account_id):
            raise AnalyzerError("BUILD_ECR_TARGET_INVALID", "ECR requires an exact 12-digit AWS account")
        if not re.fullmatch(r"(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:[a-z]+-)?[a-z]+-\d", self.region):
            raise AnalyzerError("BUILD_ECR_TARGET_INVALID", "ECR requires an explicit supported AWS region")
        if len(self.repository) > 256 or not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", self.repository):
            raise AnalyzerError("BUILD_ECR_TARGET_INVALID", "ECR requires an exact repository name")

    @property
    def registry(self) -> str:
        return f"{self.account_id}.dkr.ecr.{self.region}.amazonaws.com"

    @property
    def uri(self) -> str:
        return f"{self.registry}/{self.repository}"


@dataclass(frozen=True)
class BuildRequest:
    environment: str = "local_docker"
    context: str = "."
    dockerfile: str | None = None
    target: str | None = None
    platform: str = "linux/amd64"
    builder: str = "auto"
    timeout_seconds: int = 900
    image_repository: str = "iris-build"
    ecr_target: EcrTarget | None = None
    source_snapshot_id: str | None = None

    def __post_init__(self):
        if self.environment not in {"local_docker", "aws_codebuild"}:
            raise AnalyzerError("BUILD_ENVIRONMENT_INVALID", "Use local_docker or aws_codebuild explicitly")
        safe_relative(self.context, allow_dot=True)
        if self.dockerfile is not None:
            safe_relative(self.dockerfile)
        if self.target is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", self.target):
            raise AnalyzerError("BUILD_TARGET_INVALID", "Invalid Dockerfile stage target")
        if self.platform not in {"linux/amd64", "linux/arm64"}:
            raise AnalyzerError(
                "BUILD_PLATFORM_UNSUPPORTED", "Only single-platform Linux amd64/arm64 builds are supported"
            )
        if self.builder not in {"auto", "dockerfile", "railpack"}:
            raise AnalyzerError("BUILD_BUILDER_UNSUPPORTED", "Use auto, dockerfile, or railpack")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 3600:
            raise AnalyzerError("BUILD_TIMEOUT_INVALID", "Build timeout must be between 1 and 3600 seconds")
        if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", self.image_repository):
            raise AnalyzerError(
                "BUILD_IMAGE_INVALID", "Use a local image repository without a tag or registry port"
            )


@dataclass
class PreparedBuild:
    source_root: Path
    output_root: Path
    plan: dict
    manifest: dict
    dockerfile: Path | None


def prepare_build(
    source_root: Path, output_root: Path, manifest: dict, request: BuildRequest | None = None
) -> PreparedBuild:
    request = request or BuildRequest()
    if source_root.is_symlink() or output_root.is_symlink():
        raise AnalyzerError("BUILD_PATH_INVALID", "Source and output roots cannot be symbolic links")
    source_root, output_root = source_root.resolve(), output_root.resolve()
    verify_source(source_root, manifest)
    if source_root == output_root or source_root in output_root.parents:
        raise AnalyzerError("BUILD_OUTPUT_INVALID", "Build outputs must not be inside source input")
    output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    context = source_root / request.context
    if not context.is_dir() or context.is_symlink() or not context.resolve().is_relative_to(source_root):
        raise AnalyzerError("BUILD_CONTEXT_INVALID", "Build context does not exist inside staged source")
    unresolved, dockerfile = [], None
    # Only candidates in the selected service root are relevant. A Dockerfile
    # elsewhere in a monorepo must not prevent that service's Railpack handoff.
    candidates = sorted(
        path.relative_to(source_root).as_posix()
        for path in context.rglob("Dockerfile*")
        if path.is_file() and not path.name.endswith(".dockerignore")
    )
    chosen = request.dockerfile
    if chosen is not None:
        selected_file = source_root / chosen
        if (
            not selected_file.is_file()
            or selected_file.is_symlink()
            or not selected_file.resolve().is_relative_to(context.resolve())
        ):
            raise AnalyzerError(
                "BUILD_DOCKERFILE_INVALID", "Selected Dockerfile is not a staged context file"
            )
    if chosen is None and (context / "Dockerfile").is_file():
        chosen = (context / "Dockerfile").relative_to(source_root).as_posix()
    if request.builder == "railpack":
        builder, reason = "railpack", "explicit_railpack"
        chosen = None  # Detection never overrides an explicit service choice.
    elif chosen:
        builder, reason = "dockerfile", "source_dockerfile"
        dockerfile = source_root / chosen
    elif request.builder == "dockerfile":
        builder, reason = "dockerfile", "explicit_dockerfile_missing"
        unresolved.append(
            "Explicit Dockerfile builder requires a source Dockerfile or a service configuration decision"
        )
    elif candidates:
        builder, reason = "dockerfile", "dockerfile_selection_required"
        unresolved.append(
            "Dockerfile candidates require an explicit path or a service decision to use Railpack"
        )
    else:
        builder, reason = "railpack", "dockerfile_absent"
    handoff = {
        "owner": "service",
        "recommendedBuilder": builder,
        "requestedBuilder": None if request.builder == "auto" else request.builder,
        "decisionRequired": request.builder == "auto" or bool(unresolved),
        "reasonCode": reason,
    }
    origin = manifest["origin"]
    tag = origin.get("revision") or origin.get("uploadId") or manifest["sourceManifestSha256"][:40]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise AnalyzerError(
            "BUILD_SOURCE_ID_INVALID", "Source identity cannot be used as an immutable build tag"
        )
    image_repository = request.ecr_target.uri if request.ecr_target else request.image_repository
    ignored = context / ".dockerignore"
    specific_ignore = Path(str(dockerfile) + ".dockerignore") if dockerfile else None
    effective_ignore = specific_ignore if specific_ignore and specific_ignore.is_file() else ignored
    ignore_path = effective_ignore.relative_to(source_root).as_posix() if effective_ignore.is_file() else None
    plan = {
        "schemaVersion": "iris.build-plan.v2",
        "buildHandoff": handoff,
        "status": "needs_input" if unresolved else "ready",
        "executionAuthorized": False,
        "source": {key: value for key, value in manifest.items() if key != "files"},
        "analysisSourceSnapshotId": request.source_snapshot_id,
        "environment": {
            "kind": request.environment,
            "platform": request.platform,
            "supportedByLocalRunner": False,
            "cloudFallback": False,
        },
        "build": {
            "contextPath": request.context,
            "dockerfilePath": chosen,
            "dockerfileOrigin": "source" if dockerfile else None,
            "dockerfileSha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest() if dockerfile else None,
            "dockerfileCandidates": candidates,
            "dockerignorePath": ignore_path,
            "dockerignoreSemantics": (
                "Docker applies the unchanged effective ignore file after explicit credential/host exclusions"
                if builder == "dockerfile"
                else "Detected source ignore file only; the service builder determines Railpack ignore behavior"
            ),
            "target": request.target,
            "templateId": None,
            "templatePolicy": None,
            "buildArguments": [],
            "secretMounts": [],
            "timeoutSeconds": request.timeout_seconds,
        },
        "image": {
            "tag": tag,
            "tagBasis": "commit_sha"
            if origin.get("revision")
            else "upload_id"
            if origin.get("uploadId")
            else "source_manifest",
            "reference": f"{image_repository}:{tag}",
            "platform": request.platform,
        },
        "registry": asdict(request.ecr_target) if request.ecr_target else None,
        "unresolvedInputs": unresolved,
        "limitations": [
            "Ready means original source archive preparation only, not Railpack detection, build success, or execution authorization.",
            "The service owner selects and runs Dockerfile/Railpack builders; this analyzer does not create Dockerfiles or execute source.",
            "A successful image build does not validate application correctness or production capacity.",
            "Local Docker is for explicitly trusted source; a shared Docker daemon is not a hostile-code tenant isolation boundary.",
            "Credential/host files are excluded before Docker applies dockerignore. Exclusions may make builds that depend on them fail.",
            "Mutable base-image tags and dependency install scripts prevent a claim of byte-for-byte reproducible images.",
            "aws_codebuild requires the dedicated control-plane adapter and pre-provisioned worker; there is no implicit cloud execution.",
        ],
    }
    verify_source(source_root, manifest)
    plan["planDigest"] = digest(plan)
    (output_root / "source-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (output_root / "build-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return PreparedBuild(source_root, output_root, plan, manifest, dockerfile)
