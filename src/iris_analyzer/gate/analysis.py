"""Analysis gate: triage a pinned checkout and extract deployment units statically.

Contract ``iris.analysis-gate.v1``. Deterministic, offline and model-free: the
gate decides whether the existing single-service build path (Dockerfile or
Railpack) applies unchanged (``skip``) or whether the repository holds several
deployable images and needs unit review (``analyze``).
"""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from ..contracts import AnalyzerError, _load_schema
from .compose import (
    ComposeService,
    compose_order,
    database_engine_hint,
    image_engine,
    is_adjunct,
    load_compose,
)
from .initdb import describe_scripts, mounted_files
from .links import (
    DEFAULT_PORTS,
    DEFAULT_USERS,
    MAX_EVIDENCE,
    config_aliases,
    dependency_profile,
    env_references,
    heuristic_binding,
    key_binding,
    parse_url,
    sibling_bindings,
    source_aliases,
    whole_url_host,
)
from .scan import RepositoryScan, join, parent, relative_to, scan_repository, within
from .sources import (
    RUNTIME_MANIFESTS,
    WEAK_MANIFESTS,
    Dockerfile,
    command_port,
    dockerfile_copies,
    dockerfile_variant,
    env_example_keys,
    inferred_dependencies,
    is_compose_file,
    is_dev_dockerfile,
    is_dockerfile,
    load_package,
    node_is_app,
    node_role,
    parse_dockerfile,
    procfile_processes,
    python_role,
    script_port,
    source_port,
    workspace_members,
    workspace_patterns,
)

REQUEST_VERSION = "iris.analysis-gate-request.v1"
RESULT_VERSION = "iris.analysis-gate.v1"
REQUEST_SCHEMA = _load_schema("analysis-gate-request.schema.json")
RESULT_SCHEMA = _load_schema("analysis-gate.schema.json")
DATABASE_ENGINES = {"postgres", "redis", "mysql", "mongodb"}
_SHA = re.compile(r"[0-9a-f]{40}")
_IGNORED_COMPOSE_VARIANTS = {"test", "tests", "ci", "e2e"}
_WORKER_NAME = re.compile(r"(worker|queue|consumer|job|cron|scheduler|processor|bot)", re.I)
_WEB_NAME = re.compile(r"(^|[-_])(web|frontend|front|client|ui|site|www|app-web)($|[-_])", re.I)
_API_NAME = re.compile(r"(^|[-_])(api|server|backend|gateway|svc|service)($|[-_])", re.I)
_STATIC_IMAGES = re.compile(r"(^|/)(nginx|caddy|httpd|nginxinc/nginx-unprivileged)$")


@dataclass(frozen=True)
class GateRequest:
    source_root: Path
    root_directory: str
    source_sha: str | None
    mode: str
    ai: bool


@dataclass
class _Unit:
    id: str
    root: str
    builder: str
    dockerfile: str | None = None
    port: int | None = None
    start_command: str | None = None
    build_command: str | None = None
    role: str = "app"
    env: list[dict] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    docker: Dockerfile | None = None
    compose: ComposeService | None = None
    aliases: list[dict] = field(default_factory=list)

    def contract(self) -> dict:
        return {
            "id": self.id,
            "name": self.id,
            "rootDirectory": self.root,
            "builder": self.builder,
            "dockerfilePath": self.dockerfile if self.builder == "dockerfile" else None,
            "port": self.port,
            "startCommand": self.start_command,
            "buildCommand": self.build_command,
            "role": self.role,
            "public": self.role != "worker",
            "env": _unique_env(self.env),
            "dependsOn": sorted(set(self.depends_on)),
            "hostAliases": self.aliases,
            "evidence": _unique_evidence(self.evidence),
        }


def _error(message: str, code: str = "GATE_REQUEST_INVALID") -> AnalyzerError:
    return AnalyzerError(code, message)


