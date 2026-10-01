"""Atomically admit source-supported proposals; never trust model confidence."""

from copy import deepcopy
from pathlib import PurePosixPath

from ..contracts import AnalyzerError, canonical_bytes, digest
from ..preprocess.snapshot import SOURCE_EXTENSIONS
from .rules.express import unique_route_anchor, verify_listener, verify_route
from .rules.findings import audit_baseline, verify_finding
from .source import ImmutableSource

VERIFIER_VERSION = "iris.semantic.v1"
_SERVICE_KEYS = {
    "runtime": "runtime.name",
    "buildCommand": "build.command",
    "startCommand": "start.command",
    "workingDirectory": "docker.workdir",
    "outputDirectory": "output.directory",
    "ports": "runtime.port",
    "healthchecks": "healthcheck.path",
}
_COLLECTION_KEYS = {
    "dependencies": {"dependency.database", "dependency.volume"},
    "apiRoutes": {"api.route"},
    "environmentKeys": {"environment.key"},
    "connections": {"frontend.connection"},
}


def _field_unknown() -> dict:
    return {
        "value": None,
        "status": "unknown",
        "scope": "source",
        "evidenceIds": [],
        "reason": "Model proposal not admitted by semantic verification.",
    }


def _semantic_decision(field, name, candidate, immutable, bundle):
    ids = field["evidenceIds"]
    paths = immutable.paths(ids)
    if not ids or any(i not in immutable.evidence for i in ids):
        return "rejected", "EVIDENCE_REFERENCE_INVALID", "iris.evidence.v1", paths
    if name == "apiRoutes":
        decision, code, inspected = verify_route(field, immutable, bundle["componentRoots"])
        return decision, code, "iris.express.const-route.v1", inspected
    keys = {_SERVICE_KEYS[name]} if name in _SERVICE_KEYS else _COLLECTION_KEYS.get(name, set())
    if name in {"root", "role"}:
        if (
            candidate
            and any(
                c["candidateId"] == candidate["candidateId"] and c[name] == candidate[name]
                for c in immutable.candidates
            )
            and field["scope"] == "source"
            and field["value"] == candidate[name]
            and set(ids) <= set(candidate["evidenceIds"])
        ):
            return "supported", "SOURCE_RELATION_VERIFIED", "iris.candidate.identity.v1", paths
        return "rejected", "SERVICE_IDENTITY_MISMATCH", "iris.candidate.identity.v1", paths
    for fact in immutable.facts:
        if fact["key"] not in keys or fact["scope"] != field["scope"] or fact.get("condition"):
            continue
        if candidate and (
            fact.get("component", ".") not in {*candidate["componentRoots"], candidate["root"]}
            or fact.get("candidateId", candidate["candidateId"]) != candidate["candidateId"]
        ):
            continue
        value = deepcopy(fact["value"])
        values = [value]
        if name in _COLLECTION_KEYS and isinstance(value, dict):
            value = {**value, "component": fact.get("component", ".")}
            if fact["key"].startswith("dependency."):
                value["kind"] = fact["key"].split(".", 1)[1]
            values.append(value)
        if any(
            canonical_bytes(v) == canonical_bytes(field["value"]) for v in values
        ) and immutable.supports_fact(ids, fact):
            source_paths = sorted(
                {
                    immutable.fact_evidence[eid]["path"]
                    for eid in fact["evidenceIds"]
                    if eid in immutable.fact_evidence
                    and PurePosixPath(immutable.fact_evidence[eid]["path"]).suffix in SOURCE_EXTENSIONS
                }
            )
            if source_paths and name == "ports":
                decision, code, inspected = verify_listener(field, immutable, paths=source_paths)
                return decision, code, "iris.express.listener.v1", inspected
            if source_paths and name == "healthchecks":
                route_field = {
                    **field,
                    "value": {
                        "method": "GET",
                        "path": field["value"],
                        "component": fact.get("component", "."),
                    },
                    "scope": "source",
                }
                decision, code, inspected = verify_route(route_field, immutable, bundle["componentRoots"])
                return decision, code, "iris.express.health-route.v1", inspected
            return "supported", "SOURCE_RELATION_VERIFIED", "iris.fact." + fact["key"] + ".v1", paths
    if any(path in immutable.unavailable for path in paths):
        return "deferred", "FULL_SOURCE_UNAVAILABLE", "iris.evidence.v1", paths
    if name == "dependencies":
        return "rejected", "EVIDENCE_PREDICATE_MISMATCH", "iris.database.declaration.v1", paths
    return "deferred", "SEMANTIC_RULE_UNRESOLVED", "iris.fact-match.v1", paths


