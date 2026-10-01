import asyncio
import copy
import json

import httpx
import pytest

from iris_analyzer.deployment import measure
from iris_analyzer.deployment.contracts import normalize_planning_request, validate_planning_request

CONTAINER = "a" * 64
SNAPSHOT = "b" * 64
IMAGE = "sha256:" + "c" * 64
SOCKET = "unix:///var/run/docker.sock"


def container_metadata():
    return [
        {
            "Id": CONTAINER,
            "Image": IMAGE,
            "State": {"Running": True, "Paused": False},
            "RestartCount": 0,
            "Config": {"Labels": {"iris.sourceSnapshotId": SNAPSHOT, "iris.serviceId": "api"}},
            "HostConfig": {
                "Memory": 536870912,
                "NanoCpus": 1000000000,
                "CpuQuota": 0,
                "CpuPeriod": 0,
                "CpusetCpus": "",
            },
            "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8766"}]}},
        }
    ]


def arguments():
    return dict(
        container_id=CONTAINER[:12],
        url="http://127.0.0.1:8766/health",
        source_snapshot_id=SNAPSHOT,
        service_id="api",
    )


@pytest.fixture
def observed_target(monkeypatch):
    calls = []
    metadata = container_metadata()
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    async def docker(*args, docker_host=None):
        calls.append((args, docker_host))
        if args[0] == "context":
            return SOCKET
        if args[0] == "inspect":
            return copy.deepcopy(metadata)
        if args[:2] == ("image", "inspect"):
            return [{"Id": IMAGE, "Architecture": "amd64"}]
        raise AssertionError("No unapproved Docker command")

    async def observation(**kwargs):
        assert kwargs["container_id"] == CONTAINER
        assert kwargs["docker_host"] == SOCKET
        return {
            "elapsedSeconds": 60,
            "latenciesMs": list(range(10, 210, 10)),
            "responsesSucceeded": [True] * 19 + [False],
            "resourceSamples": [(100, 64), (250, 128)],
            "responseBytes": 100,
            "scheduledCallLimit": 600,
        }

    monkeypatch.setattr(measure, "_docker_json", docker)
    monkeypatch.setattr(measure, "_run_observation", observation)
    return metadata, calls


def test_collector_returns_scoped_contract_measurement_and_separate_context_report(observed_target):
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["status"] == "completed"
    measurement = result["measurement"]
    assert measurement["verified"] and measurement["kind"] == "load"
    assert measurement["conditions"]["capacityValidated"] is False
    assert measurement["sourceSnapshotId"] == SNAPSHOT and measurement["serviceId"] == "api"
    assert measurement["metrics"] == {
        "peakMemoryMiB": 128,
        "cpuMillicores": 250,
        "achievedRps": 1 / 3,
        "p95LatencyMs": 190,
        "errorRate": 0.05,
    }
    assert measurement["conditions"]["architecture"] == "x86_64"
    assert measurement["conditions"]["imageDigest"] == IMAGE
    assert result["collectorReport"]["target"]["hardwareLimits"]["Memory"] == 536870912
    assert not result["collectorReport"]["capacityEstablished"]
    assert not result["collectorReport"]["targetCreatedOrStarted"]
    request = normalize_planning_request()
    request["measurements"] = [measurement]
    validate_planning_request(request)
    calls = observed_target[1]
    assert all(args[0] in {"context", "inspect", "image"} for args, _ in calls)
    assert all(host == SOCKET for args, host in calls if args[0] != "context")


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://127.0.0.1:8766/health"},
        {"url": "http://localhost:8766/health"},
        {"url": "http://example.com/health"},
        {"url": "http://127.0.0.1:8766/health?token=secret"},
        {"url": "http://user:secret@127.0.0.1:8766/health"},
        {"url": "http://127.0.0.1:8766/%2e%2e/reset"},
        {"url": "http://127.0.0.1:8766/../reset"},
        {"duration_seconds": 59},
        {"duration_seconds": 301},
        {"requested_rps": 51},
        {"requested_rps": float("inf")},
        {"requested_rps": True},
        {"concurrency": 21},
        {"container_id": "--privileged"},
        {"source_snapshot_id": "not-a-snapshot"},
    ],
)
def test_unsafe_or_unbounded_inputs_fail_before_any_docker_or_network_use(observed_target, changes):
    result = asyncio.run(measure.collect_measurement(**{**arguments(), **changes}))
    assert result["status"] == "failed" and result["measurement"] is None
    assert observed_target[1] == []


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda c: c[0]["Config"]["Labels"].pop("iris.sourceSnapshotId"), "SNAPSHOT_LABEL_MISMATCH"),
        (lambda c: c[0]["Config"]["Labels"].update({"iris.serviceId": "web"}), "SERVICE_LABEL_MISMATCH"),
        (lambda c: c[0]["Config"]["Labels"].pop("iris.serviceId"), "SERVICE_LABEL_MISMATCH"),
        (lambda c: c[0]["State"].update(Running=False), "CONTAINER_NOT_RUNNING"),
        (lambda c: c[0]["NetworkSettings"]["Ports"].clear(), "URL_CONTAINER_MISMATCH"),
    ],
)
def test_unverified_target_produces_failed_report_and_no_measurement(observed_target, mutation, code):
    mutation(observed_target[0])
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["measurement"] is None
    assert result["collectorReport"]["failure"]["code"] == code


