"""Narrow, platform-generated review findings; model prose is never proof."""

import re
import shlex
from pathlib import PurePosixPath

from ...preprocess.selector import component_of
from ...preprocess.snapshot import SOURCE_EXTENSIONS
from .express import suspicious_listener


def _finding(category, reason, evidence, *, blocking=False, inspected=None):
    return {
        "category": category,
        "reason": reason,
        "evidenceIds": sorted(set(evidence)),
        "decision": "supported",
        "ruleId": "iris.review." + category + ".v1",
        "reasonCode": "SOURCE_RELATION_VERIFIED",
        "blocking": blocking,
        "origin": "verifier",
        "inspectedPaths": sorted(set(inspected or [])),
    }


def _image_runtime(raw, target):
    stages, named = [], {}
    try:
        for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            if not re.match(r"^\s*FROM\s+", line, re.I):
                continue
            tokens = shlex.split(line)
            index = 1
            if tokens[index].startswith("--platform="):
                index += 1
            image = tokens[index]
            inherited = named.get(image.lower())
            runtime = (
                inherited[0] if inherited else image.rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
            )
            stage = (runtime, number)
            stages.append(stage)
            named[str(len(stages) - 1)] = stage
            if len(tokens) > index + 2 and tokens[index + 1].upper() == "AS":
                named[tokens[index + 2].lower()] = stage
        selected = named.get(target.lower()) if target else stages[-1] if stages else None
        return (
            selected
            if selected and selected[0] in {"node", "nginx", "python", "bun", "deno", "httpd"}
            else None
        )
    except (UnicodeError, ValueError, IndexError):
        return None


def audit_baseline(baseline: dict, bundle: dict, immutable) -> list[dict]:
    findings = []
    targets = [
        f
        for f in immutable.facts
        if f["key"] == "deployment.build_target"
        and f["value"].get("dockerfilePath")
        and not f["value"].get("condition")
    ]
    for service in baseline["services"]:
        matching = [
            f for f in targets if f.get("component") in {*service["componentRoots"], service["root"]["value"]}
        ]
        selections = {(f["value"]["dockerfilePath"], f["value"].get("target")) for f in matching}
        if len(selections) != 1:
            continue
        path, target = next(iter(selections))
        if path not in immutable.files:
            continue
        runtime = _image_runtime(immutable.files[path], target)
        if runtime and (
            service["runtime"]["value"] != runtime[0] or service["runtime"]["scope"] != "container"
        ):
            ids = [e["evidenceId"] for e in bundle["evidence"] if e["path"] == path]
            if ids:
                findings.append(
                    {
                        **_finding(
                            "runtime_stage_mismatch",
                            "Selected Docker stage declares "
                            + runtime[0]
                            + "; the canonical runtime field does not represent that container stage. Source/build observations remain distinct.",
                            ids,
                            blocking=True,
                            inspected=[path],
                        ),
                        "requiredEvidenceLocations": [
                            {"path": path, "startLine": runtime[1], "endLine": runtime[1]}
                        ],
                    }
                )
    for service in baseline["services"]:
        ports = [p for p in service["ports"] if p["scope"] == "container" and p["status"] == "detected"]
        for path in immutable.paths([eid for h in service["healthchecks"] for eid in h["evidenceIds"]]):
            if (
                path in immutable.files
                and PurePosixPath(path).suffix in SOURCE_EXTENSIONS
                and suspicious_listener(immutable, path, frozenset({"get"}))
            ):
                findings.append(
                    _finding(
                        "source_conflict",
                        "A legacy health-route observation has unresolved receiver identity; it cannot establish a deployment probe until the source relationship is verified.",
                        [
                            eid
                            for h in service["healthchecks"]
                            for eid in h["evidenceIds"]
                            if immutable.evidence[eid]["path"] == path
                        ],
                        blocking=True,
                        inspected=[path],
                    )
                )
        for path in immutable.paths([eid for p in ports for eid in p["evidenceIds"]]):
            if (
                path in immutable.files
                and PurePosixPath(path).suffix in SOURCE_EXTENSIONS
                and suspicious_listener(immutable, path)
            ):
                findings.append(
                    _finding(
                        "source_conflict",
                        "A legacy listener observation has a shadowed, mutated or nested receiver; its runtime ownership must be verified before deployment.",
                        [
                            eid
                            for p in ports
                            for eid in p["evidenceIds"]
                            if immutable.evidence[eid]["path"] == path
                        ],
                        blocking=True,
                        inspected=[path],
                    )
                )
        if len({str(p["value"]) for p in ports}) > 1 and any(
            q["key"].startswith(service["serviceId"]) for q in baseline["questions"]
        ):
            findings.append(
                {
                    **_finding(
                        "source_conflict",
                        "The same application's container port declarations disagree; preserve observations until the serving listener is verified.",
                        [eid for p in ports for eid in p["evidenceIds"]],
                        blocking=True,
                        inspected=immutable.paths([eid for p in ports for eid in p["evidenceIds"]]),
                    ),
                    "obligationKeys": [
                        q["key"] for q in baseline["questions"] if q["key"].startswith(service["serviceId"])
                    ],
                }
            )
    return findings


