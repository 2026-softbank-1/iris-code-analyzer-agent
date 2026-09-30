"""Optional integration checks use a real pinned server, never a paid model call."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from iris_analyzer.contracts import MODEL_REPLY_SCHEMA
from iris_analyzer.opencode import IsolatedOpenCodeServer, ModelConfig, OpenCodeClient, OpenCodeRunner


def test_pinned_server_isolates_developer_configuration_and_validates_doc(monkeypatch, tmp_path) -> None:
    executable = os.environ.get("IRIS_OPENCODE_EXECUTABLE")
    if not executable or not Path(executable).is_file():
        pytest.skip("Set IRIS_OPENCODE_EXECUTABLE to run the pinned real-server integration check")
    poison = tmp_path / "opencode.json"
    poison.write_text('{"permission":{"*":"allow"},"instructions":["AGENTS.md"]}')
    monkeypatch.setenv("OPENCODE_CONFIG", str(poison))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", poison.read_text())
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-developer-secret")
    server = IsolatedOpenCodeServer(ModelConfig(api_key="fixture-key"), executable=executable)
    with server:
        directory = server.directory
        assert directory is not None and list((directory / "workspace").iterdir()) == []
        assert server.environment["HOME"] != os.environ["HOME"]
        assert "OPENAI_API_KEY" not in server.environment
        assert "OPENCODE_CONFIG" not in server.environment
        with OpenCodeClient(server.config) as client:
            client.verify(time.monotonic() + 10)
            assert client.server_version == "1.18.33"
            effective = client.request("GET", "/config")
            assert effective["agent"]["iris-analyzer"]["steps"] == 2
            assert effective["agent"]["iris-analyzer"]["temperature"] == 0
            assert effective["permission"] == {"*": "deny", "StructuredOutput": "allow"}
            client.validate_request(
                "/session/{sessionID}/prompt_async",
                {
                    "model": {"providerID": server.config.provider, "modelID": server.config.model},
                    "agent": "iris-analyzer",
                    "parts": [{"type": "text", "text": "schema validation only"}],
                    "format": {"type": "json_schema", "schema": MODEL_REPLY_SCHEMA, "retryCount": 0},
                },
            )
            session = client.create_session(time.monotonic() + 5)
            assert session.startswith("ses")
            assert client.abort(session) is True
    assert not directory.exists()
    assert server.process is None
    assert server.environment == {}


def test_real_first_inference_has_no_max_steps_summary_and_no_repository_tools() -> None:
    """Capture the actual native model request at a localhost fake SSE provider."""
    executable = os.environ.get("IRIS_OPENCODE_EXECUTABLE")
    if not executable or not Path(executable).is_file():
        pytest.skip("Set IRIS_OPENCODE_EXECUTABLE for the unpaid native inference regression test")
    from test_opencode_transport import REPLY, bundle

    captured = []

    class LocalModel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            content = json.dumps(REPLY)
            chunks = [
                {
                    "id": "chat_local",
                    "object": "chat.completion.chunk",
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chat_local",
                    "object": "chat.completion.chunk",
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {
                    "id": "chat_local",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
            response = (
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    worker = threading.Thread(target=endpoint.serve_forever, daemon=True)
    worker.start()
    try:
        config = ModelConfig(
            api_key="fixture-key", hive_base_url=f"http://127.0.0.1:{endpoint.server_port}/v1"
        )
        with IsolatedOpenCodeServer(config, executable=executable) as server:
            with OpenCodeRunner(server.config) as runner:
                assert runner.invoke_model(bundle()) == REPLY
                assert runner.calls[0]["maxInferenceSteps"] == 2
                assert runner.calls[0]["usageCompleteForCall"] is True
        assert len(captured) == 1
        assert "CRITICAL - MAXIMUM STEPS REACHED" not in json.dumps(captured[0]["messages"])
        assert not captured[0].get("tools")
        assert captured[0]["max_tokens"] == config.max_output_tokens
        assert captured[0]["temperature"] == 0
        assert captured[0]["reasoning_effort"] == "low"
        assert captured[0]["response_format"] == {"type": "json_object"}
    finally:
        endpoint.shutdown()
        endpoint.server_close()
        worker.join(timeout=3)
