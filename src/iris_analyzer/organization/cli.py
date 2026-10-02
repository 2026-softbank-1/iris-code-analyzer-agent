"""Organization input, fixed-snapshot replanning and separately authorized execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from ..contracts import AnalyzerError
from ..integrations import LocalAnalysisClient, create_live_runner_factory
from ..pipeline import write_json
from .contracts import REQUEST_SCHEMA, validate_request
from .execution import execute_system_bundle, reconcile_system_operation, write_system_bundle
from .pipeline import OrganizationAnalysisClient, plan_organization


def _json(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 100 * 1024 * 1024:
        raise AnalyzerError("ORGANIZATION_INPUT_INVALID", "JSON input must be a bounded ordinary file")
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise AnalyzerError("ORGANIZATION_INPUT_INVALID", "Expected a JSON object")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iris-organization")
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze", help="Inspect authorized repositories and generate a system plan")
    analyze.add_argument("organization", help="GitHub Organization slug or https://github.com/ORG")
    analyze.add_argument("--purpose")
    analyze.add_argument("--request", type=Path)
    analyze.add_argument("--sources", type=Path, help="Offline iris.organization-sources.v1 manifest")
    analyze.add_argument("--out", type=Path, required=True)
    analyze.add_argument("--include", action="append", help="Repository name/full name glob; repeatable")
    analyze.add_argument("--exclude", action="append", help="Repository name/full name glob; repeatable")
    analyze.add_argument("--max-repositories", type=int)
    analyze.add_argument("--context", help="Explicit existing Kubernetes context")
    analyze.add_argument("--namespace")
    mode = analyze.add_mutually_exclusive_group()
    mode.add_argument(
        "--ai",
        action="store_true",
        help="Use the configured model and shared budget for repo analysis and system advice",
    )
    mode.add_argument(
        "--offline",
        action="store_true",
        help="Static analysis mode (default); GitHub downloads still occur unless --sources is supplied",
    )
    analyze.add_argument("--env-file", type=Path, default=Path(".env"))
    analyze.add_argument("--opencode-executable", default="opencode")
    analyze.add_argument("--budget-ledger", type=Path, default=Path("artifacts/model-budget-ledger.json"))
    analyze.add_argument("--max-cost-usd", type=float, default=1.0)
    plan = sub.add_parser("plan", help="Answer questions against a fixed Organization snapshot")
    plan.add_argument("--snapshot", required=True, type=Path)
    plan.add_argument("--request", required=True, type=Path)
    plan.add_argument("--out", required=True, type=Path)
    compile_parser = sub.add_parser(
        "compile", help="Generate integrity-bound Helm/native execution artifacts"
    )
    compile_parser.add_argument("--plan", required=True, type=Path)
    compile_parser.add_argument("--out", required=True, type=Path)
    apply = sub.add_parser(
        "apply", help="Dry-run by default; execute only with exact artifact/scoped authorization"
    )
    apply.add_argument("--bundle", required=True, type=Path)
    apply.add_argument("--authorization", type=Path)
    apply.add_argument("--workspace", type=Path, help="Private local-workspace.json emitted during analysis")
    apply.add_argument(
        "--execute",
        action="store_true",
        help="Allow the separately authorized executor to mutate images and the explicit namespace",
    )
    reconcile = sub.add_parser(
        "reconcile", help="Record an operator-observed outcome before resuming an interrupted operation"
    )
    reconcile.add_argument("--bundle", type=Path, required=True)
    reconcile.add_argument("--authorization", type=Path, required=True)
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--resolution", choices=["retry", "complete"], required=True)
    reconcile.add_argument(
        "--image-reference", help="Verified immutable registry image for a completed image operation"
    )
    schema = sub.add_parser("schema", help="Write the public Organization request JSON Schema")
    schema.add_argument("--out", required=True, type=Path)
    return parser


def _source_manifest(path: Path) -> dict:
    document = _json(path)
    for row in document.get("repositories", []):
        if not isinstance(row, dict) or not isinstance(row.get("sourceRoot"), str):
            raise AnalyzerError(
                "ORGANIZATION_SOURCES_INVALID", "Local manifest requires sourceRoot for each repository"
            )
        source = Path(row["sourceRoot"])
        if not source.is_absolute():
            source = path.resolve().parent / source
        if source.is_symlink():
            raise AnalyzerError("ORGANIZATION_SOURCES_INVALID", "Local sources cannot be symbolic links")
        row["sourceRoot"] = str(source.resolve(strict=True))
    return document


def _request(args) -> dict:
    request = (
        _json(args.request)
        if args.request
        else {"schemaVersion": "iris.organization-request.v1", "organization": args.organization}
    )
    from .github import parse_organization

    if (
        parse_organization(request.get("organization", "")).lower()
        != parse_organization(args.organization).lower()
    ):
        raise AnalyzerError(
            "ORGANIZATION_REQUEST_INVALID", "CLI Organization and request Organization differ"
        )
    if args.purpose is not None:
        request["purpose"] = args.purpose
    for argument, field in (
        (args.include, "includeRepositories"),
        (args.exclude, "excludeRepositories"),
        (args.max_repositories, "maxRepositories"),
    ):
        if argument is not None:
            request[field] = argument
    if args.context is not None or args.namespace is not None:
        target = request.setdefault("target", {})
        if args.context is not None:
            target["context"] = args.context
        if args.namespace is not None:
            target["namespace"] = args.namespace
    return validate_request(request)


def _architecture(graph: dict) -> str:
    nodes = {item["id"]: "n" + str(index) for index, item in enumerate(graph["components"])}
    lines = [
        "# Organization system architecture",
        "",
        "Purpose: " + (graph.get("purpose") or "not confirmed"),
        "",
        "```mermaid",
        "flowchart LR",
    ]
    for item in graph["components"]:
        label = str(item.get("name", item["id"])) + " / " + item["kind"]
        label = (
            label.replace("\n", " ")
            .replace("\r", " ")
            .replace('"', "'")
            .replace("<", "")
            .replace(">", "")[:200]
        )
        lines.append(f'    {nodes[item["id"]]}["{label}"]')
    for relation in graph["relationships"]:
        source, target = nodes.get(relation.get("fromServiceId")), nodes.get(relation.get("toServiceId"))
        if source and target:
            arrow = "-->" if relation["status"] in {"detected", "user_confirmed"} else "-.->"
            lines.append(f"    {source} {arrow}|{relation['kind']}: {relation['status']}| {target}")
    lines.extend(
        [
            "```",
            "",
            "Solid links have detected or operator-confirmed relationships; dotted links are suggestions.",
            "No node or link attests that infrastructure is provisioned or deployment has succeeded.",
            "",
            "## Questions",
            "",
        ]
    )
    for question in graph["questions"]:
        reason = str(question["reason"]).replace("\n", " ")
        lines.append("- " + question["key"] + ": " + reason)
    return "\n".join(lines) + "\n"


def _save_result(result: dict, out: Path) -> dict:
    write_json(out / "system-graph.json", result["graph"])
    write_json(out / "system-plan.json", result["plan"])
    write_json(out / "organization-result.json", result)
    (out / "architecture.md").write_text(_architecture(result["graph"]))
    return write_system_bundle(result["plan"], out / "system-bundle")


async def _analyze(args) -> dict:
    request = _request(args)
    analyzer, advisor = LocalAnalysisClient(), None
    if args.ai:
        from ..opencode import ModelConfig
        from .advisor import OrganizationAdvisor

        config = ModelConfig.from_env(args.env_file if args.env_file.is_file() else None)
        if not 0 < args.max_cost_usd <= 100:
            raise AnalyzerError("ORGANIZATION_REQUEST_INVALID", "Model budget must be finite and positive")
        analyzer = LocalAnalysisClient(
            runner_factory=create_live_runner_factory(
                config,
                budget_ledger=args.budget_ledger,
                executable=args.opencode_executable,
                max_cost_usd=args.max_cost_usd,
            )
        )
        advisor = OrganizationAdvisor(
            config,
            ledger=args.budget_ledger,
            executable=args.opencode_executable,
            max_cost_usd=args.max_cost_usd,
        )
    client = OrganizationAnalysisClient(analyzer=analyzer, advisor=advisor)
    result = await client.analyze_organization(
        request, out=args.out, sources=_source_manifest(args.sources) if args.sources else None
    )
    bundle = _save_result(result, args.out.absolute())
    return {
        "schemaVersion": result["schemaVersion"],
        "organization": request["organization"],
        "mode": "ai" if args.ai else "static",
        "status": result.get("status", result["plan"]["status"]),
        "snapshotDigest": result["snapshotDigest"],
        "planDigest": result["plan"]["planDigest"],
        "bundleDigest": bundle.get("bundleDigest"),
        "questions": result["plan"]["questions"],
        "deploymentAuthorized": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv if argv is not None else sys.argv[1:])
    # An Organization URL/slug alone is a convenient alias for analyze.
    commands = {"analyze", "plan", "compile", "apply", "reconcile", "schema"}
    if values and not values[0].startswith("-") and values[0] not in commands:
        values.insert(0, "analyze")
    args = _parser().parse_args(values)
    try:
        if args.command == "analyze":
            summary = asyncio.run(_analyze(args))
        elif args.command == "plan":
            if args.out.exists() or args.out.is_symlink():
                raise AnalyzerError(
                    "ORGANIZATION_OUTPUT_EXISTS", "Replanning requires a new output directory"
                )
            args.out.mkdir(parents=True, mode=0o700)
            snapshot = _json(args.snapshot)
            result = plan_organization(snapshot, _json(args.request))
            bundle = _save_result(result, args.out)
            summary = {
                "status": result["plan"]["status"],
                "planDigest": result["plan"]["planDigest"],
                "bundleDigest": bundle.get("bundleDigest"),
                "questions": result["plan"]["questions"],
                "deploymentAuthorized": False,
            }
        elif args.command == "compile":
            summary = write_system_bundle(_json(args.plan), args.out)
        elif args.command == "apply":
            authorization = _json(args.authorization) if args.authorization else None
            if args.execute and authorization is None:
                raise AnalyzerError(
                    "SYSTEM_AUTHORIZATION_REQUIRED",
                    "Execution requires scoped authorization for this exact plan and bundle",
                )
            roots = None
            if args.workspace:
                workspace = _json(args.workspace)
                if workspace.get("schemaVersion") != "iris.organization-workspace.v1":
                    raise AnalyzerError(
                        "ORGANIZATION_INPUT_INVALID", "Expected the private Organization workspace manifest"
                    )
                plan = _json(args.bundle / "system-plan.json")
                if workspace.get("snapshotDigest") != plan.get("organizationSnapshotDigest"):
                    raise AnalyzerError(
                        "ORGANIZATION_SOURCE_CHANGED", "Workspace belongs to another Organization snapshot"
                    )
                roots = workspace.get("sourceRoots")
            summary = execute_system_bundle(
                args.bundle, authorization=authorization, source_roots=roots, dry_run=not args.execute
            )
        elif args.command == "reconcile":
            summary = reconcile_system_operation(
                args.bundle,
                args.operation_id,
                authorization=_json(args.authorization),
                resolution=args.resolution,
                image_reference=args.image_reference,
            )
        else:
            write_json(args.out, REQUEST_SCHEMA)
            summary = {"schemaVersion": "iris.organization-request.v1", "written": True}
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        return 0
    except AnalyzerError as error:
        print(
            json.dumps({"error": {"code": error.code, "message": error.message}}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {
                    "error": {
                        "code": "ORGANIZATION_IO_INVALID",
                        "message": "Input or artifact could not be processed",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
