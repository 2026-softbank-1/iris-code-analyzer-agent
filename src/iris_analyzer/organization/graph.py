"""Evidence-backed system graph across immutable repository analyses.

Names, matching environment keys and common ports are useful questions, never
proof that two independently developed repositories form one runtime system.
Runtime relationships may be cyclic; deployment ordering belongs to the plan.
"""

from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from iris_analyzer.contracts import digest

_APPLICATION_ROLES = {"api", "server", "web_api", "static", "web", "frontend", "worker", "job"}
_HTTP_ROLES = {"api", "server", "web_api"}
_RELATION_KINDS = {"http", "database", "queue", "storage", "package"}
_LIBRARY_ROLES = {"library", "package", "shared_library"}


def _dns_id(raw: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]", "-", raw.lower()).strip("-")
    if normalized == raw and len(raw) <= 63:
        return raw
    return (normalized[:50].rstrip("-") or "repo") + "-" + digest(raw)[:12]


def service_component_id(repository_id: str, source_service_id: str) -> str:
    """Stable DNS label; normalization cannot collapse distinct source IDs."""
    return _dns_id(f"repo-{repository_id}--{source_service_id}")


def _value(field):
    return field.get("value") if isinstance(field, dict) and "value" in field else field


def _relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return value


def _ids(value) -> list[str]:
    found = set()
    if isinstance(value, dict):
        found.update(x for x in value.get("evidenceIds", []) if isinstance(x, str))
        if isinstance(value.get("evidenceId"), str):
            found.add(value["evidenceId"])
        for nested in value.values():
            found.update(_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_ids(nested))
    return sorted(found)


