"""Print a constructed evidence-support audit; no model/source execution.

The JSON checks one admission invariant, not model quality or universal correctness.
Run: python scripts/audit_ai_judgment.py
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from iris_analyzer.contracts import AnalyzerError
from iris_analyzer.opencode.runner import response_template
from iris_analyzer.preprocess import prepare_context, release_snapshot
from iris_analyzer.result import static_analysis, validate_analysis, validate_analysis_with_report

with TemporaryDirectory(prefix="iris-judgment-audit-") as directory:
    root = Path(directory)
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"express": "5"}, "scripts": {"start": "node index.js"}})
    )
    (root / "index.js").write_text(
        "const express = require('express');\nconst app = express();\napp.listen(3000);\n"
    )
    (root / "Dockerfile").write_text(
        'FROM node:24\nWORKDIR /app\nCOPY . .\nEXPOSE 3000\nCMD ["node", "index.js"]\n'
    )
    bundle = prepare_context(root)
    try:
        observation = next(
            f
            for f in bundle["facts"]
            if f["key"] == "runtime.port"
            and f["scope"] == "container"
            and any(
                e["path"] == "index.js" and e["evidenceId"] in f["evidenceIds"] for e in bundle["evidence"]
            )
        )
        evidence_id = observation["evidenceIds"][0]
        evidence = next(e for e in bundle["evidence"] if e["evidenceId"] == evidence_id)
        reply = response_template(bundle)
        reply["result"]["dependencies"] = [
            {
                "value": {"name": "postgres", "kind": "database", "engine": "postgresql", "component": "."},
                "status": "suggested",
                "scope": "production",
                "evidenceIds": [evidence_id],
                "reason": "PostgreSQL is required",
            }
        ]
        accepted, verification = validate_analysis_with_report(reply, bundle)
        reply["result"]["dependencies"][0]["status"] = "detected"
        try:
            validate_analysis(reply, bundle)
            detected = "unexpected acceptance"
        except AnalyzerError as error:
            detected = error.code
        questioned = response_template(bundle)
        questioned["result"]["questions"] = [
            {
                "key": "runtime.port",
                "reason": "Review whether the observed listener is the production entrypoint",
                "kind": "code_review",
            }
        ]
        questioned_result = validate_analysis(questioned, bundle)
        report = {
            "staticStatus": static_analysis(bundle)["status"],
            "unresolved": bundle["unresolved"],
            "unrelatedCitedEvidence": {"path": evidence["path"], "text": evidence["text"]},
            "suggestedCase": {
                "status": accepted["status"],
                "dependencies": accepted["dependencies"],
                "questions": accepted["questions"],
                "verification": verification,
            },
            "sameClaimAsDetected": detected,
            "questionCase": {
                "status": questioned_result["status"],
                "questions": questioned_result["questions"],
                "portsPreserved": questioned_result["services"][0]["ports"],
            },
        }
        report["modelEvaluationPerformed"] = False
        report["policyGatePassed"] = not any(
            item["value"].get("engine") == "postgresql"
            for item in accepted["dependencies"]
            if isinstance(item["value"], dict)
        )
        report["evaluationType"] = "constructed_validator_counterexample"
        print(json.dumps(report, indent=2))
    finally:
        release_snapshot(bundle["source"]["snapshotId"])
