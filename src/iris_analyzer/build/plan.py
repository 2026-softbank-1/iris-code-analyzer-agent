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
    template: str = "auto"
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
        if self.template not in {"auto", "none", "node_npm", "vite_static"}:
            raise AnalyzerError(
                "BUILD_TEMPLATE_UNSUPPORTED", "No controlled template exists for this profile"
            )
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


def _template(root: Path, profile: str) -> tuple[str | None, str | None, list[str]]:
    package, lockfile = root / "package.json", root / "package-lock.json"
    if not package.is_file() or not lockfile.is_file():
        return (
            None,
            None,
            [
                "Fallback requires package.json and npm package-lock.json; other stacks need an explicit Dockerfile"
            ],
        )
    try:
        pkg = json.loads(package.read_text())
        lock = json.loads(lockfile.read_text())
    except (ValueError, UnicodeError):
        return None, None, ["Package manifests are not valid JSON"]
    if not isinstance(pkg, dict) or not isinstance(lock, dict):
        return None, None, ["Package manifests must be JSON objects"]
    if (
        pkg.get("workspaces")
        or not isinstance(pkg.get("packageManager", "npm"), str)
        or pkg.get("packageManager", "npm").split("@")[0] != "npm"
    ):
        return None, None, ["Workspace/non-npm fallback is unsupported; provide the project's Dockerfile"]
    if lock.get("lockfileVersion") not in {2, 3}:
        return None, None, ["Fallback requires npm lockfile version 2 or 3"]
    scripts = pkg.get("scripts", {})
    if not isinstance(scripts, dict):
        return None, None, ["Package scripts must be an object"]
    if not all(isinstance(pkg.get(key, {}), dict) for key in ("dependencies", "devDependencies")):
        return None, None, ["Package dependencies must be objects"]
    dependencies = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    build = scripts.get("build", "")
    engines = pkg.get("engines", {})
    if not isinstance(engines, dict):
        return None, None, ["Package engines must be an object"]
    declared = [engines.get("node")]
    for path in (root / ".nvmrc", root / ".node-version"):
        if path.is_file():
            declared.append(path.read_text().strip())
    major = None
    exact_version = None
    for version in filter(None, declared):
        match = re.fullmatch(r"v?(22|24)(?:\.\d+){0,2}|(?:>=|\^)(22|24)(?:\.0){0,2}|(22|24)\.x", str(version))
        if not match:
            return None, None, ["Node version constraint needs an explicit compatible Dockerfile"]
        selected = next(group for group in match.groups() if group)
        if major and major != selected:
            return None, None, ["Conflicting Node declarations require a version decision"]
        major = selected
        exact = re.fullmatch(r"v?((?:22|24)(?:\.\d+){1,2})", str(version))
        if exact:
            if exact_version and exact_version != exact[1]:
                return None, None, ["Conflicting exact Node declarations require a version decision"]
            exact_version = exact[1]
    major = exact_version or major or "22"
    common = f"FROM node:{major}-alpine AS build\nWORKDIR /app\nCOPY package.json package-lock.json ./\nRUN npm ci\nCOPY . .\n"
    if profile in {"auto", "vite_static"} and "vite" in dependencies and (root / "index.html").is_file():
        if build not in {"vite build", "tsc && vite build", "tsc -b && vite build"}:
            return None, None, ["Vite fallback only supports the verified default static build scripts"]
        if any(root.glob("vite.config.*")):
            return (
                None,
                None,
                ["Custom Vite configuration needs an explicit Dockerfile or reviewed build profile"],
            )
        content = common + (
            "RUN rm -rf dist && npm run build && test -f dist/index.html\n"
            "FROM nginx:alpine AS runtime\n"
            "COPY --from=build /app/dist /usr/share/nginx/html\n"
            "RUN printf '%s\\n' 'pid /tmp/nginx.pid;' 'events {}' 'http { include /etc/nginx/mime.types; access_log /dev/stdout; error_log /dev/stderr; client_body_temp_path /tmp/client_temp; proxy_temp_path /tmp/proxy_temp; fastcgi_temp_path /tmp/fastcgi_temp; uwsgi_temp_path /tmp/uwsgi_temp; scgi_temp_path /tmp/scgi_temp; server { listen 8080; server_name _; root /usr/share/nginx/html; index index.html; location = /healthz { return 200; } location / { try_files $uri $uri/ /index.html; } } }' > /etc/nginx/nginx.conf\n"
            'USER 101:101\nENTRYPOINT ["nginx"]\n'
            'EXPOSE 8080\nCMD ["-g", "daemon off;"]\n'
        )
        return "vite-static-npm.v1", content, []
    if profile in {"auto", "node_npm"}:
        start = scripts.get("start", "")
        match = re.fullmatch(r"node ([A-Za-z0-9_./-]+\.(?:js|mjs|cjs))", start)
        if match and not build:
            entry = safe_relative(match[1])
            if (root / entry).is_file():
                content = (
                    f"FROM node:{major}-alpine AS runtime\nWORKDIR /app\n"
                    "COPY package.json package-lock.json ./\nRUN npm ci --omit=dev\n"
                    "COPY --chown=node:node . .\nENV NODE_ENV=production\nUSER node\n"
                    f"CMD {json.dumps(['node', entry])}\n"
                )
                return "node-npm-start.v1", content, []
    return None, None, ["No verified controlled fallback profile; supply a Dockerfile and build context"]


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
    unresolved, dockerfile, template_id, content = [], None, None, None
    selection = "source"
    candidates = sorted(path.relative_to(source_root).as_posix() for path in source_root.rglob("Dockerfile*"))
    chosen = request.dockerfile
    if chosen is None and (context / "Dockerfile").is_file():
        chosen = (context / "Dockerfile").relative_to(source_root).as_posix()
    if chosen:
        dockerfile = source_root / chosen
        if (
            not dockerfile.is_file()
            or dockerfile.is_symlink()
            or not dockerfile.resolve().is_relative_to(source_root)
        ):
            raise AnalyzerError("BUILD_DOCKERFILE_INVALID", "Selected Dockerfile is not a staged source file")
    elif candidates:
        unresolved.append(
            "Dockerfile candidates exist but the build context/selected Dockerfile is ambiguous"
        )
    elif request.template == "none":
        unresolved.append("No Dockerfile found and template fallback is disabled")
    else:
        template_id, content, reasons = _template(context, request.template)
        unresolved.extend(reasons)
        if content:
            selection = "controlled_template"
            directory = output_root / "generated"
            directory.mkdir(exist_ok=True)
            dockerfile = directory / "Dockerfile"
            dockerfile.write_text(content)
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
        "schemaVersion": "iris.build-plan.v1",
        "status": "needs_input" if unresolved else "ready",
        "executionAuthorized": False,
        "source": {key: value for key, value in manifest.items() if key != "files"},
        "analysisSourceSnapshotId": request.source_snapshot_id,
        "environment": {
            "kind": request.environment,
            "platform": request.platform,
            "supportedByLocalRunner": request.environment == "local_docker",
            "cloudFallback": False,
        },
        "build": {
            "contextPath": request.context,
            "dockerfilePath": chosen,
            "dockerfileOrigin": selection,
            "dockerfileSha256": hashlib.sha256(dockerfile.read_bytes()).hexdigest() if dockerfile else None,
            "dockerfileCandidates": candidates,
            "dockerignorePath": ignore_path,
            "dockerignoreSemantics": "Docker applies the unchanged effective ignore file after explicit credential/host exclusions",
            "target": request.target,
            "templateId": template_id,
            "templatePolicy": {
                "nodeImage": next(
                    (line.split()[1] for line in content.splitlines() if line.startswith("FROM node:")), None
                ),
                "staticImage": "nginx:alpine",
                "basis": "controlled_template_policy",
                "baseImagesPinned": False,
            }
            if template_id
            else None,
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