def _endpoint(value) -> tuple | None:
    """Exact credential-free HTTP base URLs; localhost is not an identity."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    if host.lower() in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return None
    if parsed.query or parsed.fragment:
        return None
    return (
        parsed.scheme,
        host.lower(),
        port or (443 if parsed.scheme == "https" else 80),
        parsed.path.rstrip("/"),
    )


def _safe_observed_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return value if parsed.scheme in {"http", "https"} or value.startswith("/") else None


def _kind(value: dict) -> str:
    declared = value.get("kind") or value.get("type")
    if declared in _RELATION_KINDS:
        return declared
    if value.get("mountPath") or value.get("volumeName"):
        return "storage"
    engine = str(value.get("engine", "")).lower()
    if engine in {"rabbitmq", "kafka", "sqs", "nats", "amqp"}:
        return "queue"
    if engine in {"s3", "minio", "gcs", "azure_blob"}:
        return "storage"
    return "database" if engine else "package"


class _Graph:
    def __init__(self, records: list[dict], request: dict):
        self.request = request
        self.records = {str(record["repositoryId"]): record for record in records}
        self.components: dict[str, dict] = {}
        self.by_repository: dict[str, list[dict]] = {}
        self.evidence: dict[str, dict] = {}
        self.evidence_rows: dict[str, dict[str, dict]] = {}
        self.questions: dict[str, dict] = {}
        self.relationships: dict[str, dict] = {}
        self.limitations: set[str] = {
            "Runtime call relationships do not establish deployment order or runtime health.",
            "External dependencies are observations and require explicit resource or endpoint bindings.",
        }
        self.compose_names: dict[str, dict[str, set[str]]] = {}
        self.endpoints: dict[tuple, set[str]] = {}
        self.endpoint_refs: dict[tuple, set[str]] = {}

    def question(
        self, key, reason, service_id=None, candidates=(), *, required=True, kind="user_configuration"
    ):
        if required and service_id in self.components and not self.components[service_id]["selected"]:
            required = False
        if (
            required
            and service_id is None
            and candidates
            and all(
                candidate in self.components and not self.components[candidate]["selected"]
                for candidate in candidates
            )
        ):
            required = False
        self.questions[key] = {
            "key": key,
            "reason": reason,
            "kind": kind,
            "required": required,
            "serviceId": service_id,
            "candidateServiceIds": sorted(set(candidates)),
        }

    def refs(self, repository_id: str, value) -> list[str]:
        record = self.records[repository_id]
        rows = self.evidence_rows.get(repository_id, {})
        refs = []
        for original_id in _ids(value):
            row = rows.get(original_id, {})
            identifier = f"repo-{repository_id}@{record.get('commitSha') or 'unpinned'}:{original_id}"
            if identifier not in self.evidence:
                path = _relative_path(row.get("path"))
                self.evidence[identifier] = {
                    "id": identifier,
                    "repositoryId": repository_id,
                    "repositoryUrl": record.get("repositoryUrl"),
                    "commitSha": record.get("commitSha"),
                    "sourceSnapshotId": record.get("sourceSnapshotId"),
                    "originalEvidenceId": original_id,
                    "path": path,
                    "startLine": row.get("startLine") if path else None,
                    "endLine": row.get("endLine") if path else None,
                }
                if not path:
                    self.limitations.add(
                        f"{record['fullName']}: evidence {original_id} has no exported source location."
                    )
            refs.append(identifier)
        return sorted(refs)

    def relationship(
        self, source, target, kind, status, refs=(), *, key=None, phase="unknown", reason, url=None
    ):
        if kind not in _RELATION_KINDS:
            kind = "http"
        if phase not in {"build", "runtime", "unknown"}:
            phase = "unknown"
        identity = {"from": source, "to": target, "kind": kind, "key": key, "phase": phase}
        identifier = "rel-" + digest(identity)[:20]
        previous = self.relationships.get(identifier)
        priority = {"unknown": 0, "suggested": 1, "detected": 2, "user_confirmed": 3}
        if previous and priority[previous["status"]] > priority[status]:
            previous["evidenceRefs"] = sorted(set(previous["evidenceRefs"]) | set(refs))
            return previous
        row = {
            "id": identifier,
            "fromServiceId": source,
            "toServiceId": target,
            "kind": kind,
            "status": status,
            "environmentKey": key,
            "phase": phase,
            "evidenceRefs": sorted(set(refs) | set((previous or {}).get("evidenceRefs", []))),
            "reason": reason,
        }
        observed_url = _safe_observed_url(url)
        if observed_url:
            row["observedUrl"] = observed_url
        self.relationships[identifier] = row
        return row

    def services(self, repository_id):
        return [row for row in self.by_repository.get(repository_id, []) if row["kind"] == "service"]

    def owner_candidates(self, repository_id: str, value: dict) -> list[str]:
        services = self.services(repository_id)
        named_id = value.get("fromServiceId") or value.get("serviceId")
        if named_id:
            return [row["id"] for row in services if named_id in {row["id"], row["sourceServiceId"]}]
        component = value.get("fromComponent") or value.get("component")
        name = value.get("fromService") or value.get("serviceName")
        if name and component:
            expected = "svc-" + digest({"service": name, "root": component})[:16]
            matching = [row["id"] for row in services if row["sourceServiceId"] == expected]
            if matching:
                return matching
            # Explicit names may be supplied by a custom canonical analyzer.
            matching = self.compose_names.get(repository_id, {}).get(str(name), set())
            return sorted(matching)
        if name:
            return sorted(self.compose_names.get(repository_id, {}).get(str(name), set()))
        paths = [component] if isinstance(component, str) else []
        if not paths:
            for identifier in _ids(value):
                path = self.evidence_rows.get(repository_id, {}).get(identifier, {}).get("path")
                if _relative_path(path):
                    paths.append(path)
        candidates = []
        for row in services:
            roots = row.get("componentRoots", []) or [_value(row.get("root"))]
            for root in roots:
                if not isinstance(root, str):
                    continue
                if any(
                    path == root or root == "." or path.startswith(root.rstrip("/") + "/") for path in paths
                ):
                    candidates.append((len(root), row["id"]))
        if candidates:
            longest = max(length for length, _ in candidates)
            owners = sorted({sid for length, sid in candidates if length == longest})
            if len(owners) == 1:
                return owners
            key = value.get("environmentKey") or value.get("key")
            if key:
                bound_owners = {
                    binding["serviceId"]
                    for binding in self.request.get("serviceBindings", [])
                    if binding.get("serviceId") in owners
                    and any(
                        variable.get("key") == key
                        for name in ("runtimeEnv", "buildEnv", "secretRefs")
                        for variable in binding.get(name, [])
                    )
                }
                if len(bound_owners) == 1:
                    return sorted(bound_owners)
            return owners
        # A one-service repository gives collection observations one possible
        # consumer; multiple services never inherit each other's dependencies.
        return [services[0]["id"]] if len(services) == 1 else []

    def classify(self, repository_id):
        record = self.records[repository_id]
        classification = record.get("classification", {})
        kind = classification.get("kind")
        refs = self.refs(repository_id, classification)
        if kind in {"infrastructure", "package", "documentation"} and refs:
            return {
                "kind": kind,
                "status": classification.get("status", "detected"),
                "reason": classification.get("reason", "Source-grounded repository classification."),
                "evidenceRefs": refs,
            }
        services = self.services(repository_id)
        if services:
            return {
                "kind": "application",
                "status": "detected",
                "reason": "Canonical analysis identified application execution components.",
                "evidenceRefs": sorted({ref for row in services for ref in row["evidenceRefs"]}),
            }
        libraries = [row for row in self.by_repository.get(repository_id, []) if row["kind"] == "package"]
        if libraries:
            return {
                "kind": "package",
                "status": "detected",
                "reason": "Canonical source analysis classified a component as a shared library or package.",
                "evidenceRefs": sorted({ref for row in libraries for ref in row["evidenceRefs"]}),
            }
        infrastructure = [
            row
            for row in self.evidence_rows.get(repository_id, {}).values()
            if isinstance(row.get("path"), str) and row["path"].endswith((".tf", ".tf.json"))
        ]
        if infrastructure:
            source_captured = any(not row.get("manifestOnly") for row in infrastructure)
            return {
                "kind": "infrastructure",
                "status": "detected" if source_captured else "suggested",
                "reason": "Captured Terraform configuration; resources have not been applied or imported."
                if source_captured
                else "Pinned manifest contains Terraform files; their resource declarations have not been inspected.",
                "evidenceRefs": self.refs(repository_id, infrastructure),
            }
        manifest = (record.get("buildSourceManifest") or {}).get("files", [])
        document_paths = [row.get("path") for row in manifest if isinstance(row, dict)]
        if document_paths and all(
            isinstance(path, str)
            and (
                path.lower().endswith(
                    (".md", ".rst", ".adoc", ".txt", ".png", ".jpg", ".jpeg", ".svg", ".pdf")
                )
                or PurePosixPath(path).name in {"LICENSE", ".gitignore", ".gitattributes"}
            )
            for path in document_paths
        ):
            evidence = [
                row for row in self.evidence_rows.get(repository_id, {}).values() if row.get("manifestOnly")
            ]
            return {
                "kind": "documentation",
                "status": "suggested",
                "reason": "Pinned manifest contains documentation assets only; repository purpose has not been confirmed from source content.",
                "evidenceRefs": self.refs(repository_id, evidence),
            }
        return {
            "kind": "unknown",
            "status": "unknown",
            "reason": "No supported execution component or source-grounded repository classification was provided.",
            "evidenceRefs": [],
        }

    def prepare(self):
        selected = set(self.request.get("selectedServiceIds", []))
        if not str(self.request.get("purpose") or "").strip():
            self.question(
                "purpose",
                "Describe the intended system before selecting repositories or generating a deployable system plan.",
            )
        for repository_id, record in sorted(self.records.items()):
            raw_evidence = record.get("evidence", (record.get("bundle") or {}).get("evidence", []))
            self.evidence_rows[repository_id] = {
                row["evidenceId"]: row
                for row in raw_evidence
                if isinstance(row, dict) and isinstance(row.get("evidenceId"), str)
            }
            for row in (record.get("buildSourceManifest") or {}).get("files", []):
                if not isinstance(row, dict) or not _relative_path(row.get("path")):
                    continue
                identifier = "manifest-" + digest({"path": row["path"], "sha256": row.get("sha256")})[:24]
                self.evidence_rows[repository_id][identifier] = {
                    "evidenceId": identifier,
                    "path": row["path"],
                    "startLine": None,
                    "endLine": None,
                    "manifestOnly": True,
                }
            self.by_repository[repository_id] = []
            if record.get("status") != "analyzed" or not isinstance(record.get("analysis"), dict):
                self.limitations.add(
                    f"{record['fullName']}: repository was {record.get('status', 'not analyzed')}."
                )
                if record.get("status") == "failed" and (
                    not selected or any(sid.startswith(f"repo-{repository_id}--") for sid in selected)
                ):
                    self.question(
                        f"repository-coverage-{repository_id}",
                        f"Restore source access or explicitly exclude {record['fullName']} from the selected system; its analysis failed.",
                    )
                continue
            analysis = record["analysis"]
            for service in analysis.get("services", []):
                source_id = service["serviceId"]
                identifier = service_component_id(repository_id, source_id)
                role = _value(service.get("role"))
                kind = "package" if role in _LIBRARY_ROLES else "service"
                status = (service.get("role") or {}).get("status", "unknown")
                component = {
                    **copy.deepcopy(service),
                    "id": identifier,
                    "repositoryId": repository_id,
                    "sourceServiceId": source_id,
                    "name": f"{record['fullName']}:{source_id}",
                    "kind": kind,
                    "deployable": kind == "service" and role in _APPLICATION_ROLES,
                    "selected": (not selected or identifier in selected) and kind == "service",
                    "status": status,
                    "evidenceRefs": self.refs(repository_id, service),
                }
                self.components[identifier] = component
                self.by_repository[repository_id].append(component)
            readiness = record.get("readiness") or {}
            names = self.compose_names.setdefault(repository_id, {})
            for target in readiness.get("buildTargets", []):
                if target.get("serviceName") and target.get("component"):
                    source_id = (
                        "svc-" + digest({"service": target["serviceName"], "root": target["component"]})[:16]
                    )
                    identifier = service_component_id(repository_id, source_id)
                    if identifier in self.components:
                        names.setdefault(target["serviceName"], set()).add(identifier)
            for limitation in analysis.get("coverage", {}).get("limitations", []):
                self.limitations.add(f"{record['fullName']}: {limitation}")
            for question in analysis.get("questions", []):
                required, service_id = self.source_question_requirement(repository_id, question)
                self.question(
                    f"repo-{repository_id}-{question['key']}",
                    question["reason"],
                    service_id=service_id,
                    kind=question.get("kind", "code_review"),
                    required=required,
                )
            self.source_findings(repository_id)
        for identifier in selected - set(self.components):
            self.question(
                "selection-" + digest(identifier)[:12],
                "Selected service does not exist in the pinned organization graph.",
            )
        for repository_id, record in sorted(self.records.items()):
            classification = self.classify(repository_id)
            if classification["kind"] in {"infrastructure", "documentation", "package"}:
                identifier = _dns_id(f"repository-{repository_id}")
                self.components[identifier] = {
                    "id": identifier,
                    "repositoryId": repository_id,
                    "sourceServiceId": None,
                    "name": record["fullName"],
                    "kind": classification["kind"],
                    "role": {"value": classification["kind"], "status": classification["status"]},
                    "deployable": False,
                    "selected": False,
                    "status": classification["status"],
                    "evidenceRefs": classification["evidenceRefs"],
                }
            if record.get("status") == "analyzed" and classification["kind"] == "unknown":
                self.question(
                    f"repository-role-{repository_id}",
                    f"Confirm the role of {record['fullName']}; no supported deployable application was identified.",
                    required=False,
                )
        for identifier in selected & set(self.components):
            if self.components[identifier]["kind"] != "service":
                self.question(
                    "non-application-selection-" + digest(identifier)[:12],
                    "Selected component is a non-application repository or external dependency; select application execution components instead.",
                )

    def source_question_requirement(self, repository_id, question):
        key = question["key"]
        selected_services = [row for row in self.services(repository_id) if row["selected"]]
        if key.startswith("coverage."):
            return bool(selected_services), None
        for service in selected_services:
            prefix = service["sourceServiceId"] + "."
            if not key.startswith(prefix):
                continue
            field = key[len(prefix) :]
            binding = next(
                (
                    row
                    for row in self.request.get("serviceBindings", [])
                    if row.get("serviceId") == service["id"]
                ),
                {},
            )
            binding_field = "port" if field == "ports" else field
            if binding.get(binding_field) is not None:
                return False, service["id"]
            # Build/start commands can be discovered by the selected builder;
            # source identity and execution port cannot be guessed that way.
            return field in {"root", "role", "runtime", "ports", "support", "workingDirectory"}, service["id"]
        # Collection ownership and target obligations are represented by the
        # system graph and the original deployment planner's scoped bindings.
        return False, None

    def source_findings(self, repository_id):
        record = self.records[repository_id]
        selected_services = [row for row in self.services(repository_id) if row["selected"]]
        if not selected_services:
            return
        readiness_findings = (record.get("readiness") or {}).get("findings", [])
        verification = (record.get("runReport") or {}).get("verification") or {}
        if not isinstance(verification, dict):
            verification = {}
        findings = [row for row in readiness_findings if row.get("severity") == "error"]
        findings.extend(
            row
            for row in verification.get("reviewFindings", [])
            if row.get("blocking") is True and row.get("decision") == "supported"
        )
        for index, finding in enumerate(findings):
            self.refs(repository_id, finding)
            owners = self.owner_candidates(repository_id, finding)
            self.question(
                f"source-review-{repository_id}-{index}",
                finding.get(
                    "reason", "Resolve the source-grounded blocking finding before system deployment."
                ),
                owners[0] if len(owners) == 1 else None,
                kind="code_review",
            )

    def register_endpoints(self):
        for binding in self.request.get("serviceBindings", []):
            sid = binding.get("serviceId")
            if sid not in self.components:
                continue
            values = [binding.get(key) for key in ("endpoint", "baseUrl", "url")]
            host = binding.get("ingressHost") or binding.get("host")
            ingress = binding.get("ingress")
            if isinstance(ingress, dict):
                host = host or ingress.get("host")
            if host and isinstance(host, str):
                # Host-only input must declare protocol; silently guessing TLS
                # would associate an endpoint the user did not bind.
                scheme = binding.get("protocol") or binding.get("scheme")
                if scheme in {"http", "https"}:
                    values.append(
                        f"{scheme}://{host}" + (f":{binding['port']}" if binding.get("port") else "")
                    )
            for value in values:
                endpoint = _endpoint(value)
                if endpoint:
                    self.endpoints.setdefault(endpoint, set()).add(sid)
        for repository_id, record in self.records.items():
            for route in (record.get("analysis") or {}).get("apiRoutes", []):
                value = _value(route)
                if not isinstance(value, dict) or route.get("status") != "detected":
                    continue
                values = [value.get(key) for key in ("baseUrl", "serverUrl")]
                values.extend(
                    server.get("url") for server in value.get("servers", []) if isinstance(server, dict)
                )
                owners = self.owner_candidates(repository_id, {**route, **value})
                if len(owners) != 1:
                    continue
                for candidate in values:
                    endpoint = _endpoint(candidate)
                    if endpoint:
                        self.endpoints.setdefault(endpoint, set()).add(owners[0])
                        self.endpoint_refs.setdefault(endpoint, set()).update(self.refs(repository_id, route))

    def explicit_connections(self):
        for index, binding in enumerate(self.request.get("connectionBindings", [])):
            source, target = binding.get("fromServiceId"), binding.get("toServiceId")
            if source not in self.components or target not in self.components:
                self.question(
                    f"connection-binding-{index}",
                    "Connection binding references a service absent from the pinned system graph.",
                    source if source in self.components else None,
                )
                continue
            self.relationship(
                source,
                target,
                binding.get("kind", "http"),
                "user_confirmed",
                key=binding.get("environmentKey"),
                phase=binding.get("phase", "runtime"),
                reason="Explicit user binding between qualified organization component identities.",
            )

    def external_dependencies(self, repository_id: str):
        record = self.records[repository_id]
        for index, field in enumerate((record.get("analysis") or {}).get("dependencies", [])):
            value = _value(field)
            if not isinstance(value, dict):
                continue
            kind, refs = _kind(value), self.refs(repository_id, field)
            name = str(value.get("name") or value.get("engine") or f"dependency-{index}")
            identifier = _dns_id(f"resource-repo-{repository_id}-{kind}-{name}")
            if identifier not in self.components:
                self.components[identifier] = {
                    "id": identifier,
                    "repositoryId": repository_id,
                    "sourceServiceId": None,
                    "name": name,
                    "kind": kind,
                    "role": {"value": kind, "status": field.get("status", "unknown")},
                    "deployable": False,
                    "selected": False,
                    "status": field.get("status", "unknown"),
                    "scope": field.get("scope", "source"),
                    "dependency": copy.deepcopy(value),
                    "evidenceRefs": refs,
                    "provisioning": "unbound",
                }
            else:
                previous = self.components[identifier]
                previous["evidenceRefs"] = sorted(set(previous["evidenceRefs"]) | set(refs))
                if field.get("scope") == "container":
                    previous["scope"] = "container"
            owners = self.owner_candidates(repository_id, {**field, **value})
            if field.get("scope") == "container":
                # Compose declaring a DB or volume proves its existence, but
                # not that every app consumes it; serviceConnections does that.
                continue
            for owner in owners:
                self.relationship(
                    owner,
                    identifier,
                    kind,
                    "suggested",
                    refs,
                    phase="runtime" if kind != "package" else "build",
                    reason="Declared dependency does not establish a required live external resource.",
                )
            if owners:
                self.question(
                    f"dependency-binding-{identifier}",
                    f"Confirm whether declared dependency {name} requires a live resource and bind it explicitly.",
                    owners[0] if len(owners) == 1 else None,
                    owners,
                    kind="dependency",
                )

    def compose_connections(self, repository_id: str):
        record = self.records[repository_id]
        for index, item in enumerate((record.get("readiness") or {}).get("serviceConnections", [])):
            refs = self.refs(repository_id, item)
            owners = self.owner_candidates(repository_id, item)
            if len(owners) != 1:
                self.question(
                    f"compose-owner-{repository_id}-{index}",
                    "Compose connection consumer is ambiguous; select its source execution component.",
                    candidates=owners,
                )
                continue
            target_name = item.get("toService")
            targets = set(self.compose_names.get(repository_id, {}).get(str(target_name), set()))
            targets.update(
                component["id"]
                for component in self.components.values()
                if component["repositoryId"] == repository_id
                and component["kind"] in {"database", "queue", "storage", "package"}
                and component["name"] == target_name
                and component.get("scope") == "container"
            )
            source = owners[0]
            if len(targets) == 1:
                target = next(iter(targets))
                target_kind = self.components[target]["kind"]
                kind = target_kind if target_kind in _RELATION_KINDS else "http"
                self.relationship(
                    source,
                    target,
                    kind,
                    "detected",
                    refs,
                    key=item.get("environmentKey"),
                    phase="runtime",
                    reason="Source Compose metadata explicitly connects the consumer and named target in the same repository.",
                )
                if target_kind != "service":
                    self.question(
                        f"resource-binding-{target}",
                        f"Bind observed {target_kind} resource {target_name}; source Compose is not a provisioning authorization.",
                        source,
                        kind="dependency",
                    )
            else:
                self.relationship(
                    source,
                    None,
                    "http",
                    "unknown",
                    refs,
                    key=item.get("environmentKey"),
                    phase="runtime",
                    reason="Compose target has no unique analyzed component or external dependency node.",
                )
                self.question(
                    f"compose-target-{repository_id}-{index}",
                    f"Resolve Compose target {target_name} within its source repository or provide an explicit organization binding.",
                    source,
                    targets,
                    kind="connection",
                )

    def http_connections(self, repository_id: str):
        record = self.records[repository_id]
        for index, field in enumerate((record.get("analysis") or {}).get("connections", [])):
            value = _value(field)
            if not isinstance(value, dict):
                continue
            owners = self.owner_candidates(repository_id, {**field, **value})
            if len(owners) != 1:
                self.question(
                    f"http-owner-{repository_id}-{index}",
                    "HTTP connection has no unique consumer; source evidence does not identify one execution component.",
                    candidates=owners,
                )
                continue
            source, refs = owners[0], self.refs(repository_id, field)
            key = value.get("environmentKey")
            # User bindings win even if historic source contains localhost URLs.
            bound = [
                row
                for row in self.relationships.values()
                if row["fromServiceId"] == source
                and row["kind"] == "http"
                and row["status"] == "user_confirmed"
                and (key is None or row["environmentKey"] == key)
            ]
            if len(bound) == 1:
                bound[0]["evidenceRefs"] = sorted(set(bound[0]["evidenceRefs"]) | set(refs))
                continue
            endpoint = _endpoint(value.get("baseUrl"))
            targets = set(self.endpoints.get(endpoint, set())) - {source}
            if len(targets) == 1 and field.get("status") == "detected":
                self.relationship(
                    source,
                    next(iter(targets)),
                    "http",
                    "detected",
                    set(refs) | self.endpoint_refs.get(endpoint, set()),
                    key=key,
                    phase=value.get("phase", "unknown"),
                    reason="Observed HTTP base URL exactly matches an explicitly bound or source-declared service URL.",
                    url=value.get("baseUrl"),
                )
            else:
                self.relationship(
                    source,
                    None,
                    "http",
                    "unknown",
                    refs,
                    key=key,
                    phase=value.get("phase", "unknown"),
                    reason="Observed HTTP connection has no unique verified target in the organization.",
                    url=value.get("baseUrl"),
                )
                candidates = targets or {
                    component["id"]
                    for component in self.components.values()
                    if component["kind"] == "service"
                    and component["id"] != source
                    and _value(component.get("role")) in _HTTP_ROLES
                }
                self.question(
                    f"http-target-{repository_id}-{index}",
                    "Bind the HTTP target explicitly; service names and matching ports cannot establish repository relationships.",
                    source,
                    candidates,
                    kind="connection",
                )

    def environment_connections(self, repository_id: str):
        record = self.records[repository_id]
        for index, item in enumerate((record.get("readiness") or {}).get("environmentVariables", [])):
            key = item.get("key", "")
            if not re.search(r"(?:API|BACKEND|SERVICE).*(?:URL|HOST|ORIGIN)", key, re.I):
                continue
            owners = self.owner_candidates(repository_id, item)
            if len(owners) != 1:
                self.question(
                    f"environment-owner-{repository_id}-{index}",
                    f"Confirm the consumer of {key} before binding a system connection.",
                    candidates=owners,
                )
                continue
            source = owners[0]
            if any(
                row["fromServiceId"] == source
                and row["environmentKey"] == key
                and row["status"] in {"user_confirmed", "detected"}
                for row in self.relationships.values()
            ):
                continue
            candidates = {
                component["id"]
                for component in self.components.values()
                if component["kind"] == "service"
                and component["id"] != source
                and _value(component.get("role")) in _HTTP_ROLES
            }
            target = next(iter(candidates)) if len(candidates) == 1 else None
            self.relationship(
                source,
                target,
                "http",
                "suggested" if target else "unknown",
                self.refs(repository_id, item),
                key=key,
                phase=item.get("phase", "unknown"),
                reason="Environment key suggests an HTTP consumer; target identity requires explicit confirmation.",
            )
            self.question(
                f"environment:{source}:{key}",
                f"Select the service consumed through {key} and confirm its build/runtime phase.",
                source,
                candidates,
            )

    def export(self):
        repositories = []
        for repository_id, record in sorted(self.records.items()):
            repositories.append(
                {
                    "repositoryId": repository_id,
                    "fullName": record["fullName"],
                    "repositoryUrl": record.get("repositoryUrl"),
                    "ref": record.get("ref"),
                    "commitSha": record.get("commitSha"),
                    "sourceSnapshotId": record.get("sourceSnapshotId"),
                    "status": record.get("status", "failed"),
                    "classification": self.classify(repository_id),
                }
            )
        return {
            "schemaVersion": "iris.system-graph.v1",
            "organization": self.request.get("organization"),
            "purpose": self.request.get("purpose"),
            "environment": self.request.get("environment", "test"),
            "repositories": repositories,
            "components": sorted(self.components.values(), key=lambda row: row["id"]),
            "relationships": sorted(self.relationships.values(), key=lambda row: row["id"]),
            "questions": sorted(self.questions.values(), key=lambda row: row["key"]),
            "limitations": sorted(self.limitations),
            "evidence": sorted(self.evidence.values(), key=lambda row: row["id"]),
        }


def build_system_graph(records: list[dict], request: dict) -> dict:
    """Combine source analyses without executing code or inferring provisioned resources."""
    graph = _Graph(records, request)
    graph.prepare()
    # Dependency nodes must exist before Compose consumer metadata is resolved.
    for repository_id in sorted(graph.records):
        if graph.records[repository_id].get("status") == "analyzed":
            graph.external_dependencies(repository_id)
    # Explicit bindings may target an observed external dependency, whose ID
    # must be known before binding validation and HTTP endpoint registration.
    graph.register_endpoints()
    graph.explicit_connections()
    for repository_id in sorted(graph.records):
        if graph.records[repository_id].get("status") != "analyzed":
            continue
        graph.compose_connections(repository_id)
        graph.http_connections(repository_id)
        graph.environment_connections(repository_id)
    return graph.export()
