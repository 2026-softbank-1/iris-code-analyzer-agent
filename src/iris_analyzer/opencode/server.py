"""Launch a platform-owned OpenCode process in an empty, disposable environment.

This prevents loading analyzed repositories or developer configuration. It is a
tool/configuration isolation boundary, not an operating-system process sandbox.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from iris_analyzer.contracts import AnalyzerError

from .config import ModelConfig


class IsolatedOpenCodeServer:
    def __init__(
        self, config: ModelConfig, *, executable: str | Path = "opencode", startup_timeout_seconds: float = 30
    ) -> None:
        self.original_config = config
        self.config = config
        self.executable = str(executable)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.directory: Path | None = None
        self.environment: dict[str, str] = {}
        self.process: subprocess.Popen | None = None

    def _configuration(self) -> dict:
        config = self.original_config
        result: dict = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{config.provider}/{config.model}",
            "small_model": f"{config.provider}/{config.model}",
            "enabled_providers": [config.provider],
            "default_agent": "iris-analyzer",
            "autoupdate": False,
            "share": "disabled",
            "snapshot": False,
            "plugin": [],
            "mcp": {},
            "instructions": [],
            "permission": {"*": "deny", "StructuredOutput": "allow"},
            "compaction": {"auto": False, "prune": False},
            "agent": {
                "iris-analyzer": {
                    "description": "Deployment analysis of supplied JSON data only",
                    "prompt": "You are Iris, a deployment analyst. Follow the platform system prompt and analyze only supplied JSON data. Never execute code or use repository tools.",
                    "mode": "primary",
                    "temperature": 0,
                    "permission": {"*": "deny", "StructuredOutput": "allow"},
                    "steps": config.max_inference_steps,
                },
                "title": {"disable": True},
                "summary": {"disable": True},
                "compaction": {"disable": True},
            },
        }
        if config.provider == "hive-ai":
            model_options = {"reasoningEffort": config.reasoning_effort} if config.reasoning_effort else {}
            if config.native_json_mode:
                model_options["response_format"] = {"type": "json_object"}
            result["provider"] = {
                config.provider: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Hive",
                    "options": {
                        "baseURL": config.hive_base_url,
                        "apiKey": "{env:HIVE_AI}",
                        "maxRetries": 0,
                        "timeout": int(config.timeout_seconds * 1000),
                    },
                    "models": {
                        config.model: {
                            "name": config.model,
                            "temperature": True,
                            "options": model_options,
                            "limit": {"context": 1_000_000, "output": config.max_output_tokens},
                            "modalities": {"input": ["text"], "output": ["text"]},
                        },
                    },
                },
            }
        return result

    def start(self) -> IsolatedOpenCodeServer:
        if self.process is not None:
            return self
        executable = (
            shutil.which(self.executable) if not Path(self.executable).is_absolute() else self.executable
        )
        if not executable or not Path(executable).is_file():
            raise AnalyzerError("OPENCODE_EXECUTABLE_MISSING", "Install the pinned OpenCode executable")
        if self.original_config.provider == "hive-ai" and not self.original_config.api_key:
            raise AnalyzerError("MODEL_AUTH_MISSING", "HIVE_AI is required to start the Hive provider")
        self.directory = Path(tempfile.mkdtemp(prefix="iris-opencode-"))
        for name in ("workspace", "home", "config", "data", "cache", "state", "platform"):
            (self.directory / name).mkdir(mode=0o700)
        password = secrets.token_urlsafe(32)
        # Deliberately do not inherit provider credentials, development variables,
        # OPENCODE_* settings, plugin paths, NODE_OPTIONS or BUN_OPTIONS.
        self.environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.directory / "home"),
            "XDG_CONFIG_HOME": str(self.directory / "config"),
            "XDG_DATA_HOME": str(self.directory / "data"),
            "XDG_CACHE_HOME": str(self.directory / "cache"),
            "XDG_STATE_HOME": str(self.directory / "state"),
            "OPENCODE_CONFIG_DIR": str(self.directory / "platform"),
            "OPENCODE_TEST_MANAGED_CONFIG_DIR": str(self.directory / "platform"),
            "OPENCODE_CONFIG_CONTENT": json.dumps(self._configuration()),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
            "OPENCODE_DISABLE_CLAUDE_CODE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_AUTOCOMPACT": "true",
            "OPENCODE_DISABLE_PRUNE": "true",
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_SERVER_USERNAME": "opencode",
        }
        if self.original_config.api_key:
            self.environment["HIVE_AI"] = self.original_config.api_key
        try:
            version = subprocess.run(
                [executable, "--version"],
                cwd=self.directory / "workspace",
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            if version != self.original_config.expected_version:
                raise AnalyzerError(
                    "OPENCODE_VERSION_MISMATCH",
                    "OpenCode executable differs from the tested pin",
                    {
                        "expectedVersion": self.original_config.expected_version,
                        "actualVersion": version,
                    },
                )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.config = replace(
                self.original_config, server_url=f"http://127.0.0.1:{port}", server_password=password
            )
            self.process = subprocess.Popen(
                [executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=self.directory / "workspace",
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + self.startup_timeout_seconds
            with httpx.Client(auth=("opencode", password), trust_env=False, timeout=1) as client:
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise AnalyzerError(
                            "OPENCODE_START_FAILED",
                            "Isolated OpenCode process exited before readiness",
                            {"exitCode": self.process.returncode},
                        )
                    try:
                        response = client.get(self.config.server_url + "/global/health")
                        if response.is_success and response.json().get("healthy") is True:
                            return self
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.1)
            raise AnalyzerError("OPENCODE_START_FAILED", "Isolated OpenCode server did not become ready")
        except (subprocess.SubprocessError, OSError) as exc:
            self.close()
            raise AnalyzerError(
                "OPENCODE_START_FAILED", "Cannot start the pinned OpenCode executable"
            ) from exc
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None
        self.environment.clear()

    def __enter__(self) -> IsolatedOpenCodeServer:
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.close()
