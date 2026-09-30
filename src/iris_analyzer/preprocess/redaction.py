"""Line-preserving masking before any source snippet leaves preprocessing."""

from __future__ import annotations

import re

from .snapshot import is_env_example

_SENSITIVE = r"(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)[\w.-]*)"
_ASSIGNMENT = re.compile(rf"(?i)([\"']?\b{_SENSITIVE}\b[\"']?\s*[:=]\s*)([\"'`])([^\n]*?)(\2)")
_ENV_ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*)([^\n]*)$", re.MULTILINE)
_SENSITIVE_ENV = re.compile(rf"(?im)^(\s*(?:ENV\s+)?{_SENSITIVE}\s*[:=]\s*)([^\n]*)$")
_INLINE_ASSIGNMENT = re.compile(rf"(?i)(\b{_SENSITIVE}\b\s*=\s*)(?:[\"'][^\n\"']*[\"']|[^\s\n]+)")
_URI_CREDENTIAL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s'\"`]+):([^/\s'\"`]+)@")
_KNOWN_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b")
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_FLAG = r"--(?:[\w-]*(?:token|password|passwd|secret|api-key|api_key|access-key|private-key)[\w-]*)"
_CLI_FLAG = re.compile(rf"(?i)({_SECRET_FLAG}(?:\s+|=))(?:[\"'][^\n\"']*[\"']|[^\s\n]+)")
_JSON_FLAG = re.compile(rf"(?i)([\"']{_SECRET_FLAG}[\"']\s*,\s*)([\"'])([^\n]*?)(\2)")


def redact(text: str, path: str) -> tuple[str, bool]:
    original = text
    if is_env_example(path):
        text = _ENV_ASSIGNMENT.sub(lambda m: m[1] + "<REDACTED>", text)
    text = _ASSIGNMENT.sub(lambda m: m[1] + m[2] + "<REDACTED>" + m[4], text)
    text = _SENSITIVE_ENV.sub(lambda m: m[1] + "<REDACTED>", text)
    text = _INLINE_ASSIGNMENT.sub(lambda m: m[1] + "<REDACTED>", text)
    text = _URI_CREDENTIAL.sub(lambda m: m[1] + "<REDACTED>@", text)
    text = _KNOWN_TOKEN.sub("<REDACTED>", text)
    text = _BEARER.sub(lambda m: m[1] + "<REDACTED>", text)
    text = _JSON_FLAG.sub(lambda m: m[1] + m[2] + "<REDACTED>" + m[4], text)
    text = _CLI_FLAG.sub(lambda m: m[1] + "<REDACTED>", text)
    in_key = False
    lines = []
    for line in text.splitlines(keepends=True):
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            in_key = True
        if in_key:
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append("<REDACTED>" + newline)
        else:
            lines.append(line)
        if "-----END " in line and "PRIVATE KEY-----" in line:
            in_key = False
    text = "".join(lines)
    assert text.count("\n") == original.count("\n")
    return text, text != original
