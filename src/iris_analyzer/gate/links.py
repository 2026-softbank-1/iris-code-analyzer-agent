"""Static links between deployment units: env bindings, host aliases and database profiles.

Everything here reads Compose ``environment`` values, nginx style config and
source string literals only to find *which* service a name points at. Values
are never returned: results carry service ids, ports, property names and
``path:line`` evidence. Passwords are reduced to a ``passwordInSource`` flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .scan import RepositoryScan

UNRESOLVED = "\x00"
DEFAULT_PORTS = {"postgres": 5432, "redis": 6379, "mysql": 3306, "mongodb": 27017}
DEFAULT_USERS = {"postgres": "postgres", "mysql": "root"}
MAX_SOURCE_FILES = 120
MAX_CONFIG_FILES = 40
MAX_EVIDENCE = 20

_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?([-?+=])([^}]*))?\}")
_BARE_VAR = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_URL = re.compile(
    r"^\s*([a-z][a-z0-9+.-]*)://(?:([^:@/\s]*)(?::([^@/\s]*))?@)?([^:/?#@\s]+)(?::(\d+))?(?:/([^?#\s]*))?"
)
_URL_FULL = re.compile(
    r"^([a-z][a-z0-9+.-]*)://(?:([^@/?#\s]*)@)?([^:/?#@\s]+)(?::(\d+))?([/?#]\S*)?$", re.I
)
_SECRET_PARAM = re.compile(
    r"(?:^|[?&#;])(?:[^=&#]*(?:password|passwd|pwd|secret|token|credential|api[_-]?key|access[_-]?key)"
    r"[^=&#]*|sig|signature)=",
    re.I,
)
_EMBEDDED_HOST = re.compile(r"(?:://|@)([A-Za-z0-9_.-]+)(?::(\d+))?")
_HOST_PORT = re.compile(r"^([A-Za-z0-9_.-]+):(\d+)$")
_SOURCE_LITERAL = re.compile(
    r"""['"`]((?:https?|wss?|grpcs?|postgres(?:ql)?|rediss?|mongodb(?:\+srv)?|mysql|mariadb|amqps?)://)"""
    r"""(?:[^'"`@/\s]*@)?([A-Za-z][A-Za-z0-9_.-]*)(?::(\d{2,5}))?"""
)
_PASS_DIRECTIVE = re.compile(
    r"^\s*(?:proxy_pass|fastcgi_pass|grpc_pass|uwsgi_pass)\s+(?:(?:https?|grpcs?)://)?"
    r"([A-Za-z][A-Za-z0-9_.-]*)(?::(\d{2,5}))?"
)
_UPSTREAM = re.compile(r"^\s*upstream\s+([A-Za-z0-9_.-]+)\s*\{")
_UPSTREAM_SERVER = re.compile(r"^\s*server\s+(?:https?://)?([A-Za-z][A-Za-z0-9_.-]*)(?::(\d{2,5}))?")
_SOURCE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx", ".py", ".go", ".rb")

