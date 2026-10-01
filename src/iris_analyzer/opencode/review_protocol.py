"""Small model-authored deltas; source identity is bound by trusted transport."""

from copy import deepcopy

from ..contracts import AnalyzerError

COLLECTIONS = ("apiRoutes", "dependencies", "connections", "environmentKeys")
SERVICE_FIELDS = (
    "runtime",
    "buildCommand",
    "startCommand",
    "workingDirectory",
    "outputDirectory",
    "ports",
    "healthchecks",
)


def make_review_schema(legacy: dict) -> dict:
    changes = []
    types = {
        "apiRoutes": "routeProposal",
        "connections": "connectionProposal",
        "environmentKeys": "textProposal",
    }
    for target in [*COLLECTIONS, *("services." + key for key in SERVICE_FIELDS)]:
        field = target.split(".")[-1]
        name = types.get(
            target,
            "portProposal"
            if field == "ports"
            else "textProposal"
            if target.startswith("services.")
            else "field",
        )
        changes.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "target": {"const": target},
                    "serviceId": {"type": "string", "minLength": 1}
                    if target.startswith("services.")
                    else {"type": "null"},
                    "field": {"$ref": "#/$defs/" + name},
                },
                "required": ["target", "serviceId", "field"],
            }
        )
    review_findings = next(
        row for row in legacy["oneOf"] if row["properties"]["kind"].get("const") == "analysis"
    )["properties"]["reviewFindings"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "iris.model-review.v2: deltas only; trusted adapter attaches source identity after response-parent validation.",
        "oneOf": [
            deepcopy(legacy["oneOf"][0]),
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"const": "review"},
                    "changes": {"type": "array", "maxItems": 30, "items": {"oneOf": changes}},
                    "reviewFindings": deepcopy(review_findings),
                    "questions": {"type": "array", "maxItems": 20, "items": {"$ref": "#/$defs/question"}},
                },
                "required": ["kind", "changes", "reviewFindings", "questions"],
            },
        ],
        "$defs": {
            name: deepcopy(legacy["$defs"][name])
            for name in (
                "field",
                "textProposal",
                "portProposal",
                "routeProposal",
                "connectionProposal",
                "question",
            )
        },
    }


def review_template() -> dict:
    return {"kind": "review", "changes": [], "reviewFindings": [], "questions": []}


def canonicalize_review(wire: dict, bundle: dict) -> dict:
    """Adapt verified transport output; no model hash/status is repaired or trusted."""
    if wire["kind"] == "needs_files":
        return deepcopy(wire)
    from ..result import static_analysis
    from .runner import response_template

    baseline = static_analysis(bundle)
    reply = response_template(bundle)
    reply["reviewFindings"] = deepcopy(wire["reviewFindings"])
    reply["result"]["questions"] = deepcopy(wire["questions"])
    originals = {service["serviceId"]: service for service in baseline["services"]}
    services = {}
    changed = set()
    for change in wire["changes"]:
        target, sid, field = change["target"], change["serviceId"], deepcopy(change["field"])
        if target in COLLECTIONS:
            reply["result"][target].append(field)
            continue
        name = target.split(".", 1)[1]
        if sid not in originals:
            raise AnalyzerError("RESULT_OBSERVATION_INVALID", "Review delta refers to an unknown service")
        if sid not in services:
            # These unchanged fields are adapter-owned identity/shape, never
            # model discoveries. Raw wire deltas are stored separately.
            services[sid] = deepcopy(originals[sid])
        if name in {"ports", "healthchecks"}:
            if (sid, name) not in changed:
                services[sid][name] = []
            services[sid][name].append(field)
        else:
            if (sid, name) in changed:
                raise AnalyzerError("RESULT_SCHEMA_INVALID", "Review contains duplicate scalar changes")
            services[sid][name] = field
        changed.add((sid, name))
    reply["result"]["services"] = list(services.values())
    return reply