def verify_proposals(reply: dict, bundle: dict) -> tuple[dict, dict]:
    """Return a model-shaped admitted reply and a source-bound audit sidecar.

    Callers still validate structural identity/detected fields. This function
    also checks hashes so a direct caller cannot replay a different context.
    It never reads the live checkout and never imports or executes target code.
    """
    from ..result import static_analysis

    if reply.get("kind") != "analysis":
        raise AnalyzerError("RESULT_SCHEMA_INVALID", "Semantic admission requires an analysis reply")
    baseline = static_analysis(bundle)
    model = reply["result"]
    if (
        model["sourceSnapshotId"] != bundle["source"]["snapshotId"]
        or model["contextHash"] != bundle["contextHash"]
    ):
        raise AnalyzerError(
            "RESULT_CONTEXT_MISMATCH", "Model proposal belongs to another immutable source context"
        )
    immutable = ImmutableSource(bundle)
    sanitized = deepcopy(reply)
    sanitized.pop("reviewFindings", None)
    report = {
        "schemaVersion": "iris.analysis-verification.v1",
        "verifierVersion": VERIFIER_VERSION,
        "sourceSnapshotId": bundle["source"]["snapshotId"],
        "contextHash": bundle["contextHash"],
        "baselineDigest": digest(baseline),
        "proposalDigest": digest(reply),
        "decisions": [],
        "reviewFindings": [],
        "resolvedObligations": [],
        "unavailableSourcePaths": immutable.unavailable,
        "limitations": [
            "Only selected immutable source files and supported declaration rules are checked; no runtime behavior or whole-repository correctness is established."
        ],
    }
    candidates = {c["candidateId"]: c for c in bundle["deploymentCandidates"]}

    def verify(field, name, path, candidate=None):
        if field["status"] == "unknown" and name in {*_COLLECTION_KEYS, "ports", "healthchecks"}:
            existing_items = (
                baseline.get(name, [])
                if candidate is None
                else next(
                    service[name]
                    for service in baseline["services"]
                    if service["serviceId"] == candidate["candidateId"]
                )
            )
            existing = any(canonical_bytes(item) == canonical_bytes(field) for item in existing_items)
            report["decisions"].append(
                {
                    "fieldPath": path,
                    "decision": "supported" if existing else "deferred",
                    "ruleId": "iris.obligation.baseline.v1",
                    "reasonCode": "BASELINE_OBLIGATION_PRESERVED"
                    if existing
                    else "UNVERIFIED_COLLECTION_OBLIGATION",
                    "reason": "Existing source uncertainty preserved."
                    if existing
                    else "An optional unknown collection entry does not establish a new blocking source obligation.",
                    "evidenceIds": list(field["evidenceIds"]),
                    "proposedDigest": digest(field),
                    "inspectedPaths": [],
                    "missingObligations": []
                    if existing
                    else [
                        "Identify a verified source obligation before blocking on this unknown collection entry."
                    ],
                }
            )
            return existing
        if field["status"] != "suggested":
            return True
        decision, code, rule, inspected = _semantic_decision(field, name, candidate, immutable, bundle)
        reason = (
            "Verified a narrow source declaration in its stated scope; execution, connection success and required deployment capacity are not established."
            if decision == "supported"
            else "The supplied source does not establish this proposed relationship; the value is excluded from admitted analysis."
        )
        report["decisions"].append(
            {
                "fieldPath": path,
                "decision": decision,
                "ruleId": rule,
                "reasonCode": code,
                "reason": reason,
                "evidenceIds": list(field["evidenceIds"]),
                "proposedDigest": digest(field),
                "inspectedPaths": inspected,
                "supportingLocators": [
                    {
                        "path": inspected_path,
                        "sourceDigest": next(
                            m["digest"] for m in bundle["manifest"] if m["path"] == inspected_path
                        ),
                        "startLine": 1,
                        "endLine": max(1, len(immutable.files[inspected_path].splitlines())),
                    }
                    for inspected_path in inspected
                    if inspected_path in immutable.files
                ],
                "missingObligations": []
                if decision == "supported"
                else [
                    "Provide a source-supported relationship in the correct service/scope, or retain it as unverified review."
                ],
            }
        )
        if decision == "supported":
            field["reason"] = reason
            if name == "apiRoutes" and not any(
                entry["scope"] == field["scope"]
                and canonical_bytes(entry["value"]) == canonical_bytes(field["value"])
                for entry in baseline["apiRoutes"]
            ):
                for obligation in bundle["unresolved"]:
                    if (
                        obligation["key"] != "runtime.httpRoutes"
                        or obligation["reason"] != "Express route path cannot be resolved statically"
                        or obligation.get("component", ".") != field["value"].get("component", ".")
                        or not obligation.get("evidenceIds")
                        or any(eid not in immutable.evidence for eid in obligation["evidenceIds"])
                        or {immutable.evidence[eid]["path"] for eid in obligation["evidenceIds"]}
                        != {obligation.get("path")}
                    ):
                        continue
                    anchored = {**field, "evidenceIds": list(obligation["evidenceIds"])}
                    if (
                        unique_route_anchor(anchored, immutable)
                        and verify_route(anchored, immutable, bundle["componentRoots"])[0] == "supported"
                    ):
                        identifier = digest(obligation)
                        if not any(
                            item["obligationDigest"] == identifier for item in report["resolvedObligations"]
                        ):
                            report["resolvedObligations"].append(
                                {
                                    "obligationDigest": identifier,
                                    "key": obligation["key"],
                                    "reason": obligation["reason"],
                                    "path": obligation["path"],
                                    "component": obligation.get("component", "."),
                                    "evidenceIds": list(obligation["evidenceIds"]),
                                    "fieldPath": path,
                                    "ruleId": "iris.express.const-route.v1",
                                    "decision": "supported",
                                }
                            )
        return decision == "supported"

    for i, service in enumerate(sanitized["result"]["services"]):
        candidate = candidates.get(service["serviceId"])
        if candidate is None:
            raise AnalyzerError(
                "RESULT_OBSERVATION_INVALID", "Proposal references an unknown application service"
            )
        for name in ("root", "role", *_SERVICE_KEYS):
            if name not in service:
                continue
            value = service[name]
            if isinstance(value, list):
                service[name] = [
                    field
                    for j, field in enumerate(value)
                    if verify(field, name, ["result", "services", i, name, j], candidate)
                ]
            elif not verify(value, name, ["result", "services", i, name], candidate):
                service[name] = _field_unknown()
    for name in _COLLECTION_KEYS:
        sanitized["result"][name] = [
            field
            for i, field in enumerate(sanitized["result"][name])
            if verify(field, name, ["result", name, i])
        ]
    baseline_questions = {(q["key"], q["kind"]): q for q in baseline["questions"]}
    admitted_questions = []
    for index, question in enumerate(sanitized["result"]["questions"]):
        existing = baseline_questions.get((question["key"], question["kind"]))
        report["decisions"].append(
            {
                "fieldPath": ["result", "questions", index],
                "decision": "supported" if existing else "deferred",
                "ruleId": "iris.obligation.baseline.v1",
                "reasonCode": "BASELINE_OBLIGATION_PRESERVED" if existing else "REVIEW_RELATION_UNRESOLVED",
                "reason": "Existing source obligation preserved."
                if existing
                else "Additional model question requires source verification before it can block execution.",
                "evidenceIds": [],
                "proposedDigest": digest(question),
                "inspectedPaths": [],
                "missingObligations": [] if existing else ["Confirm a source-grounded blocking obligation."],
            }
        )
        if existing:
            admitted_questions.append(deepcopy(existing))
    sanitized["result"]["questions"] = admitted_questions
    # Narrative text is a raw model assertion too; only pre-existing bounded
    # coverage limitations belong in the canonical, source-grounded result.
    for index, limitation in enumerate(sanitized["result"]["coverage"]["limitations"]):
        if limitation not in baseline["coverage"]["limitations"]:
            report["decisions"].append(
                {
                    "fieldPath": ["result", "coverage", "limitations", index],
                    "decision": "deferred",
                    "ruleId": "iris.coverage.baseline.v1",
                    "reasonCode": "REVIEW_RELATION_UNRESOLVED",
                    "reason": "Model narrative limitation is retained only in the raw proposal until independently verified.",
                    "evidenceIds": [],
                    "proposedDigest": digest(limitation),
                    "inspectedPaths": [],
                    "missingObligations": ["Provide structured evidence for a source coverage limitation."],
                }
            )
    sanitized["result"]["coverage"]["limitations"] = [
        value
        for value in sanitized["result"]["coverage"]["limitations"]
        if value in baseline["coverage"]["limitations"]
    ]
    audit = audit_baseline(baseline, bundle, immutable)
    report["reviewFindings"] = list(audit)
    for item in reply.get("reviewFindings", []):
        if any(e not in immutable.evidence for e in item["evidenceIds"]):
            raise AnalyzerError(
                "RESULT_EVIDENCE_INVALID", "Review finding references evidence outside the immutable context"
            )
        report["reviewFindings"].append(verify_finding(item, baseline, bundle, immutable, audit))
    for decision in report["decisions"]:
        if decision["decision"] == "rejected":
            report["reviewFindings"].append(
                {
                    "category": "missing_evidence",
                    "reason": "An individual model claim cites source that does not support the proposed relationship; baseline observations are preserved.",
                    "evidenceIds": decision["evidenceIds"],
                    "decision": "supported",
                    "ruleId": "iris.review.rejected-proposal.v1",
                    "reasonCode": decision["reasonCode"],
                    "blocking": False,
                    "origin": "verifier",
                    "inspectedPaths": decision["inspectedPaths"],
                }
            )
    return sanitized, report