def parse_request(document: object) -> GateRequest:
    """Strict request validation; errors never echo the supplied paths or values."""
    if not isinstance(document, dict):
        raise _error("Request must be a JSON object")
    error = next(iter(Draft202012Validator(REQUEST_SCHEMA).iter_errors(document)), None)
    if error is not None:
        field_name = ".".join(str(item) for item in error.absolute_path) or "request"
        raise _error(f"Request does not match {REQUEST_VERSION} ({field_name})")
    source = document["sourceRoot"]
    if "\x00" in source or not os.path.isabs(source):
        raise _error("sourceRoot must be an absolute path")
    source_root = Path(source)
    try:
        info = os.stat(source_root)
    except OSError:
        raise _error("sourceRoot does not exist", "GATE_SOURCE_NOT_FOUND") from None
    if not stat.S_ISDIR(info.st_mode):
        raise _error("sourceRoot must be a directory", "GATE_SOURCE_NOT_FOUND")
    raw_root = document.get("rootDirectory", ".")
    parts = PurePosixPath(raw_root).parts
    if (
        not raw_root
        or "\\" in raw_root
        or any(ord(character) < 32 for character in raw_root)
        or raw_root.startswith("/")
        or ".." in parts
    ):
        raise _error("rootDirectory must be a relative path inside sourceRoot")
    root_directory = PurePosixPath(raw_root).as_posix()
    root_directory = "." if root_directory in ("", ".") else root_directory.removeprefix("./")
    current = source_root
    for part in [] if root_directory == "." else root_directory.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            raise _error("rootDirectory does not exist", "GATE_ROOT_DIRECTORY_NOT_FOUND") from None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise _error("rootDirectory must be a real directory", "GATE_ROOT_DIRECTORY_NOT_FOUND")
    sha = document.get("sourceSha")
    if sha is not None and not _SHA.fullmatch(sha):
        raise _error("sourceSha must be 40 lowercase hex characters or null")
    return GateRequest(
        source_root=source_root,
        root_directory=root_directory,
        source_sha=sha,
        mode=document.get("mode", "auto"),
        ai=bool(document.get("ai", False)),
    )


def validate_gate_result(result: dict) -> dict:
    error = next(iter(Draft202012Validator(RESULT_SCHEMA).iter_errors(result)), None)
    if error is not None:
        raise AnalyzerError(
            "GATE_RESULT_INVALID",
            "Gate result does not match the versioned contract",
            {"path": list(error.absolute_path), "reason": error.message},
        )
    return result


def run_gate(document: object) -> dict:
    request = parse_request(document)
    started = time.monotonic()
    result = _Gate(request, scan_repository(request.source_root, request.root_directory)).run()
    result["analysis"]["durationMs"] = int((time.monotonic() - started) * 1000)
    return validate_gate_result(result)


def _slug(value: str, fallback: str = "app") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower().split("/")[-1]).strip("-")[:40].strip("-")
    return text or fallback


def _unique_env(rows: list[dict]) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["key"], row["stage"])
        if key in seen:
            seen[key]["required"] = seen[key]["required"] or row["required"]
            seen[key]["binding"] = seen[key]["binding"] or row.get("binding")
        else:
            seen[key] = {
                "key": row["key"],
                "stage": row["stage"],
                "required": bool(row["required"]),
                "binding": row.get("binding"),
            }
    return [seen[key] for key in sorted(seen, key=lambda item: (item[1] != "runtime", item[0]))]


def _unique_evidence(rows: list[dict]) -> list[dict]:
    unique = {(row["path"], row["line"]) for row in rows}
    return [{"path": path, "line": line} for path, line in sorted(unique)]


def _reason(code: str, message: str, paths: list[str] | None = None) -> dict:
    return {"code": code, "message": message, "paths": sorted(set(paths or []))[:50]}


def _question(code: str, message: str, unit_id: str | None = None) -> dict:
    return {"code": code, "unitId": unit_id, "message": message}


