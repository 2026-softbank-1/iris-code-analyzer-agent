"""Bounded measurements of an explicitly selected, already running container.

This module never builds or starts an image. Its verified flag establishes target
identity and observation scope, not production capacity or deployment approval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..contracts import canonical_bytes, digest
from .contracts import normalize_planning_request, validate_planning_request

MAX_DOCKER_BYTES = 262_144
MAX_RESPONSE_BYTES = 262_144
MAX_OBSERVATION_BYTES = 67_108_864
DOCKER_TIMEOUT_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 3
STATS_INTERVAL_SECONDS = 2


class MeasurementFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate_inputs(
    container_id, url, source_snapshot_id, service_id, duration_seconds, requested_rps, concurrency
):
    if not isinstance(container_id, str) or not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
        raise MeasurementFailure("CONTAINER_ID_INVALID", "Use the hexadecimal ID of an existing container.")
    if not isinstance(source_snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{64}", source_snapshot_id):
        raise MeasurementFailure("SNAPSHOT_ID_INVALID", "A source snapshot SHA256 is required.")
    if not isinstance(service_id, str) or not service_id.strip() or len(service_id) > 200:
        raise MeasurementFailure("SERVICE_ID_INVALID", "A nonempty source service identifier is required.")
    if type(duration_seconds) is not int or not 60 <= duration_seconds <= 300:
        raise MeasurementFailure("DURATION_INVALID", "Observation duration must be 60 to 300 seconds.")
    if (
        type(requested_rps) not in (int, float)
        or not math.isfinite(requested_rps)
        or not 0 < requested_rps <= 50
    ):
        raise MeasurementFailure("RPS_INVALID", "Requested rate must be finite and between 0 and 50 RPS.")
    if type(concurrency) is not int or not 1 <= concurrency <= 20:
        raise MeasurementFailure("CONCURRENCY_INVALID", "Concurrency must be 1 to 20.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise MeasurementFailure("URL_INVALID", "Supply an explicit loopback HTTP URL.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9/_\.~-]*", parsed.path or "/")
        or any(segment in (".", "..") for segment in parsed.path.split("/"))
    ):
        raise MeasurementFailure(
            "URL_INVALID", "Only an explicitly approved loopback HTTP GET path is accepted."
        )
    return parsed


async def _docker_json(*arguments: str, docker_host: str | None = None) -> Any:
    command = ["docker"]
    if docker_host is not None:
        command += ["--host", docker_host]
    command += list(arguments)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise MeasurementFailure("DOCKER_UNAVAILABLE", "Docker CLI is unavailable.") from exc
    try:
        async with asyncio.timeout(DOCKER_TIMEOUT_SECONDS):
            output = bytearray()
            while chunk := await process.stdout.read(65_536):
                output.extend(chunk)
                if len(output) > MAX_DOCKER_BYTES:
                    raise MeasurementFailure(
                        "DOCKER_OUTPUT_LIMIT", "Docker metadata exceeds the bounded output limit."
                    )
            returncode = await process.wait()
        if returncode != 0:
            raise MeasurementFailure(
                "DOCKER_COMMAND_FAILED", "Docker could not observe the selected running container."
            )
        return json.loads(output)
    except TimeoutError as exc:
        raise MeasurementFailure("DOCKER_TIMEOUT", "Docker observation exceeded its time limit.") from exc
    except (ValueError, UnicodeError) as exc:
        raise MeasurementFailure(
            "DOCKER_OUTPUT_INVALID", "Docker returned invalid observation metadata."
        ) from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _local_docker_host() -> str:
    host = os.environ.get("DOCKER_HOST")
    if not host:
        host = await _docker_json("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}")
    if not isinstance(host, str):
        raise MeasurementFailure("DOCKER_CONTEXT_INVALID", "Docker context does not expose a local socket.")
    parsed = urlsplit(host)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise MeasurementFailure(
            "DOCKER_REMOTE_REJECTED", "Resource collection requires a local Unix-socket Docker daemon."
        )
    return host


def _verify_container(
    inspected: Any, container_id: str, source_snapshot_id: str, service_id: str, host_port: int
) -> dict:
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise MeasurementFailure("CONTAINER_METADATA_INVALID", "Exactly one inspected container is required.")
    container = inspected[0]
    identifier = container.get("Id", "")
    image_id = container.get("Image", "")
    if (
        not isinstance(identifier, str)
        or not re.fullmatch(r"[a-f0-9]{64}", identifier)
        or not identifier.startswith(container_id)
    ):
        raise MeasurementFailure(
            "CONTAINER_ID_MISMATCH", "Inspected container differs from the requested container."
        )
    state = container.get("State", {})
    if not state.get("Running") or state.get("Paused"):
        raise MeasurementFailure(
            "CONTAINER_NOT_RUNNING", "The selected container must already be running and unpaused."
        )
    labels = container.get("Config", {}).get("Labels") or {}
    if labels.get("iris.sourceSnapshotId") != source_snapshot_id:
        raise MeasurementFailure(
            "SNAPSHOT_LABEL_MISMATCH", "Container source identity is not verified by its snapshot label."
        )
    if labels.get("iris.serviceId") != service_id:
        raise MeasurementFailure(
            "SERVICE_LABEL_MISMATCH", "Container service identity is not verified by its service label."
        )
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise MeasurementFailure(
            "IMAGE_ID_INVALID", "The running container must expose an immutable image ID."
        )
    published = []
    for port, bindings in (container.get("NetworkSettings", {}).get("Ports") or {}).items():
        if not port.endswith("/tcp"):
            continue
        for binding in bindings or []:
            if str(binding.get("HostPort")) == str(host_port) and binding.get("HostIp") in (
                "",
                "127.0.0.1",
                "0.0.0.0",
            ):
                published.append(port)
    if not published:
        raise MeasurementFailure(
            "URL_CONTAINER_MISMATCH", "Loopback URL must use a published TCP port of the selected container."
        )
    limits = container.get("HostConfig", {})
    return {
        "containerId": identifier,
        "imageId": image_id,
        "sourceSnapshotIdLabel": source_snapshot_id,
        "serviceIdLabel": labels.get("iris.serviceId"),
        "publishedContainerPorts": sorted(published),
        "hardwareLimits": {
            key: limits.get(key) for key in ("Memory", "NanoCpus", "CpuQuota", "CpuPeriod", "CpusetCpus")
        },
    }


def _resource_sample(row: Any, container_id: str) -> tuple[float, float]:
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("ID"), str)
        or not re.fullmatch(r"[a-f0-9]{12,64}", row["ID"])
        or not container_id.startswith(row["ID"])
    ):
        raise MeasurementFailure(
            "STATS_ID_MISMATCH", "Container statistics do not match the verified target."
        )
    cpu = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)%", str(row.get("CPUPerc", "")).strip())
    memory = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(B|kB|KB|KiB|MB|MiB|GB|GiB|TB|TiB)",
        str(row.get("MemUsage", "")).split("/")[0].strip(),
    )
    if cpu is None or memory is None:
        raise MeasurementFailure("STATS_INVALID", "Container CPU and memory observations are unavailable.")
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "KiB": 1024,
        "MB": 10**6,
        "MiB": 1024**2,
        "GB": 10**9,
        "GiB": 1024**3,
        "TB": 10**12,
        "TiB": 1024**4,
    }
    millicores = float(cpu[1]) * 10
    memory_mib = float(memory[1]) * units[memory[2]] / 1024**2
    if not math.isfinite(millicores) or not math.isfinite(memory_mib) or memory_mib <= 0:
        raise MeasurementFailure(
            "STATS_INVALID", "Container resource observations must be finite and memory must be positive."
        )
    return millicores, memory_mib


async def _run_observation(*, container_id, docker_host, url, duration_seconds, requested_rps, concurrency):
    started = time.monotonic()
    deadline = started + duration_seconds
    maximum_calls = math.ceil(duration_seconds * requested_rps)
    next_call = 0
    next_due = started
    total_bytes = 0
    latencies, results, resources = [], [], []
    status_counts = {}

    async def sample_resources():
        while time.monotonic() < deadline:
            row = await _docker_json(
                "stats", "--no-stream", "--format", "{{json .}}", container_id, docker_host=docker_host
            )
            resources.append(_resource_sample(row, container_id))
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(STATS_INTERVAL_SECONDS, remaining))

    async def request_worker(client):
        nonlocal next_call, next_due, total_bytes
        while next_call < maximum_calls:
            next_call += 1
            now = time.monotonic()
            due = max(next_due, now)
            next_due = due + 1 / requested_rps
            if due >= deadline:
                break
            delay = due - now
            if delay > 0:
                await asyncio.sleep(delay)
            if time.monotonic() >= deadline:
                break
            before = time.monotonic()
            success = False
            status = "network_error"
            try:
                async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                    async with client.stream("GET", url) as response:
                        status = str(response.status_code)
                        response_bytes = 0
                        async for chunk in response.aiter_raw(65_536):
                            response_bytes += len(chunk)
                            total_bytes += len(chunk)
                            if total_bytes > MAX_OBSERVATION_BYTES:
                                raise MeasurementFailure(
                                    "HTTP_BYTE_LIMIT",
                                    "Observation exceeded its aggregate response-byte budget.",
                                )
                            if response_bytes > MAX_RESPONSE_BYTES:
                                break
                        success = 200 <= response.status_code < 300 and response_bytes <= MAX_RESPONSE_BYTES
            except (httpx.HTTPError, TimeoutError):
                pass
            results.append(success)
            status_counts[status] = status_counts.get(status, 0) + 1
            latencies.append((time.monotonic() - before) * 1000)

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False, limits=limits
    ) as client:
        tasks = [asyncio.create_task(request_worker(client)) for _ in range(concurrency)]
        tasks.append(asyncio.create_task(sample_resources()))
        try:
            async with asyncio.timeout(duration_seconds + DOCKER_TIMEOUT_SECONDS + 1):
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "elapsedSeconds": time.monotonic() - started,
        "latenciesMs": latencies,
        "responsesSucceeded": results,
        "resourceSamples": resources,
        "responseBytes": total_bytes,
        "scheduledCallLimit": maximum_calls,
        "httpStatusCounts": status_counts,
    }


async def collect_measurement(
    *,
    container_id: str,
    url: str,
    source_snapshot_id: str,
    service_id: str,
    duration_seconds: int = 60,
    requested_rps: float = 10,
    concurrency: int = 2,
) -> dict:
    """Return a measurement plus scope report, or an honest failed report."""
    report = {
        "schemaVersion": "iris.measurement-collector.v1",
        "scope": "existing_local_container",
        "deploymentAuthorized": False,
        "targetCreatedOrStarted": False,
        "capacityEstablished": False,
        "limitations": [
            "Only the explicitly approved GET path and supplied traffic scenario are measured.",
            "Docker memory is container working-set usage, not host RSS or source test-process memory.",
            "CPU/memory and latency observations do not establish production capacity or availability.",
            "Image architecture does not establish equivalent host CPU, emulation behavior or cloud-instance performance.",
        ],
    }
    try:
        parsed = _validate_inputs(
            container_id, url, source_snapshot_id, service_id, duration_seconds, requested_rps, concurrency
        )
        report["scenario"] = {
            "url": url,
            "method": "GET",
            "requestedRps": requested_rps,
            "concurrency": concurrency,
            "requestedDurationSeconds": duration_seconds,
        }
        docker_host = await _local_docker_host()
        inspected = await _docker_json(
            "inspect", "--type", "container", container_id, docker_host=docker_host
        )
        identity = _verify_container(inspected, container_id, source_snapshot_id, service_id, parsed.port)
        report["target"] = identity
        architecture = None
        try:
            image_metadata = await _docker_json(
                "image", "inspect", identity["imageId"], docker_host=docker_host
            )
            if (
                isinstance(image_metadata, list)
                and len(image_metadata) == 1
                and image_metadata[0].get("Id") == identity["imageId"]
            ):
                architecture = {"amd64": "x86_64", "arm64": "arm64"}.get(
                    image_metadata[0].get("Architecture")
                )
        except MeasurementFailure:
            pass
        identity["architecture"] = architecture
        if architecture is None:
            report["limitations"].append(
                "Image architecture is unknown; cross-hardware sizing remains an assumption."
            )
        observed = await _run_observation(
            container_id=identity["containerId"],
            docker_host=docker_host,
            url=url,
            duration_seconds=duration_seconds,
            requested_rps=requested_rps,
            concurrency=concurrency,
        )
        final_inspection = await _docker_json(
            "inspect", "--type", "container", container_id, docker_host=docker_host
        )
        final_identity = _verify_container(
            final_inspection, container_id, source_snapshot_id, service_id, parsed.port
        )
        if final_identity["hardwareLimits"] != identity["hardwareLimits"] or final_inspection[0].get(
            "RestartCount", 0
        ) != inspected[0].get("RestartCount", 0):
            raise MeasurementFailure(
                "TARGET_CHANGED", "Container restarted or its resource limits changed during observation."
            )
        resources, latencies, responses = (
            observed["resourceSamples"],
            observed["latenciesMs"],
            observed["responsesSucceeded"],
        )
        elapsed = observed["elapsedSeconds"]
        if (
            not resources
            or not responses
            or len(latencies) != len(responses)
            or not math.isfinite(elapsed)
            or elapsed < duration_seconds
        ):
            raise MeasurementFailure(
                "OBSERVATION_INCOMPLETE",
                "A complete observation window, HTTP responses and container resource samples are required.",
            )
        if any(not math.isfinite(value) or value <= 0 for value in latencies):
            raise MeasurementFailure(
                "LATENCY_INVALID", "Request latency observations must be finite and positive."
            )
        peak_cpu = max(sample[0] for sample in resources)
        peak_memory = max(sample[1] for sample in resources)
        measured_at = datetime.now(timezone.utc).isoformat()
        metrics = {
            "peakMemoryMiB": peak_memory,
            "cpuMillicores": peak_cpu if peak_cpu > 0 else None,
            "achievedRps": len(responses) / elapsed,
            "p95LatencyMs": sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)],
            "errorRate": sum(not success for success in responses) / len(responses),
        }
        measurement = {
            "id": "measure-"
            + digest(
                {
                    "container": identity["containerId"],
                    "snapshot": source_snapshot_id,
                    "service": service_id,
                    "at": measured_at,
                }
            )[:24],
            "serviceId": service_id,
            "sourceSnapshotId": source_snapshot_id,
            "kind": "load",
            "verified": True,
            "measuredAt": measured_at,
            "metrics": metrics,
            "conditions": {
                "durationSeconds": elapsed,
                "concurrency": concurrency,
                "command": "iris-measure: existing container; approved loopback HTTP GET",
                "architecture": architecture,
                "imageDigest": identity["imageId"],
                "capacityValidated": False,
            },
        }
        validation = normalize_planning_request()
        validation["measurements"] = [measurement]
        validate_planning_request(validation)
        report["observation"] = {
            "durationSeconds": elapsed,
            "completedResponses": len(responses),
            "successfulResponses": sum(responses),
            "responseBytes": observed["responseBytes"],
            "scheduledCallLimit": observed["scheduledCallLimit"],
            "resourceSampleCount": len(resources),
            "observedCpuPeakMillicores": peak_cpu,
            "httpStatusCounts": observed.get("httpStatusCounts", {}),
        }
        if peak_cpu == 0:
            report["limitations"].append("CPU samples rounded to zero; CPU sizing remains unmeasured.")
        return {"status": "completed", "measurement": measurement, "collectorReport": report}
    except asyncio.CancelledError:
        raise
    except MeasurementFailure as exc:
        report["failure"] = {"code": exc.code, "message": str(exc)}
    except TimeoutError:
        report["failure"] = {
            "code": "OBSERVATION_TIMEOUT",
            "message": "Observation exceeded its bounded execution window.",
        }
    except Exception:
        report["failure"] = {
            "code": "OBSERVATION_FAILED",
            "message": "Observation failed; no verified measurement was produced.",
        }
    return {"status": "failed", "measurement": None, "collectorReport": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure an existing labeled local container without building or starting it."
    )
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--source-snapshot-id", required=True)
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--rps", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = asyncio.run(
        collect_measurement(
            container_id=args.container_id,
            url=args.url,
            source_snapshot_id=args.source_snapshot_id,
            service_id=args.service_id,
            duration_seconds=args.duration,
            requested_rps=args.rps,
            concurrency=args.concurrency,
        )
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(canonical_bytes(result) + b"\n")
    print(json.dumps({"status": result["status"], "out": str(args.out)}, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