_PASSWORD_KEYS = {
    "postgres": ("POSTGRES_PASSWORD",),
    "mysql": ("MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "MARIADB_ROOT_PASSWORD", "MARIADB_PASSWORD"),
    "mongodb": ("MONGO_INITDB_ROOT_PASSWORD",),
    "redis": ("REDIS_PASSWORD",),
}
_USER_KEYS = {
    "postgres": ("POSTGRES_USER",),
    "mysql": ("MYSQL_USER", "MARIADB_USER"),
    "mongodb": ("MONGO_INITDB_ROOT_USERNAME",),
}
_DATABASE_KEYS = {
    "postgres": ("POSTGRES_DB",),
    "mysql": ("MYSQL_DATABASE", "MARIADB_DATABASE"),
    "mongodb": ("MONGO_INITDB_DATABASE",),
}
_ENGINE_KEYS = (
    (re.compile(r"(^|_)MONGO(DB)?_(URL|URI)$", re.I), "mongodb"),
    (re.compile(r"(^|_)REDIS_(URL|URI)$", re.I), "redis"),
    (re.compile(r"(^|_)MYSQL_(URL|URI)$", re.I), "mysql"),
    (re.compile(r"(^|_)(POSTGRES(QL)?|PG)_(URL|URI)$", re.I), "postgres"),
)
_GENERIC_DATABASE_KEY = re.compile(r"(^|_)(DATABASE|DB)_(URL|URI)$", re.I)
_SIBLINGS = (
    (re.compile(r"^(.*?)_?PORT$", re.I), "port"),
    (re.compile(r"^(.*?)_?(USER|USERNAME)$", re.I), "user"),
    (re.compile(r"^(.*?)_?(PASSWORD|PASS|PASSWD)$", re.I), "password"),
    (re.compile(r"^(.*?)_?(DB|DATABASE|DB_NAME|DBNAME|NAME)$", re.I), "database"),
)
_HOST_KEY = re.compile(r"^(.*?)_?(HOST|HOSTNAME|ADDR|ADDRESS|SERVER)$", re.I)


def substitute(value: object) -> str:
    """Replace Compose interpolation by its default; anything unknown becomes ``UNRESOLVED``."""

    def replace(match: re.Match[str]) -> str:
        return match[2] if match[1] == "-" and match[2] else UNRESOLVED

    return _BARE_VAR.sub(UNRESOLVED, _VAR.sub(replace, "" if value is None else str(value)))


def resolved(value: str | None) -> str | None:
    return value if value and UNRESOLVED not in value else None


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    user: str | None
    password_literal: bool
    host: str | None
    port: int | None
    database: str | None


def parse_url(value: object) -> ParsedUrl | None:
    match = _URL.match(substitute(value))
    if not match:
        return None
    path = (match[6] or "").split("/")[0]
    return ParsedUrl(
        scheme=match[1],
        user=resolved(match[2]) or None,
        password_literal=bool(match[3]) and UNRESOLVED not in match[3],
        host=resolved(match[4]),
        port=int(match[5]) if match[5] and 0 < int(match[5]) < 65536 else None,
        database=resolved(path) or None,
    )


def env_references(value: object, names: dict[str, str]) -> list[tuple[str, int | None]]:
    """(host, port) pairs in a Compose environment value that name a known service."""
    text = substitute(value)
    found: list[tuple[str, int | None]] = []
    stripped = text.strip()
    if stripped in names:
        found.append((stripped, None))
    exact = _HOST_PORT.match(stripped)
    if exact and exact[1] in names and 0 < int(exact[2]) < 65536:
        found.append((exact[1], int(exact[2])))
    for match in _EMBEDDED_HOST.finditer(text):
        if match[1] in names:
            found.append((match[1], int(match[2]) if match[2] else None))
    return list(dict.fromkeys(found))


def whole_url_host(value: object) -> ParsedUrl | None:
    parsed = parse_url(value)
    return parsed if parsed and parsed.host else None


def dependency_profile(engine: str, service) -> dict:
    """Port, database, user and a literal-password flag from a Compose database service."""
    values = {key: substitute(value) for key, value in service.env_values.items()}

    def first(keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = resolved(values.get(key))
            if value:
                return value
        return None

    port = next((port for port, _ in service.ports), None) or DEFAULT_PORTS.get(engine)
    in_source = any(
        bool(values.get(key)) and UNRESOLVED not in values[key] for key in _PASSWORD_KEYS.get(engine, ())
    ) or bool(re.search(r"--requirepass[= ]+(?![$'\"]?\$)\S+", service.command or ""))
    return {
        "port": port,
        "database": first(_DATABASE_KEYS.get(engine, ())),
        "user": first(_USER_KEYS.get(engine, ())),
        "passwordInSource": in_source,
    }


def url_parts(value: object) -> dict | None:
    """Scheme, exact path+query+fragment and a credentials flag of a whole URL value.

    Userinfo is dropped (only reported as ``hasCredentials``). None when the URL cannot
    be reduced safely: unresolved interpolation after the host, ``+srv`` schemes, or a
    secret-looking query parameter.
    """
    text = substitute(value).strip()
    match = _URL_FULL.match(text)
    if not match or UNRESOLVED in (match[1] + (match[3] or "") + (match[5] or "")):
        return None
    scheme, suffix = match[1], match[5] or ""
    if scheme.lower().endswith("+srv") or _SECRET_PARAM.search(suffix):
        return None
    return {"scheme": scheme, "urlSuffix": suffix, "hasCredentials": match[2] is not None}


def key_binding(key: str, value: object, names: dict[str, str], kinds: dict[str, str]) -> dict | None:
    """Binding from a Compose value: ``{kind, targetId, property}`` or None."""
    parsed = whole_url_host(value)
    if parsed and parsed.host in names:
        parts = url_parts(value)
        if parts is None:
            return None
        return {
            "kind": kinds[names[parsed.host]],
            "targetId": names[parsed.host],
            "property": "url",
            **parts,
        }
    text = substitute(value).strip()
    if text in names and _HOST_KEY.match(key):
        return {"kind": kinds[names[text]], "targetId": names[text], "property": "host"}
    exact = _HOST_PORT.match(text)
    if exact and exact[1] in names:
        return {"kind": kinds[names[exact[1]]], "targetId": names[exact[1]], "property": "host"}
    return None


def sibling_bindings(rows: dict[str, dict | None], kinds: dict[str, str]) -> None:
    """``DB_PORT``/``DB_USER``... follow a ``DB_HOST`` bound to a service with the same prefix."""
    hosts = {}
    for key, binding in rows.items():
        match = _HOST_KEY.match(key)
        if binding and binding["property"] == "host" and match:
            hosts[match[1].upper()] = binding
    for key in rows:
        if rows[key] is not None:
            continue
        for pattern, prop in _SIBLINGS:
            match = pattern.match(key)
            host = hosts.get(match[1].upper()) if match else None
            if host and (prop in {"port"} or host["kind"] == "dependency"):
                rows[key] = {"kind": host["kind"], "targetId": host["targetId"], "property": prop}
                break


def heuristic_binding(
    key: str, depends: list[str], engines: dict[str, str], global_by_engine: dict[str, str | None]
) -> dict | None:
    """Key-name heuristic (DATABASE_URL, REDIS_URL, ...) against dependencies of that engine."""
    engine = next((engine for pattern, engine in _ENGINE_KEYS if pattern.search(key)), None)
    candidates: list[str] = []
    if engine:
        candidates = [dep for dep in depends if engines.get(dep) == engine]
        if not candidates and global_by_engine.get(engine):
            candidates = [global_by_engine[engine]]
    elif _GENERIC_DATABASE_KEY.search(key):
        for wanted in ("postgres", "mysql"):
            candidates = [dep for dep in depends if engines.get(dep) == wanted]
            if candidates:
                break
        if not candidates:
            sql = [dep for dep in depends if engines.get(dep) in {"postgres", "mysql"}]
            candidates = sql
    if len(candidates) == 1:
        return {"kind": "dependency", "targetId": candidates[0], "property": "url"}
    return None


def line_of_key(text: str | None, start: int, key: str) -> int:
    if text:
        pattern = re.compile(rf"^\s*(?:-\s*)?['\"]?{re.escape(key)}['\"]?\s*[:=]")
        for number, line in enumerate(text.splitlines()[start - 1 :], start):
            if pattern.match(line):
                return number
    return start


def config_aliases(scan: RepositoryScan, names: dict[str, str]) -> list[tuple[str, str, int | None, int]]:
    """(path, host, port, line) from nginx style ``proxy_pass``/``upstream`` in ``*.conf`` files."""
    rows: list[tuple[str, str, int | None, int]] = []
    checked = 0
    for path in scan.files():
        name = path.rsplit("/", 1)[-1].lower()
        if not (name.endswith((".conf", ".conf.template", ".conf.erb")) or name.startswith("nginx")):
            continue
        checked += 1
        if checked > MAX_CONFIG_FILES:
            break
        text = scan.read(path)
        if not text or not re.search(r"proxy_pass|fastcgi_pass|grpc_pass|uwsgi_pass|upstream", text):
            continue
        upstreams: dict[str, list[tuple[str, int | None, int]]] = {}
        current = None
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            opened = _UPSTREAM.match(line)
            if opened:
                current = opened[1]
                upstreams[current] = []
                continue
            if current is not None:
                if "}" in line:
                    current = None
                    continue
                server = _UPSTREAM_SERVER.match(line)
                if server:
                    upstreams[current].append((server[1], int(server[2]) if server[2] else None, number))
                continue
            passed = _PASS_DIRECTIVE.match(line)
            if not passed:
                continue
            if passed[1] in upstreams:
                for host, port, server_line in upstreams[passed[1]]:
                    rows.append((path, host, port, server_line))
                    rows.append((path, host, port, number))
            else:
                rows.append((path, passed[1], int(passed[2]) if passed[2] else None, number))
    return [(path, host, port, line) for path, host, port, line in rows if host in names]


def source_aliases(
    scan: RepositoryScan, names: dict[str, str], paths: list[str]
) -> list[tuple[str, str, int | None, int]]:
    """(path, host, port, line) for string literals such as ``"http://api:3000"`` (bounded)."""
    rows = []
    checked = 0
    for path in paths:
        if not path.endswith(_SOURCE_SUFFIXES):
            continue
        checked += 1
        if checked > MAX_SOURCE_FILES:
            break
        text = scan.read(path)
        if not text or "://" not in text:
            continue
        for match in _SOURCE_LITERAL.finditer(text):
            if match[2] in names:
                port = int(match[3]) if match[3] else None
                rows.append((path, match[2], port, text.count("\n", 0, match.start()) + 1))
    return rows