class _Gate:
    def __init__(self, request: GateRequest, scan: RepositoryScan) -> None:
        self.request = request
        self.scan = scan
        self.scope = request.root_directory
        self.questions: list[dict] = []
        self.dependencies: dict[str, dict] = {}
        self.units: list[_Unit] = []
        self.ids: set[str] = set()
        self.dockerfiles: dict[str, Dockerfile] = {}
        self.service_ids: dict[str, str] = {}
        self.init_files: dict[str, dict[str, str]] = {}

    # -- inventory -------------------------------------------------------
    def _inventory(self) -> None:
        scan = self.scan
        files = list(scan.files())
        self.all_dockerfiles = [path for path in files if is_dockerfile(PurePosixPath(path).name)]
        self.docker_candidates = [
            path for path in self.all_dockerfiles if not is_dev_dockerfile(PurePosixPath(path).name)
        ]
        compose_files = [
            path
            for path in files
            if is_compose_file(PurePosixPath(path).name)
            and not _compose_variant_ignored(PurePosixPath(path).name)
        ]
        self.compose_files = compose_order(scan, compose_files)
        self.services: dict[str, ComposeService] = {}
        for path in self.compose_files:
            services, problem = load_compose(scan, path)
            if problem:
                self.questions.append(
                    _question("compose_invalid", f"{path} Compose 파일을 해석할 수 없어 건너뛰었습니다.")
                )
            for service in services:
                existing = self.services.get(service.name)
                if existing is None:
                    self.services[service.name] = service
                elif service.build and (existing.root, existing.dockerfile) != (
                    service.root,
                    service.dockerfile,
                ):
                    self.questions.append(
                        _question(
                            "compose_variant",
                            f"Compose 서비스 '{service.name}'가 {existing.file}와 {service.file}에서 다르게 "
                            f"정의되어 {existing.file} 정의를 사용했습니다.",
                        )
                    )
        self.build_services = [service for service in self.services.values() if service.build]
        self.image_services = [service for service in self.services.values() if not service.build]

        self.manifests: list[dict] = []
        self.manifest_dirs: dict[str, list[str]] = {}
        for directory, names in sorted(scan.directories.items()):
            for name in names:
                runtime = RUNTIME_MANIFESTS.get(name)
                if runtime is None and directory == self.scope:
                    runtime = WEAK_MANIFESTS.get(name)
                if runtime is None:
                    continue
                self.manifests.append({"path": join(directory, name), "runtime": runtime})
                self.manifest_dirs.setdefault(directory, []).append(name)

        self.workspace_manifests: list[str] = []
        self.workspace_roots: set[str] = set()
        members: set[str] = set()
        for directory in sorted(scan.directories):
            patterns, declared = workspace_patterns(scan, directory)
            self.workspace_manifests.extend(declared)
            if patterns:
                self.workspace_roots.add(directory)
                members.update(workspace_members(scan, directory, patterns))
        self.workspace_members = sorted(members)
        self.workspace_apps = [
            directory
            for directory in self.workspace_members
            if node_is_app(scan, directory, load_package(scan, join(directory, "package.json")) or {})
        ]
        self.procfile = procfile_processes(scan, self.scope)

    def _app_manifest_dirs(self) -> list[str]:
        """Directories holding a runnable runtime manifest, outside workspace membership."""
        result = []
        for directory, names in sorted(self.manifest_dirs.items()):
            if any(within(directory, member) for member in self.workspace_members):
                continue
            strong = [name for name in names if name in RUNTIME_MANIFESTS]
            if not strong:
                if directory == self.scope:
                    result.append(directory)
                continue
            if strong == ["package.json"]:
                package = load_package(self.scan, join(directory, "package.json")) or {}
                if directory in self.workspace_roots and not isinstance(
                    (package.get("scripts") or {}).get("start"), str
                ):
                    continue
                if directory != self.scope and not node_is_app(self.scan, directory, package):
                    continue
            result.append(directory)
        return result

    def _docker(self, path: str) -> Dockerfile:
        if path not in self.dockerfiles:
            self.dockerfiles[path] = parse_dockerfile(self.scan, path)
        return self.dockerfiles[path]

    def _covered(self, directory: str, roots: list[tuple[str, Dockerfile | None]]) -> bool:
        for root, docker in roots:
            if directory == root:
                return True
            if docker is not None and within(directory, root):
                relative = relative_to(directory, root)
                if dockerfile_copies(docker, relative) or any(
                    source in (".", "./") for source in docker.copy_sources
                ):
                    return True
        return False

    # -- triage -----------------------------------------------------------
    def _complex_reasons(self, uncovered_dirs: list[str]) -> list[dict]:
        reasons = []
        if len(self.docker_candidates) >= 2:
            reasons.append(
                _reason(
                    "multiple_dockerfiles",
                    f"빌드 대상 Dockerfile {len(self.docker_candidates)}개",
                    self.docker_candidates,
                )
            )
        if len(self.build_services) >= 2:
            reasons.append(
                _reason(
                    "compose_multi_build",
                    f"build가 있는 Compose 서비스 {len(self.build_services)}개: "
                    + ", ".join(service.name for service in self.build_services),
                    [service.file for service in self.build_services],
                )
            )
        if len(self.workspace_apps) >= 2:
            reasons.append(
                _reason(
                    "workspace_multi_app",
                    f"워크스페이스에 실행 가능한 앱 {len(self.workspace_apps)}개",
                    [join(item, "package.json") for item in self.workspace_apps],
                )
            )
        if len(uncovered_dirs) >= 2:
            reasons.append(
                _reason(
                    "multi_language_roots",
                    f"서로 다른 디렉터리에 런타임 매니페스트 {len(uncovered_dirs)}곳",
                    [
                        join(directory, name)
                        for directory in uncovered_dirs
                        for name in self.manifest_dirs.get(directory, [])
                    ],
                )
            )
        if len(self.procfile) >= 2:
            reasons.append(
                _reason(
                    "procfile_multi_process",
                    f"Procfile 프로세스 {len(self.procfile)}종: "
                    + ", ".join(row[0] for row in self.procfile),
                    [join(self.scope, "Procfile")],
                )
            )
        return reasons

    def run(self) -> dict:
        self._inventory()
        self._extract_units()
        docker_roots = [(unit.root, unit.docker) for unit in self.units if unit.builder == "dockerfile"]
        uncovered = [item for item in self._app_manifest_dirs() if not self._covered(item, docker_roots)]
        reasons = self._complex_reasons(uncovered)
        db_images = [
            service.name for service in self.image_services if image_engine(service.image) is not None
        ]
        simple_build = None
        if reasons:
            complexity = "complex"
        elif not self.units:
            complexity = "unsupported"
            reasons.append(
                _reason("no_builder_signal", "Dockerfile도 Railpack이 인식하는 런타임 매니페스트도 없음")
            )
            self.questions.append(
                _question(
                    "no_builder_signal",
                    "빌드 방법을 찾지 못했습니다. 루트 디렉터리를 확인하거나 Dockerfile을 추가하세요.",
                )
            )
        elif (
            len(self.units) == 1
            and self.units[0].root == self.scope
            and (self.units[0].builder == "railpack" or self.units[0].docker is not None)
        ):
            complexity = "simple"
            unit = self.units[0]
            if unit.builder == "dockerfile":
                path = join(unit.root, unit.dockerfile or "Dockerfile")
                ignored = sorted(set(self.all_dockerfiles) - set(self.docker_candidates))
                message = "Dockerfile 1개" + (f" (개발용 변형 {len(ignored)}개 제외)" if ignored else "")
                reasons.append(_reason("single_dockerfile", message, [path]))
                simple_build = {"builder": "dockerfile", "dockerfilePath": unit.dockerfile}
            else:
                runtimes = sorted(
                    {row["runtime"] for row in self.manifests if parent(row["path"]) == self.scope}
                )
                reasons.append(
                    _reason(
                        "single_railpack_app",
                        "Dockerfile 없음 · Railpack이 빌드할 앱 1개 (" + ", ".join(runtimes) + ")",
                        [join(self.scope, name) for name in self.manifest_dirs.get(self.scope, [])],
                    )
                )
                simple_build = {"builder": "railpack", "dockerfilePath": None}
        else:
            complexity = "complex"
            if len(self.units) == 1 and self.units[0].root == self.scope:
                reasons.append(
                    _reason(
                        "dockerfile_missing",
                        "Compose가 가리키는 Dockerfile이 없음",
                        [join(self.scope, self.units[0].dockerfile or "Dockerfile")],
                    )
                )
            elif len(self.units) == 1:
                reasons.append(
                    _reason(
                        "single_unit_in_subdirectory",
                        f"배포 단위가 요청 루트가 아닌 '{self.units[0].root}'에 있음",
                        [self.units[0].root],
                    )
                )
            else:
                reasons.append(
                    _reason(
                        "multiple_units",
                        f"배포 단위 후보 {len(self.units)}개: " + ", ".join(unit.id for unit in self.units),
                        [unit.root for unit in self.units],
                    )
                )
        if db_images:
            reasons.append(
                _reason(
                    "has_image_dependencies",
                    "이미지 전용 Compose 서비스(빌드 대상 아님): " + ", ".join(db_images),
                    [service.file for service in self.image_services if service.name in db_images],
                )
            )
        decision = "skip" if complexity == "simple" else "analyze"
        if self.request.mode == "force":
            decision = "analyze"
            simple_build = None
            reasons.append(_reason("forced", "사용자가 분석 실행을 요청함 (mode=force)"))
        if self.scan.truncated:
            self.questions.append(
                _question("scan_truncated", "파일 수/깊이 상한으로 일부 디렉터리를 보지 못했습니다.")
            )
        for path in sorted(set(self.scan.oversized)):
            self.questions.append(_question("file_too_large", f"{path} 파일이 커서 읽지 않았습니다."))
        if self.request.ai:
            self.questions.append(
                _question("ai_not_configured", "AI 분석이 구성되지 않아 정적 분석 결과만 제공합니다.")
            )
        analyze = decision == "analyze"
        if analyze:
            self._init_scripts()
        if not analyze:
            self.questions = [
                item for item in self.questions if item["code"] in {"ai_not_configured", "scan_truncated"}
            ]
        return {
            "schemaVersion": RESULT_VERSION,
            "sourceSha": self.request.source_sha,
            "rootDirectory": self.scope,
            "decision": decision,
            "complexity": complexity,
            "reasons": reasons,
            "signals": {
                "dockerfiles": self.all_dockerfiles,
                "composeFiles": self.compose_files,
                "composeBuildServices": sorted(service.name for service in self.build_services),
                "composeImageServices": sorted(service.name for service in self.image_services),
                "workspaceManifests": sorted(set(self.workspace_manifests)),
                "runtimeManifests": self.manifests[:200],
            },
            "simpleBuild": simple_build,
            "units": [unit.contract() for unit in sorted(self.units, key=lambda u: (u.root, u.id))]
            if analyze
            else [],
            "dependencies": [self.dependencies[key] for key in sorted(self.dependencies)] if analyze else [],
            "questions": self.questions,
            "analysis": {"engine": "static", "durationMs": 0, "modelCalls": 0},
            "executionAuthorized": False,
        }

    # -- units --------------------------------------------------------------
    def _new_id(self, base: str) -> str:
        candidate = _slug(base)
        index = 2
        while candidate in self.ids:
            candidate = f"{_slug(base)[:36]}-{index}"
            index += 1
        self.ids.add(candidate)
        return candidate

    def _dependency(
        self,
        name: str,
        engine: str,
        image: str | None,
        evidence: list[dict],
        service: ComposeService | None = None,
    ) -> str:
        profile = dependency_profile(engine, service) if service is not None else {}
        for key, row in self.dependencies.items():
            if row["engine"] == engine and engine != "other" and (image is None or row["image"] == image):
                row["evidence"] = _unique_evidence(row["evidence"] + evidence)
                _merge_profile(row, profile)
                self._collect_init(key, service)
                return key
        identifier = self._new_id(name)
        self.dependencies[identifier] = {
            "id": identifier,
            "engine": engine,
            "image": image,
            "port": DEFAULT_PORTS.get(engine),
            "database": None,
            "user": None,
            "passwordInSource": False,
            "evidence": _unique_evidence(evidence),
        }
        _merge_profile(self.dependencies[identifier], profile, replace_port=True)
        self._collect_init(identifier, service)
        return identifier

    def _collect_init(self, identifier: str, service: ComposeService | None) -> None:
        if service is None or not service.volumes:
            return
        files = mounted_files(self.scan.repo_root, self.scope, parent(service.file), service.volumes)
        if files:
            self.init_files.setdefault(identifier, {}).update(files)

    def _init_scripts(self) -> None:
        for identifier, files in sorted(self.init_files.items()):
            row = self.dependencies[identifier]
            scripts, problems = describe_scripts(self.scan.repo_root, row["engine"], files)
            if not scripts:
                continue
            row["initScripts"] = scripts
            for code, path in dict.fromkeys(problems):
                reason = (
                    "플랫폼이 셸 스크립트를 실행하지 않습니다"
                    if code == "init_script_unsupported"
                    else "파일당·합계 1 MiB 상한을 넘습니다"
                )
                self.questions.append(
                    _question(code, f"'{identifier}' 초기화 스크립트 {path}를 자동 실행할 수 없습니다: {reason}.")
                )

    def _extract_units(self) -> None:
        service_ids: dict[str, str] = {}
        claimed: set[str] = set()
        # 1) Compose: build services -> units, image services -> dependencies.
        for service in self.image_services:
            if is_adjunct(service.image):
                continue
            engine = image_engine(service.image)
            evidence = [{"path": service.file, "line": service.line}]
            if engine is not None:
                service_ids[service.name] = self._dependency(
                    service.name, engine, service.image, evidence, service
                )
            elif self.build_services:
                self.questions.append(
                    _question(
                        "image_service_ignored",
                        f"Compose 서비스 '{service.name}'는 빌드 없이 이미지({service.image or '미지정'})만 "
                        "사용해 배포 단위에서 제외했습니다.",
                    )
                )
        compose_units: list[tuple[_Unit, ComposeService]] = []
        for service in self.build_services:
            evidence = [{"path": service.file, "line": service.line}]
            if service.problem or service.root is None:
                self.questions.append(
                    _question(
                        "compose_build_unsupported",
                        f"Compose 서비스 '{service.name}'의 build 설정({service.problem})을 정적으로 "
                        "해석할 수 없어 제외했습니다.",
                    )
                )
                continue
            if not within(service.root, self.scope):
                self.questions.append(
                    _question(
                        "unit_outside_scope",
                        f"Compose 서비스 '{service.name}'의 빌드 컨텍스트가 요청 루트 밖에 있어 제외했습니다.",
                    )
                )
                continue
            path = join(service.root, service.dockerfile or "Dockerfile")
            docker = self._docker(path) if self.scan.exists(path) else None
            if docker is not None:
                claimed.add(path)
            engine = database_engine_hint(service, docker.final_image if docker else None)
            if engine is not None:
                if docker is not None:
                    evidence.append({"path": path, "line": 1})
                service_ids[service.name] = self._dependency(
                    service.name,
                    engine,
                    (docker.final_image if docker else None) or service.image,
                    evidence,
                    service,
                )
                self.questions.append(
                    _question(
                        "dependency_built_from_dockerfile",
                        f"'{service.name}'는 {engine} 데이터베이스를 직접 빌드합니다. 플랫폼은 DB를 만들지 "
                        "않으므로 관리형/외부 인스턴스를 Variables로 연결하세요.",
                    )
                )
                continue
            unit = _Unit(
                id=self._new_id(service.name),
                root=service.root,
                builder="dockerfile",
                dockerfile=service.dockerfile or "Dockerfile",
                start_command=service.command,
                env=list(service.env),
                evidence=evidence,
                docker=docker,
                compose=service,
            )
            service_ids[service.name] = unit.id
            if docker is None:
                self.questions.append(
                    _question(
                        "dockerfile_missing",
                        f"'{service.name}'의 Dockerfile({path})을 찾지 못했습니다.",
                        unit.id,
                    )
                )
            for port, line in service.ports:
                if unit.port is None:
                    unit.port = port
                    unit.evidence.append({"path": service.file, "line": line})
            compose_units.append((unit, service))
            self.units.append(unit)
        for unit, service in compose_units:
            for name in service.depends_on:
                if name in service_ids and service_ids[name] != unit.id:
                    unit.depends_on.append(service_ids[name])
            for host in service.hosts:
                if host in service_ids and service_ids[host] != unit.id:
                    unit.depends_on.append(service_ids[host])
            for engine in service.env_engines.values():
                if not any(
                    row["engine"] == engine and row["id"] in unit.depends_on
                    for row in self.dependencies.values()
                ):
                    unit.depends_on.append(
                        self._dependency(engine, engine, None, [{"path": service.file, "line": service.line}])
                    )
            unit.role = self._role(service.name, unit)

        # 2) Dockerfiles not referenced by Compose.
        for path in self.docker_candidates:
            if path in claimed:
                continue
            docker = self._docker(path)
            directory = parent(path)
            name = PurePosixPath(path).name
            variant = dockerfile_variant(name)
            base = self._base_name(directory)
            label = f"{base}-{variant}" if variant else base
            engine = image_engine(docker.final_image)
            if engine in DATABASE_ENGINES:
                self._dependency(label, engine, docker.final_image, [{"path": path, "line": 1}])
                self.questions.append(
                    _question(
                        "dependency_built_from_dockerfile",
                        f"{path}는 {engine} 데이터베이스 이미지입니다. 플랫폼은 DB를 만들지 않으므로 "
                        "관리형/외부 인스턴스를 Variables로 연결하세요.",
                    )
                )
                continue
            if not within(directory, self.scope):
                continue
            unit = _Unit(
                id=self._new_id(label),
                root=directory,
                builder="dockerfile",
                dockerfile=name,
                evidence=[{"path": path, "line": 1}],
                docker=docker,
            )
            unit.role = self._role(label, unit)
            self.units.append(unit)

        # 3) Railpack units for runnable manifests not built by a Dockerfile.
        docker_roots = [(unit.root, unit.docker) for unit in self.units if unit.builder == "dockerfile"]
        nested_roots = [unit.root for unit in self.units if unit.root != self.scope]
        directories = sorted(set(self.workspace_apps) | set(self._app_manifest_dirs()))
        for directory in directories:
            if self._covered(directory, docker_roots) or any(
                within(directory, root) for root in nested_roots
            ):
                continue
            manifests = self.manifest_dirs.get(directory, [])
            evidence = [{"path": join(directory, name), "line": 1} for name in manifests[:3]]
            label = self._base_name(directory)
            package = load_package(self.scan, join(directory, "package.json")) or {}
            if directory != self.scope and isinstance(package.get("name"), str):
                label = package["name"]
            unit = _Unit(id=self._new_id(label), root=directory, builder="railpack", evidence=evidence)
            unit.port = script_port(package)
            unit.role = self._role(label, unit)
            self.units.append(unit)

        # Procfile with several process types: one unit per process at the scope root.
        if len(self.procfile) >= 2:
            root_unit = next((unit for unit in self.units if unit.root == self.scope), None)
            if root_unit is not None:
                self.units.remove(root_unit)
                self.ids.discard(root_unit.id)
                for process, command, line in self.procfile:
                    unit = _Unit(
                        id=self._new_id(process if process != "web" else root_unit.id),
                        root=root_unit.root,
                        builder=root_unit.builder,
                        dockerfile=root_unit.dockerfile,
                        start_command=command,
                        port=command_port(command),
                        role="web" if process == "web" else "worker",
                        env=list(root_unit.env),
                        evidence=root_unit.evidence + [{"path": join(self.scope, "Procfile"), "line": line}],
                        docker=root_unit.docker,
                        compose=root_unit.compose,
                    )
                    self.units.append(unit)

        for unit in self.units:
            self._enrich(unit)
        self.service_ids = service_ids
        self._link_units()

    def _base_name(self, directory: str) -> str:
        if directory == ".":
            return "app"
        return PurePosixPath(directory).name

    def _role(self, name: str, unit: _Unit) -> str:
        package = load_package(self.scan, join(unit.root, "package.json")) or {}
        framework = node_role(package) or python_role(self.scan, unit.root)
        final_image = unit.docker.final_image if unit.docker else None
        if _WORKER_NAME.search(name):
            return "worker"
        if final_image and _STATIC_IMAGES.search(final_image.split("@")[0].split(":")[0]):
            return "web"
        if _WEB_NAME.search(name):
            return "web"
        if _API_NAME.search(name):
            return "api"
        return framework or "app"

    def _enrich(self, unit: _Unit) -> None:
        """Ports, env keys and client-library dependencies from the unit's own files."""
        docker = unit.docker
        if unit.port is None and docker is not None:
            if docker.exposed:
                unit.port, line = docker.exposed[0]
                unit.evidence.append({"path": docker.path, "line": line})
            elif docker.env_port:
                unit.port, line = docker.env_port
                unit.evidence.append({"path": docker.path, "line": line})
            elif command_port(docker.command):
                unit.port = command_port(docker.command)
        if unit.port is None:
            found = source_port(self.scan, unit.root)
            if found is not None:
                unit.port = found[0]
                unit.evidence.append({"path": found[1], "line": found[2]})
        if unit.port is None:
            self.questions.append(
                _question(
                    "port_unknown",
                    f"'{unit.id}'의 수신 포트를 소스에서 확인하지 못했습니다. 포트를 지정하세요"
                    + (" (워커라면 비워 둘 수 있습니다)." if unit.role == "worker" else "."),
                    unit.id,
                )
            )
        known = {row["key"] for row in unit.env}
        for key, required, engine, path, line in env_example_keys(self.scan, unit.root):
            if key not in known:
                unit.env.append({"key": key, "stage": "runtime", "required": required})
            if engine:
                self._link(unit, engine, path, line)
        for engine, path, line in inferred_dependencies(self.scan, unit.root):
            self._link(unit, engine, path, line)

    def _link(self, unit: _Unit, engine: str, path: str, line: int) -> None:
        for row in self.dependencies.values():
            if row["engine"] == engine:
                if row["id"] not in unit.depends_on:
                    unit.depends_on.append(row["id"])
                return
        unit.depends_on.append(self._dependency(engine, engine, None, [{"path": path, "line": line}]))

    # -- links: env bindings and host aliases ------------------------------
    def _link_units(self) -> None:
        kinds = {unit.id: "unit" for unit in self.units}
        kinds.update({key: "dependency" for key in self.dependencies})
        names = {identifier: identifier for identifier in kinds}
        names.update({name: target for name, target in self.service_ids.items() if target in kinds})
        engines = {key: row["engine"] for key, row in self.dependencies.items()}
        by_engine: dict[str, list[str]] = {}
        for key, engine in engines.items():
            by_engine.setdefault(engine, []).append(key)
        single = {engine: ids[0] if len(ids) == 1 else None for engine, ids in by_engine.items()}
        ports = {unit.id: unit.port for unit in self.units}
        ports.update({key: row["port"] for key, row in self.dependencies.items()})

        def owners(path: str) -> list[_Unit]:
            candidates = [unit for unit in self.units if within(path, unit.root)]
            if not candidates:
                return []
            deepest = max(len(unit.root) if unit.root != "." else 0 for unit in candidates)
            return [u for u in candidates if (len(u.root) if u.root != "." else 0) == deepest]

        configured: dict[str, list[tuple]] = {}
        for path, host, port, line in config_aliases(self.scan, names):
            for unit in owners(path):
                configured.setdefault(unit.id, []).append((host, port, path, line))

        for unit in self.units:
            found: list[tuple[str, int | None, str, int]] = list(configured.get(unit.id, []))
            compose = unit.compose
            rows: dict[str, dict | None] = {}
            for row in unit.env:
                if row["stage"] != "runtime":
                    continue
                key = row["key"]
                value = compose.env_values.get(key) if compose else None
                binding = key_binding(key, value, names, kinds) if value is not None else None
                if binding and binding["targetId"] == unit.id:
                    binding = None
                rows[key] = binding
            sibling_bindings(rows, kinds)
            if compose:
                for key, value in compose.env_values.items():
                    line = compose.env_lines.get(key, compose.line)
                    found.extend(
                        (host, port, compose.file, line) for host, port in env_references(value, names)
                    )
                    parsed = whole_url_host(value)
                    if parsed and parsed.host in names and names[parsed.host] in self.dependencies:
                        self._learn(self.dependencies[names[parsed.host]], parsed)
            for row in unit.env:
                key = row["key"]
                if row["stage"] != "runtime":
                    continue
                binding = rows.get(key)
                if binding is None:
                    value = compose.env_values.get(key) if compose else None
                    parsed = parse_url(value) if value is not None else None
                    if not (parsed and parsed.host):
                        binding = heuristic_binding(key, unit.depends_on, engines, single)
                row["binding"] = binding
            paths = []
            for path in self.scan.files():
                if within(path, unit.root) and len(PurePosixPath(relative_to(path, unit.root)).parts) <= 5:
                    if unit in owners(path):
                        paths.append(path)
            found.extend(
                (host, port, path, line) for path, host, port, line in source_aliases(self.scan, names, paths)
            )

            merged: dict[tuple[str, int | None], dict] = {}
            for host, port, path, line in found:
                target = names[host]
                if target == unit.id:
                    continue
                if port is None:
                    port = ports.get(target)
                entry = merged.setdefault(
                    (host, port), {"host": host, "port": port, "targetId": target, "evidence": []}
                )
                entry["evidence"].append({"path": path, "line": line})
            unit.aliases = []
            for entry in sorted(merged.values(), key=lambda item: (item["host"], item["port"] or 0)):
                entry["evidence"] = _unique_evidence(entry["evidence"])[:MAX_EVIDENCE]
                unit.aliases.append(entry)
                if entry["targetId"] not in unit.depends_on:
                    unit.depends_on.append(entry["targetId"])
        for row in self.dependencies.values():
            if row["user"] is None:
                row["user"] = DEFAULT_USERS.get(row["engine"])

    @staticmethod
    def _learn(row: dict, parsed) -> None:
        """Non-secret connection facts from a URL that points at this dependency."""
        if row["user"] is None and parsed.user:
            row["user"] = parsed.user
        if row["database"] is None and parsed.database:
            row["database"] = parsed.database
        if parsed.password_literal:
            row["passwordInSource"] = True


def _merge_profile(row: dict, profile: dict, replace_port: bool = False) -> None:
    if profile.get("port") and (replace_port or row.get("port") is None):
        row["port"] = profile["port"]
    for key in ("database", "user"):
        if row.get(key) is None and profile.get(key):
            row[key] = profile[key]
    row["passwordInSource"] = bool(row["passwordInSource"] or profile.get("passwordInSource"))


def _compose_variant_ignored(name: str) -> bool:
    stem = name.lower().rsplit(".", 1)[0]
    parts = stem.split(".")
    return len(parts) > 1 and parts[-1] in _IGNORED_COMPOSE_VARIANTS