def verify_finding(item: dict, baseline: dict, bundle: dict, immutable, audit: list[dict]) -> dict:
    category, ids = item["category"], item["evidenceIds"]
    paths = immutable.paths(ids)
    result = {
        "category": category,
        "reason": "The supplied evidence does not establish this review finding.",
        "evidenceIds": list(ids),
        "decision": "rejected",
        "ruleId": "iris.review." + category + ".v1",
        "reasonCode": "EVIDENCE_PREDICATE_MISMATCH",
        "blocking": False,
        "origin": "model",
        "inspectedPaths": paths,
    }
    for finding in audit:
        required = finding.get("requiredEvidenceLocations") or [
            {key: immutable.evidence[eid][key] for key in ("path", "startLine", "endLine")}
            for eid in finding["evidenceIds"]
        ]
        if (
            finding["category"] == category
            and required
            and all(
                all(
                    any(
                        immutable.evidence[eid]["path"] == locator["path"]
                        and not immutable.evidence[eid]["redacted"]
                        and immutable.evidence[eid]["startLine"] <= line <= immutable.evidence[eid]["endLine"]
                        for eid in ids
                    )
                    for line in range(locator["startLine"], locator["endLine"] + 1)
                )
                for locator in required
            )
        ):
            return {**finding, "origin": "model"}
    if category == "documentation_mismatch":
        for path in paths:
            if not PurePosixPath(path).name.lower().startswith("readme") or path not in immutable.files:
                continue
            # A model finding must cite the actual conflicting documentation,
            # not merely any nearby README ID. Development instructions are a
            # distinct environment, even when their command/port differ.
            text = "\n".join(
                row["text"]
                for identifier in ids
                if (row := immutable.evidence[identifier])["path"] == path and not row["redacted"]
            )
            text = "\n".join(
                line
                for line in text.splitlines()
                if re.search(r"\b(?:production|container)\b", line, re.I)
                and not re.search(r"\bdevelopment\b", line, re.I)
            )
            component = component_of(path, bundle["componentRoots"])
            commands = re.findall(r"\b(?:uses|runs|starts)\s+`([^`]+)`", text, re.I)
            ports = [int(p) for p in re.findall(r"\b(?:listens\s+on\s+port|port)\s+(\d{1,5})\b", text, re.I)]
            for service in baseline["services"]:
                if component not in {*service["componentRoots"], service["root"]["value"]}:
                    continue
                command = service["startCommand"]
                actual_ports = [
                    p for p in service["ports"] if p["scope"] == "container" and p["status"] == "detected"
                ]
                mismatch = (
                    commands
                    and command["status"] == "detected"
                    and command["scope"] == "container"
                    and command["value"] not in commands
                ) or (ports and actual_ports and not {p["value"] for p in actual_ports} & set(ports))
                source_ids = set(command["evidenceIds"]) | {
                    eid for p in actual_ports for eid in p["evidenceIds"]
                }
                if mismatch and any(immutable.evidence[i]["path"] != path and i in source_ids for i in ids):
                    return {
                        **_finding(
                            category,
                            "README deployment instructions differ from the selected container declarations; this comparison does not establish behavior in other environments.",
                            ids,
                            inspected=paths,
                        ),
                        "origin": "model",
                    }
    if category == "missing_evidence":
        cited_paths = set(paths)
        unresolved = [u for u in bundle["unresolved"] if u.get("path") in cited_paths]
        if unresolved or any(path in immutable.unavailable for path in paths):
            result.update(
                decision="supported",
                reasonCode="SOURCE_OBLIGATION_IDENTIFIED",
                reason="The supplied immutable context leaves a referenced source or configuration obligation unresolved; no runtime value is inferred.",
            )
        elif bundle["unresolved"] and any(
            e["evidenceId"] in ids and e["path"] in paths for e in bundle["evidence"]
        ):
            # Do not infer which missing relation an arbitrary citation refers to.
            result.update(
                decision="deferred",
                reasonCode="REVIEW_RELATION_UNRESOLVED",
                reason="Missing context exists, but this finding's exact dependency relation requires further verification.",
            )
    elif any(path in immutable.unavailable for path in paths):
        result.update(
            decision="deferred",
            reasonCode="FULL_SOURCE_UNAVAILABLE",
            reason="Complete immutable source is unavailable for this review rule.",
        )
    return result
