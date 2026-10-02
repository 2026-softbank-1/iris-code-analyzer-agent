"""Bounded local Chat Completions test of real repository and system-advisor contracts.

Uses only the synthetic Organization source manifest by default. No paid provider,
GitHub collection, source execution, image build or cluster deployment occurs.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from contextlib import nullcontext
from pathlib import Path

import httpx

from iris_analyzer.contracts import AnalyzerError, canonical_bytes, digest
from iris_analyzer.integrations import LocalAnalysisClient
from iris_analyzer.opencode import ModelConfig, OpenCodeRunner
from iris_analyzer.opencode.review_protocol import canonicalize_review
from iris_analyzer.opencode.runner import MODEL_REVIEW_SCHEMA, parse_json_reply
from iris_analyzer.organization.advisor import OrganizationOpenCodeRunner, advisor_input
from iris_analyzer.organization.cli import _save_result, _source_manifest
from iris_analyzer.organization.pipeline import OrganizationAnalysisClient
from iris_analyzer.pipeline import write_json


class LocalChatTransport:
    def invoke_model(self, bundle):
        self._validate_input(bundle)
        recorder = self.recorder
        if self.cancel_event.is_set() or any(row.get("remoteCompletion") == "unknown" for row in recorder):
            raise AnalyzerError(
                "LOCAL_EXECUTION_UNCERTAIN",
                "An interrupted local inference must be reconciled before another call",
            )
        if len(recorder) >= self.call_limit:
            raise AnalyzerError("LOCAL_CALL_LIMIT", "Local test reached its configured model call limit")
        document = self._request_document(bundle)
        content = json.dumps(document, ensure_ascii=False)
        self.last_request = {"parts": [{"type": "text", "text": content}]}
        self.last_response = self.last_wire_reply = None
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            "reasoning_effort": "none",
        }
        if self.local_output_mode == "schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "iris_organization_test",
                    "strict": True,
                    "schema": self._response_schema(bundle),
                },
            }
        if len(canonical_bytes(payload)) > 512_000:
            raise AnalyzerError("LOCAL_INPUT_LIMIT", "Local model input exceeds the fixture test limit")
        record = {
            "id": len(recorder) + 1,
            "provider": "local",
            "model": self.config.model,
            "promptVersion": self.prompt_version,
            "transport": "chat_completions",
            "outputMode": self.local_output_mode,
            "inputDigest": digest(payload),
            "inputBytes": len(canonical_bytes(payload)),
        }
        recorder.append(record)
        self.calls.append(record)
        started = time.monotonic()
        artifact = self.local_artifacts / f"call-{record['id']:02d}"
        write_json(artifact / "request.json", payload)
        try:
            with httpx.Client(
                timeout=self.config.timeout_seconds, trust_env=False, follow_redirects=False
            ) as client:
                response = client.post(self.local_base_url + "/chat/completions", json=payload)
            record["httpStatus"] = response.status_code
            if response.status_code != 200:
                write_json(
                    artifact / "http-error.json",
                    {"status": response.status_code, "body": response.text[:4000]},
                )
                raise AnalyzerError("LOCAL_HTTP_ERROR", "Local model rejected the supplied request")
            data = response.json()
            self.last_response = data
            write_json(artifact / "response.json", data)
            record["usage"] = data.get("usage")
            record["reportedModel"] = data.get("model")
            if data.get("model") != self.config.model:
                raise AnalyzerError("MODEL_CONTEXT_MISMATCH", "Local server reported another model")
            choice = data["choices"][0]
            record["finishReason"] = choice.get("finish_reason")
            reply, recovery = parse_json_reply(
                choice["message"]["content"], finish_reason=choice.get("finish_reason")
            )
            record["formatRecovery"] = bool(recovery)
            self.last_wire_reply = copy.deepcopy(reply)
            validated = self._validate_output(reply)
            write_json(artifact / "validated-output.json", validated)
            record["status"] = "validated"
            return (
                canonicalize_review(reply, bundle)
                if self.response_schema is MODEL_REVIEW_SCHEMA
                else validated
            )
        except httpx.HTTPError as error:
            record.update(status="failed", error=type(error).__name__, remoteCompletion="unknown")
            raise AnalyzerError(
                "LOCAL_CONNECTION_ERROR", "Local model connection failed; remote completion unknown"
            ) from None
        except AnalyzerError as error:
            record.update(status="failed", error=error.code)
            raise
        except (ValueError, KeyError, TypeError, IndexError):
            record.update(status="failed", error="LOCAL_RESPONSE_INVALID")
            raise AnalyzerError(
                "LOCAL_RESPONSE_INVALID", "Local model returned an invalid response envelope"
            ) from None
        finally:
            record["elapsedSeconds"] = round(time.monotonic() - started, 3)
            write_json(artifact / "call-report.json", record)
            print(json.dumps(record, ensure_ascii=False), flush=True)


class LocalRepositoryRunner(LocalChatTransport, OpenCodeRunner):
    pass


class LocalSystemRunner(LocalChatTransport, OrganizationOpenCodeRunner):
    pass


def configure(runner, args, recorder):
    runner.local_base_url = args.base_url.rstrip("/")
    runner.local_output_mode = args.output_mode
    runner.local_artifacts = args.out / "local-calls"
    runner.call_limit = args.max_calls
    runner.recorder = recorder
    return runner


class LocalSystemAdvisor:
    def __init__(self, runner):
        self.runner = runner

    async def suggest(self, graph, request):
        outcome = await asyncio.to_thread(self.runner.invoke_model, advisor_input(graph, request))
        return outcome, {
            "schemaVersion": "iris.system-advice-run.v1",
            "mode": "local_ai",
            "graphDigest": graph["graphDigest"],
            "calls": copy.deepcopy(self.runner.calls),
        }


async def run(args):
    request = json.loads(args.request.read_text())
    recorder = []
    config = ModelConfig(
        provider="local",
        model=args.model,
        server_url=args.base_url,
        timeout_seconds=args.timeout,
        max_output_tokens=args.max_output_tokens,
    )
    repo_runner = configure(LocalRepositoryRunner(config), args, recorder)
    system_runner = configure(LocalSystemRunner(config), args, recorder)
    started = time.monotonic()
    try:

        def runner_factory(cancelled):
            repo_runner.cancel_event = cancelled
            return nullcontext(repo_runner)

        client = OrganizationAnalysisClient(
            analyzer=LocalAnalysisClient()
            if args.static_repositories
            else LocalAnalysisClient(runner_factory=runner_factory),
            advisor=LocalSystemAdvisor(system_runner),
        )
        result = await client.analyze_organization(
            request, out=args.out, sources=_source_manifest(args.sources)
        )
        _save_result(result, args.out)
        summary = {
            "schemaVersion": "iris.organization-local-test.v1",
            "model": args.model,
            "outputMode": args.output_mode,
            "repositoryAnalysisMode": "static" if args.static_repositories else "local_ai",
            "durationSeconds": round(time.monotonic() - started, 3),
            "status": result.get("status", result["plan"]["status"]),
            "repositories": [
                {"name": row["fullName"], "status": row["status"]} for row in result["graph"]["repositories"]
            ],
            "components": [
                {"id": row["id"], "kind": row["kind"], "selected": row.get("selected")}
                for row in result["graph"]["components"]
            ],
            "questions": result["plan"]["questions"],
            "systemAdvice": result.get("systemAdvice"),
            "advisorReport": result.get("advisorReport"),
            "calls": recorder,
            "modelValidation": "passed"
            if recorder and all(row.get("status") == "validated" for row in recorder)
            else "failed",
            "deploymentExecuted": False,
            "cloudProviderCalls": 0,
        }
        write_json(args.out / "local-test-report.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0 if summary["modelValidation"] == "passed" else 2
    finally:
        repo_runner.close()
        system_runner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://192.168.0.67:1234/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--sources", type=Path, default=Path("fixtures/organization-demo/sources.json"))
    parser.add_argument("--request", type=Path, default=Path("fixtures/organization-demo/request.json"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--output-mode", choices=["schema", "plain"], default="schema")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--max-calls", type=int, default=8)
    parser.add_argument(
        "--static-repositories",
        action="store_true",
        help="Isolate system advice: statically analyze repositories, then call the local model for the system graph",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except AnalyzerError as error:
        print(json.dumps({"error": error.code, "message": error.message}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
