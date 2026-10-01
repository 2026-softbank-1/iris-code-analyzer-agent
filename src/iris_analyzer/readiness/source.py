"""Bounded source-readiness observations; target code is never executed.

The context contains snippets rather than file handles. Whole-file checks run
only when unredacted snippets reconstruct the original SHA-256 exactly. This
prevents a cut-off function or a masking placeholder becoming a syntax finding.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from collections import defaultdict
from pathlib import PurePosixPath

import yaml
from jsonschema import Draft202012Validator

from iris_analyzer.contracts import AnalyzerError, digest, validate_bundle
from iris_analyzer.preprocess.javascript import Source
from iris_analyzer.preprocess.selector import component_of
from iris_analyzer.preprocess.snapshot import SOURCE_EXTENSIONS

from .metadata import BUILD_TARGET_SCHEMA, CONNECTION_SCHEMA, ENVIRONMENT_SCHEMA, execution_metadata

_LANGUAGES = {
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".py": "Python",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".rb": "Ruby",
    ".cs": "C#",
    ".c": "C",
    ".cpp": "C++",
}
_HINTS = {".nvmrc", ".node-version"}
_STRICT_JSON = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "lerna.json"}
_IMAGE_RUNTIMES = {"node", "python", "nginx", "bun", "deno", "httpd", "golang", "rust"}

_EVIDENCE = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
_TEXT = {"type": "string", "minLength": 1}
_PATH = {"type": ["string", "null"]}


def _record(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


READINESS_SCHEMA = _record(
    {
        "schemaVersion": {"const": "iris.source-readiness.v1"},
        "sourceSnapshotId": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "contextHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "languages": {
            "type": "array",
            "items": _record(
                {
                    "language": _TEXT,
                    "status": {"const": "detected"},
                    "evidenceIds": _EVIDENCE,
                    "paths": {"type": "array", "items": _TEXT, "minItems": 1},
                    "reason": _TEXT,
                }
            ),
        },
        "runtimeVersions": {
            "type": "array",
            "items": _record(
                {
                    "runtime": _TEXT,
                    "component": _TEXT,
                    "scope": {"enum": ["source", "build", "runtime", "container_stage_unknown"]},
                    "stage": _PATH,
                    "constraint": {"type": ["string", "null"]},
                    "imageReference": {"type": ["string", "null"]},
                    "imageDigest": {"type": ["string", "null"]},
                    "versionKind": {
                        "enum": [
                            "declared_constraint",
                            "version_file",
                            "image_tag",
                            "image_digest",
                            "unknown",
                        ]
                    },
                    "status": {"enum": ["detected", "unknown", "conflict"]},
                    "evidenceIds": _EVIDENCE,
                    "path": _PATH,
                    "reason": _TEXT,
                }
            ),
        },
        "findings": {
            "type": "array",
            "items": _record(
                {
                    "ruleId": _TEXT,
                    "severity": {"enum": ["error", "warning", "info"]},
                    "status": {"enum": ["detected", "needs_review"]},
                    "evidenceIds": _EVIDENCE,
                    "path": _TEXT,
                    "reason": _TEXT,
                    "startLine": {"type": "integer", "minimum": 1},
                    "endLine": {"type": "integer", "minimum": 1},
                }
            ),
        },
        "coverage": _record(
            {
                "selectedFileCount": {"type": "integer", "minimum": 0},
                "completeVerifiedFiles": {"type": "array", "items": _TEXT},
                "syntaxCheckedFiles": {"type": "array", "items": _TEXT},
                "jsonCheckedFiles": {"type": "array", "items": _TEXT},
                "containerCheckedFiles": {"type": "array", "items": _TEXT},
                "skippedFiles": {"type": "array", "items": _record({"path": _TEXT, "reason": _TEXT})},
                "requiredContextPaths": {"type": "array", "items": _TEXT},
                "status": {"enum": ["partial", "selected_files_checked"]},
                "targetCodeExecuted": {"const": False},
            }
        ),
        "limitations": {"type": "array", "items": _TEXT, "minItems": 1},
    }
)
# Additive fields remain optional when older stored readiness documents are
# consumed; new captures always publish these supplemental observations.
READINESS_SCHEMA["properties"].update(
    {
        "buildTargets": {"type": "array", "items": BUILD_TARGET_SCHEMA},
        "environmentVariables": {"type": "array", "items": ENVIRONMENT_SCHEMA},
        "serviceConnections": {"type": "array", "items": CONNECTION_SCHEMA},
    }
)


def validate_readiness(document: dict) -> dict:
    """Validate the supplemental contract without changing analysis-result v1."""
    digest(document)  # Reject non-JSON/non-finite values as well.
    error = next(iter(Draft202012Validator(READINESS_SCHEMA).iter_errors(document)), None)
    if error is not None:
        raise AnalyzerError(
            "READINESS_SCHEMA_INVALID",
            "Invalid source-readiness document",
            {"path": list(error.absolute_path), "reason": error.message},
        )
    return document


def _verified_text(rows: list[dict], source_digest: str) -> str | None:
    """Reconstruct only bytes proven to be the entire captured source."""
    lines = {}
    for row in rows:
        if row["redacted"]:
            continue
        for number, text in enumerate(row["text"].split("\n"), row["startLine"]):
            if number in lines and lines[number] != text:
                return None
            lines[number] = text
    if not lines or sorted(lines) != list(range(1, max(lines) + 1)):
        return None
    values = [lines[number] for number in sorted(lines)]
    for separator in ("\n", "\r\n", "\r"):
        for final in ("", separator):
            for bom in ("", "\ufeff"):
                candidate = bom + separator.join(values) + final
                if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == source_digest:
                    return "\n".join(values)
    return None


def _ids(rows: list[dict], start: int | None = None, end: int | None = None) -> list[str]:
    return sorted(
        {
            row["evidenceId"]
            for row in rows
            if start is None or row["startLine"] <= (end or start) and row["endLine"] >= start
        }
    )


def _finding(
    rule: str,
    path: str,
    rows: list[dict],
    reason: str,
    *,
    severity: str = "warning",
    status: str = "needs_review",
    start: int = 1,
    end: int | None = None,
) -> dict:
    return {
        "ruleId": rule,
        "severity": severity,
        "status": status,
        "evidenceIds": _ids(rows, start, end),
        "path": path,
        "reason": reason,
        "startLine": start,
        "endLine": end or start,
    }


def _version(
    runtime: str,
    component: str,
    scope: str,
    constraint: str | None,
    rows: list[dict],
    path: str | None,
    *,
    stage: str | None = None,
    kind: str = "declared_constraint",
    image: str | None = None,
    image_digest: str | None = None,
    reason: str = "Declared version constraint; execution was not verified.",
) -> dict:
    return {
        "runtime": runtime,
        "component": component,
        "scope": scope,
        "stage": stage,
        "constraint": constraint,
        "imageReference": image,
        "imageDigest": image_digest,
        "versionKind": kind,
        "status": "detected" if constraint else "unknown",
        "evidenceIds": _ids(rows),
        "path": path,
        "reason": reason,
    }


def _eligible_configuration(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in _HINTS or name.startswith("dockerfile") or name in _STRICT_JSON or _is_compose(path)


def _is_compose(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"} or (
        name.startswith(("compose.", "docker-compose.")) and name.endswith((".yaml", ".yml"))
    )


def _image_constraint(image: str) -> tuple[str | None, str, str | None]:
    reference, separator, image_digest = image.partition("@")
    tail = reference.rsplit("/", 1)[-1]
    if "$" in image:
        return None, "unknown", None
    if ":" in tail:
        return tail.rsplit(":", 1)[1], "image_tag", image_digest if separator else None
    if separator:
        return image_digest, "image_digest", image_digest
    return None, "unknown", None


def _docker_versions(
    path: str, text: str | None, rows: list[dict], component: str, facts: list[dict]
) -> tuple[list[dict], list[dict]]:
    result, findings = [], []
    # Partial evidence can prove a FROM declaration, but cannot establish that
    # no later FROM exists. An existing final-stage runtime fact is an anchor.
    lines = text.split("\n") if text is not None else []
    stages = {}
    supplied = [
        (number, line)
        for row in rows
        for number, line in enumerate(row["text"].split("\n"), row["startLine"])
    ]
    instructions = list(enumerate(lines, 1)) if text is not None else sorted(set(supplied))
    froms = [
        (number, match)
        for number, line in instructions
        if (match := re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.I))
    ]
    for index, (number, match) in enumerate(froms):
        image, alias = match.groups()
        # Resolve aliases transitively, retaining the base-image evidence.
        image, inherited_lines = stages.get(image.lower(), (image, []))
        origin_lines = [*inherited_lines, number]
        if text is not None:  # Partial evidence cannot establish stage indices.
            stages[str(index)] = (image, origin_lines)
        if alias:
            stages[alias.lower()] = (image, origin_lines)
        name = image.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
        if name not in _IMAGE_RUNTIMES:
            continue
        evidence = [
            row
            for row in rows
            if any(row["startLine"] <= origin <= row["endLine"] for origin in origin_lines)
        ]
        identifiers = set(_ids(evidence))
        anchored = any(
            fact["scope"] == "container"
            and fact["key"] in {"runtime.name", "runtime.image", "hosting.runtime"}
            and identifiers.intersection(fact["evidenceIds"])
            for fact in facts
        )
        scope = (
            ("runtime" if index == len(froms) - 1 else "build")
            if text is not None
            else ("runtime" if anchored else "container_stage_unknown")
        )
        constraint, kind, image_digest = _image_constraint(image)
        result.append(
            _version(
                name,
                component,
                scope,
                constraint,
                evidence,
                path,
                stage=alias or (f"stage-{index + 1}" if text is not None else None),
                kind=kind,
                image=image,
                image_digest=image_digest,
                reason="Image reference was observed; tags do not establish the installed patch version or CPU architecture.",
            )
        )
    if text is not None:
        last_from = froms[-1][0] if froms else 1
        users = [
            (number, line)
            for number, line in instructions
            if number >= last_from and re.match(r"\s*USER\s+", line, re.I)
        ]
        if users:
            number, line = users[-1]
            if re.fullmatch(r"\s*USER\s+(?:root|0)(?::(?:root|0))?\s*", line, re.I):
                findings.append(
                    _finding(
                        "container.explicit_root_user",
                        path,
                        rows,
                        "The final image explicitly selects root. Review the production security context; this does not establish exploitability.",
                        start=number,
                    )
                )
    return result, findings


def _compose_versions(
    path: str, text: str, rows: list[dict], component: str
) -> tuple[list[dict], list[dict]]:
    versions, findings = [], []
    try:
        document = yaml.safe_load(text)
        tree = yaml.compose(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return [], [
            _finding(
                "config.invalid_compose_yaml",
                path,
                rows,
                "The complete supplied Compose configuration is not valid YAML.",
                severity="error",
                status="detected",
                start=mark.line + 1 if mark else 1,
            )
        ]
    services = document.get("services", {}) if isinstance(document, dict) else {}
    if not isinstance(services, dict) or not isinstance(tree, yaml.MappingNode):
        return [], []
    service_nodes = next((node for key, node in tree.value if key.value == "services"), None)
    if not isinstance(service_nodes, yaml.MappingNode):
        return [], []
    for key_node, service_node in service_nodes.value:
        service = services.get(key_node.value)
        if not isinstance(service, dict) or not isinstance(service_node, yaml.MappingNode):
            continue
        fields = {key.value: node for key, node in service_node.value}
        image = service.get("image")
        if isinstance(image, str) and "image" in fields:
            node = fields["image"]
            name = image.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
            if name in _IMAGE_RUNTIMES:
                evidence = [
                    row for row in rows if row["startLine"] <= node.start_mark.line + 1 <= row["endLine"]
                ]
                constraint, kind, image_digest = _image_constraint(image)
                versions.append(
                    _version(
                        name,
                        component,
                        "runtime",
                        constraint,
                        evidence,
                        path,
                        stage=f"compose:{key_node.value}",
                        kind=kind,
                        image=image,
                        image_digest=image_digest,
                        reason="Compose image reference was observed; image declarations do not establish installed runtime versions or architecture compatibility.",
                    )
                )
        for field, rule, reason in (
            (
                "privileged",
                "container.compose_privileged",
                "This service explicitly enables privileged container mode; review whether deployment requires that access.",
            ),
            (
                "network_mode",
                "container.compose_host_network",
                "This service explicitly selects host networking; review isolation and port conflicts for the target platform.",
            ),
        ):
            if field in fields and (
                service.get(field) is True if field == "privileged" else service.get(field) == "host"
            ):
                findings.append(_finding(rule, path, rows, reason, start=fields[field].start_mark.line + 1))
        volumes = service.get("volumes", [])
        if isinstance(volumes, list) and "volumes" in fields:
            for volume in volumes:
                origin = (
                    volume.split(":", 1)[0]
                    if isinstance(volume, str)
                    else volume.get("source")
                    if isinstance(volume, dict)
                    else None
                )
                if origin in {"/var/run/docker.sock", "/run/docker.sock"}:
                    findings.append(
                        _finding(
                            "container.compose_docker_socket",
                            path,
                            rows,
                            "This service explicitly mounts the host Docker socket; review the required host-engine access before translating it to deployment templates.",
                            start=fields["volumes"].start_mark.line + 1,
                        )
                    )
    return versions, findings


def _resolve_relative(origin: str, target: str, inventory: set[str]) -> bool:
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(origin), target))
    if candidate.startswith("../") or candidate.startswith("/") or candidate == "..":
        return False
    attempts = {candidate}
    suffix = PurePosixPath(candidate).suffix
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        attempts.update(
            candidate[: -len(suffix)] + replacement for replacement in (".ts", ".tsx", ".mts", ".cts")
        )
    if not suffix:
        attempts.update(candidate + extension for extension in (*SOURCE_EXTENSIONS, ".json"))
        attempts.update(posixpath.join(candidate, "index" + extension) for extension in SOURCE_EXTENSIONS)
        attempts.add(posixpath.join(candidate, "package.json"))
    return bool(attempts.intersection(inventory))


def _source_findings(path: str, text: str, rows: list[dict], inventory: set[str]) -> list[dict]:
    source = Source(path, text.encode("utf-8"))
    findings = []
    if source.tree.root_node.has_error:
        stack = [source.tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "ERROR" or node.is_missing:
                findings.append(
                    _finding(
                        "syntax.javascript_parser",
                        path,
                        rows,
                        "The syntax parser reports an error or missing token in the complete supplied source. Validate with the project compiler before treating it as a build failure.",
                        severity="error",
                        status="detected",
                        start=node.start_point.row + 1,
                        end=max(node.start_point.row + 1, node.end_point.row + 1),
                    )
                )
                if len(findings) == 20:
                    break
            else:
                stack.extend(reversed(node.children))
        return findings
    for reference in source.imports():
        target = reference["target"]
        # Query/fragment imports and generated modules require tool-specific
        # resolution; they are explicitly outside this lightweight check.
        if not target.startswith(".") or "?" in target or "#" in target:
            continue
        if not _resolve_relative(path, target, inventory):
            node = reference["node"]
            findings.append(
                _finding(
                    "imports.unresolved_relative",
                    path,
                    rows,
                    f"Relative import {target!r} has no literal source/index candidate in the captured inventory. Generated files and custom resolver behavior may satisfy it at build time.",
                    start=node.start_point.row + 1,
                    end=node.end_point.row + 1,
                )
            )
    limiter_import = re.search(r"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]express-rate-limit['\"]", text)
    if limiter_import:
        for call, function, args in source.calls():
            if source.text(function) != limiter_import[1] or not args or args[0].type != "object":
                continue
            properties = {
                source.text(p.child_by_field_name("key")).strip("\"'"): p.child_by_field_name("value")
                for p in args[0].named_children
                if p.type == "pair"
            }
            maximum = source.value(properties.get("limit") or properties.get("max"))
            window = properties.get("windowMs")
            if type(maximum) is not int or maximum < 1 or window is None:
                continue
            findings.append(
                _finding(
                    "traffic.configured_rate_limit",
                    path,
                    rows,
                    f"Observed express-rate-limit configuration limit={maximum}, windowMs={source.text(window)!r}. "
                    "Check the traffic scenario and limiter key/proxy scope; this setting is not maximum server capacity.",
                    severity="info",
                    status="needs_review",
                    start=call.start_point.row + 1,
                    end=call.end_point.row + 1,
                )
            )
    return findings


def _command_findings(path: str, package: dict, rows: list[dict], inventory: set[str]) -> list[dict]:
    findings = []
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return findings
    for name, command in sorted(scripts.items()):
        if not isinstance(command, str):
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        # Only direct, literal runtime invocations. No shell evaluation or
        # guessed working directory for nested/workspace commands.
        if (
            len(tokens) < 2
            or tokens[0] not in {"node", "tsx", "ts-node"}
            or any(char in command for char in ("$", "&&", "||", ";", "|", "*", "<", ">"))
        ):
            continue
        target = tokens[1]
        if target.startswith("-") or PurePosixPath(target).suffix not in SOURCE_EXTENSIONS:
            continue
        destination = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if destination in inventory:
            continue
        findings.append(
            _finding(
                "commands.missing_literal_target",
                path,
                rows,
                f"Script {name!r} references {target!r}, which is absent from the captured source inventory. It may be generated by the build; this check did not execute the build.",
                start=1,
                end=max(row["endLine"] for row in rows),
            )
        )
    return findings


def _allowed_major(constraint: str, major: int) -> bool | None:
    """Prove obvious major-version exclusions; never implement guessed semver."""
    tokens = constraint.strip().split()
    if not tokens or any(part in constraint for part in ("||", " - ", "*", "x", "X")):
        return None
    checks = []
    for token in tokens:
        match = re.fullmatch(r"(>=|<=|>|<|\^|~|=)?v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", token)
        if not match:
            return None
        operator, first, minor, patch = match.groups()
        first = int(first)
        if operator in {None, "=", "^", "~"}:
            checks.append(major == first)
        elif operator == ">=":
            checks.append(major >= first)
        elif operator == ">":
            checks.append(major >= first)  # same major may contain newer minors
        elif operator == "<":
            checks.append(major < first if minor in {None, "0"} and patch in {None, "0"} else major <= first)
        else:
            checks.append(major <= first)
    return all(checks)


def _version_conflicts(versions: list[dict], rows_by_path: dict[str, list[dict]]) -> list[dict]:
    findings = []
    node_constraints = [
        item
        for item in versions
        if item["runtime"] == "node"
        and item["scope"] == "source"
        and item["versionKind"] == "declared_constraint"
        and item["constraint"]
    ]
    for declaration in node_constraints:
        for candidate in versions:
            if (
                candidate["runtime"] != "node"
                or candidate["scope"] in {"build", "container_stage_unknown"}
                or candidate["versionKind"] not in {"version_file", "image_tag"}
                or not candidate["constraint"]
            ):
                continue
            root = declaration["component"]
            if root != "." and candidate["component"] != root:
                continue
            match = re.match(r"v?(\d+)(?:[.\-]|$)", candidate["constraint"])
            if not match or _allowed_major(declaration["constraint"], int(match[1])) is not False:
                continue
            declaration["status"] = candidate["status"] = "conflict"
            path = candidate["path"]
            item = _finding(
                "runtime.incompatible_major_constraint",
                path,
                rows_by_path[path],
                f"Observed Node declaration {candidate['constraint']!r} is outside the major versions permitted by {declaration['constraint']!r}. Confirm which runtime/build stage the deployment uses.",
                severity="error",
                status="detected",
                start=1,
                end=max(row["endLine"] for row in rows_by_path[path]),
            )
            item["evidenceIds"] = sorted(set(candidate["evidenceIds"] + declaration["evidenceIds"]))
            findings.append(item)
    return findings


def build_readiness(bundle: dict) -> dict:
    """Return facts/check coverage from the supplied bundle, with no source reads."""
    validate_bundle(bundle)
    if digest({key: value for key, value in bundle.items() if key != "contextHash"}) != bundle["contextHash"]:
        raise AnalyzerError("CONTEXT_HASH_INVALID", "Readiness requires an unchanged context bundle")
    manifest = {row["path"]: row for row in bundle["manifest"]}
    selected_index = {row["path"]: row for row in bundle["selectedFiles"]}
    evidence_ids = {row["evidenceId"] for row in bundle["evidence"]}
    if (
        len(manifest) != len(bundle["manifest"])
        or len(selected_index) != len(bundle["selectedFiles"])
        or len(evidence_ids) != len(bundle["evidence"])
        or evidence_ids != set(bundle["coverage"]["providedEvidenceIds"])
    ):
        raise AnalyzerError(
            "READINESS_EVIDENCE_INVALID", "Readiness requires an unambiguous source/evidence inventory"
        )
    rows_by_path = defaultdict(list)
    for row in bundle["evidence"]:
        if (
            row["path"] not in manifest
            or row["path"] not in selected_index
            or not manifest[row["path"]]["eligible"]
            or row["sourceDigest"] != manifest[row["path"]]["digest"]
            or hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["contentDigest"]
            or row["text"].count("\n") + 1 != row["endLine"] - row["startLine"] + 1
            or not any(
                interval["startLine"] <= row["startLine"] <= row["endLine"] <= interval["endLine"]
                for interval in selected_index[row["path"]]["providedRanges"]
            )
        ):
            raise AnalyzerError("READINESS_EVIDENCE_INVALID", "Readiness evidence does not match its source")
        rows_by_path[row["path"]].append(row)
    complete = {
        path: text
        for path, rows in rows_by_path.items()
        if (text := _verified_text(rows, manifest[path]["digest"])) is not None
    }
    inventory = set(manifest)
    roots = bundle["componentRoots"]
    languages = defaultdict(list)
    versions, findings, syntax, json_checked, container_checked, skipped = [], [], [], [], [], []
    known_node_components = {
        fact.get("component", ".")
        for fact in bundle["facts"]
        if fact["key"] == "runtime.name" and fact["value"] == "node" and fact["scope"] == "source"
    }
    for path, rows in sorted(rows_by_path.items()):
        pure = PurePosixPath(path)
        component = component_of(path, roots)
        name = pure.name.lower()
        if pure.suffix.lower() in _LANGUAGES:
            languages[_LANGUAGES[pure.suffix.lower()]].append(path)
        if name.startswith("dockerfile"):
            image_versions, image_findings = _docker_versions(
                path, complete.get(path), rows, component, bundle["facts"]
            )
            versions.extend(image_versions)
            findings.extend(image_findings)
            if path in complete:
                container_checked.append(path)
        if _is_compose(path) and path in complete:
            image_versions, image_findings = _compose_versions(path, complete[path], rows, component)
            versions.extend(image_versions)
            findings.extend(image_findings)
            container_checked.append(path)
        checkable = pure.suffix.lower() in SOURCE_EXTENSIONS or _eligible_configuration(path)
        if checkable and path not in complete:
            skipped.append(
                {
                    "path": path,
                    "reason": "Complete original bytes could not be proven from partial or redacted evidence; whole-file checks were skipped.",
                }
            )
            continue
        if name in _HINTS and path in complete:
            value = complete[path].strip()
            literal = re.fullmatch(r"v?\d+(?:\.\d+){0,2}", value)
            versions.append(
                _version(
                    "node",
                    component,
                    "source",
                    value if literal else None,
                    rows,
                    path,
                    kind="version_file" if literal else "unknown",
                    reason="Literal Node version-file declaration; execution was not verified."
                    if literal
                    else "Version file uses an alias or unsupported expression; no concrete version was inferred.",
                )
            )
        if name in _STRICT_JSON and path in complete:
            json_checked.append(path)
            try:
                document = json.loads(complete[path])
            except json.JSONDecodeError as exc:
                findings.append(
                    _finding(
                        "config.invalid_strict_json",
                        path,
                        rows,
                        "The complete supplied manifest/configuration is not valid strict JSON.",
                        severity="error",
                        status="detected",
                        start=exc.lineno,
                    )
                )
                continue
            if not isinstance(document, dict):
                findings.append(
                    _finding(
                        "config.manifest_not_object",
                        path,
                        rows,
                        "This package manifest/configuration must be a JSON object.",
                        severity="error",
                        status="detected",
                    )
                )
                continue
            if name == "package.json":
                known_node_components.add(component)
                for field in ("engines", "scripts", "dependencies", "devDependencies"):
                    if field in document and not isinstance(document[field], dict):
                        findings.append(
                            _finding(
                                "config.package_field_not_object",
                                path,
                                rows,
                                f"Package field {field!r} must be a JSON object.",
                                severity="error",
                                status="detected",
                            )
                        )
                engines = document.get("engines", {})
                if isinstance(engines, dict):
                    for runtime, value in sorted(engines.items()):
                        if (
                            runtime in {"node", "npm", "yarn", "pnpm", "bun"}
                            and isinstance(value, str)
                            and value
                        ):
                            versions.append(_version(runtime, component, "source", value, rows, path))
                findings.extend(_command_findings(path, document, rows, inventory))
        if pure.suffix.lower() in SOURCE_EXTENSIONS and path in complete:
            syntax.append(path)
            findings.extend(_source_findings(path, complete[path], rows, inventory))
    findings.extend(_version_conflicts(versions, rows_by_path))
    for component in sorted(known_node_components):
        if not any(
            item["runtime"] == "node" and item["component"] in {component, "."} and item["constraint"]
            for item in versions
        ):
            path = posixpath.join(component, "package.json") if component != "." else "package.json"
            if path not in rows_by_path:
                continue
            versions.append(
                _version(
                    "node",
                    component,
                    "source",
                    None,
                    rows_by_path[path],
                    path,
                    kind="unknown",
                    reason="Node application metadata was observed, but no supplied concrete Node version constraint is available.",
                )
            )
    required = sorted(
        (
            path
            for path, row in manifest.items()
            if row["eligible"]
            and _eligible_configuration(path)
            and path not in complete
            and PurePosixPath(path).name not in {"package-lock.json", "npm-shrinkwrap.json"}
        ),
        key=lambda path: (
            0
            if PurePosixPath(path).name in _HINTS
            else 1
            if PurePosixPath(path).name.lower().startswith("dockerfile")
            else 2
            if _is_compose(path)
            else 3,
            path,
        ),
    )
    selected = {row["path"] for row in bundle["selectedFiles"]}
    report = {
        "schemaVersion": "iris.source-readiness.v1",
        "sourceSnapshotId": bundle["source"]["snapshotId"],
        "contextHash": bundle["contextHash"],
        **execution_metadata(bundle),
        "languages": [
            {
                "language": language,
                "status": "detected",
                "paths": paths,
                "evidenceIds": sorted(
                    {identifier for path in paths for identifier in _ids(rows_by_path[path])}
                ),
                "reason": "Language inferred from supplied source-file extensions; syntax checks cover JavaScript/TypeScript only.",
            }
            for language, paths in sorted(languages.items())
        ],
        "runtimeVersions": sorted(
            versions,
            key=lambda row: (
                row["component"],
                row["runtime"],
                row["scope"],
                row["path"] or "",
                row["stage"] or "",
            ),
        ),
        "findings": sorted(
            findings, key=lambda row: (row["path"], row["startLine"], row["ruleId"], row["reason"])
        ),
        "coverage": {
            "selectedFileCount": len(selected),
            "completeVerifiedFiles": sorted(complete),
            "syntaxCheckedFiles": syntax,
            "jsonCheckedFiles": json_checked,
            "containerCheckedFiles": container_checked,
            "skippedFiles": skipped,
            "requiredContextPaths": required,
            "status": "partial"
            if skipped or required or bundle["coverage"]["truncated"]
            else "selected_files_checked",
            "targetCodeExecuted": False,
        },
        "limitations": [
            "Only supplied immutable sanitized evidence is inspected; unselected, excluded and masked content may contain additional defects.",
            "No target code, package install, compiler, unit tests, container build or network service is executed by this module.",
            "Syntax parser errors require project-compiler confirmation; absence of findings does not establish build success or security.",
            "Relative import and command checks inspect the captured source inventory; generated output and custom resolution can require review.",
            "Image tags and version constraints are declarations, not measured installed runtime versions or architecture compatibility.",
            "Source checks cannot establish CPU/memory capacity, suitable instance size, aggregate throughput, availability or deployment readiness. Literal rate-limit configurations are settings, not measured capacity.",
        ],
    }
    return validate_readiness(report)