def test_remote_docker_context_is_rejected(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["status"] == "failed" and result["measurement"] is None
    assert result["collectorReport"]["failure"]["code"] == "DOCKER_REMOTE_REJECTED"


@pytest.mark.parametrize(
    "cpu,memory,expected",
    [
        ("12.50%", "64MiB / 512MiB", (125, 64)),
        ("150.00%", "1GiB / 2GiB", (1500, 1024)),
        ("0.00%", "1048576B / 512MiB", (0, 1)),
    ],
)
def test_stats_are_container_millicores_and_mib(cpu, memory, expected):
    assert (
        measure._resource_sample({"ID": CONTAINER[:12], "CPUPerc": cpu, "MemUsage": memory}, CONTAINER)
        == expected
    )


def test_invalid_or_foreign_container_stats_are_not_promoted_to_observations():
    for row in [
        {"ID": "d" * 12, "CPUPerc": "1%", "MemUsage": "1MiB / 512MiB"},
        {"ID": CONTAINER[:12], "CPUPerc": "nan%", "MemUsage": "1MiB / 512MiB"},
        {"ID": CONTAINER[:12], "CPUPerc": "1%", "MemUsage": "0B / 512MiB"},
    ]:
        with pytest.raises(measure.MeasurementFailure):
            measure._resource_sample(row, CONTAINER)


def test_missing_docker_and_timeout_results_are_honest_and_sanitized(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    async def missing(*args, **kwargs):
        raise OSError("private local installation path")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["collectorReport"]["failure"]["code"] == "DOCKER_UNAVAILABLE"
    assert "private local" not in json.dumps(result)

    async def timed_out(*args, **kwargs):
        raise measure.MeasurementFailure("DOCKER_TIMEOUT", "Docker observation exceeded its time limit.")

    monkeypatch.setattr(measure, "_docker_json", timed_out)
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["collectorReport"]["failure"]["code"] == "DOCKER_TIMEOUT"
    assert result["measurement"] is None


def test_architecture_unavailable_is_reported_without_inventing_cpu_platform(observed_target, monkeypatch):
    previous = measure._docker_json

    async def docker(*args, **kwargs):
        if args[0] == "image":
            raise measure.MeasurementFailure("DOCKER_TIMEOUT", "Bounded image metadata failed")
        return await previous(*args, **kwargs)

    monkeypatch.setattr(measure, "_docker_json", docker)
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["status"] == "completed"
    assert result["measurement"]["conditions"]["architecture"] is None
    assert any(
        "architecture is unknown" in limitation for limitation in result["collectorReport"]["limitations"]
    )


def test_target_restart_invalidates_observation_window(observed_target, monkeypatch):
    previous = measure._run_observation

    async def observation(**kwargs):
        result = await previous(**kwargs)
        observed_target[0][0]["RestartCount"] = 1
        return result

    monkeypatch.setattr(measure, "_run_observation", observation)
    result = asyncio.run(measure.collect_measurement(**arguments()))
    assert result["status"] == "failed" and result["measurement"] is None
    assert result["collectorReport"]["failure"]["code"] == "TARGET_CHANGED"


class Payload(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"small response"


@pytest.mark.parametrize("status", [200, 302])
def test_load_loop_is_bounded_loopback_get_and_does_not_follow_redirects(monkeypatch, status):
    calls, client_options = [], []
    original_client = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "127.0.0.1"
        return httpx.Response(status, headers={"Location": "https://foreign.example/"}, stream=Payload())

    def client(**kwargs):
        client_options.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    async def docker(*args, **kwargs):
        assert args[0] == "stats"
        return {"ID": CONTAINER[:12], "CPUPerc": "10.00%", "MemUsage": "64MiB / 512MiB"}

    monkeypatch.setattr(measure.httpx, "AsyncClient", client)
    monkeypatch.setattr(measure, "_docker_json", docker)
    observed = asyncio.run(
        measure._run_observation(
            container_id=CONTAINER,
            docker_host=SOCKET,
            url="http://127.0.0.1:8766/health",
            duration_seconds=0.05,
            requested_rps=50,
            concurrency=2,
        )
    )
    assert 1 <= len(calls) <= 3
    assert observed["scheduledCallLimit"] == 3
    assert all(success == (status == 200) for success in observed["responsesSucceeded"])
    assert client_options[0]["follow_redirects"] is False and client_options[0]["trust_env"] is False
    assert observed["resourceSamples"]


def test_cli_writes_failure_report_for_retry_without_fake_measurement(tmp_path, monkeypatch):
    async def failed(**kwargs):
        return {
            "status": "failed",
            "measurement": None,
            "collectorReport": {"failure": {"code": "DOCKER_UNAVAILABLE"}},
        }

    monkeypatch.setattr(measure, "collect_measurement", failed)
    output = tmp_path / "measurement.json"
    result = measure.main(
        [
            "--container-id",
            CONTAINER,
            "--url",
            "http://127.0.0.1:8766/health",
            "--source-snapshot-id",
            SNAPSHOT,
            "--service-id",
            "api",
            "--out",
            str(output),
        ]
    )
    assert result == 1
    assert json.loads(output.read_text())["measurement"] is None
