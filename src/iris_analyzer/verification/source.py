"""Read only immutable captured bytes or digest-verified complete evidence."""

import hashlib
from collections import defaultdict
from types import MappingProxyType

from ..contracts import AnalyzerError
from ..preprocess.observations import Observations
from ..preprocess.redaction import redact
from ..preprocess.selector import Selection
from ..preprocess.snapshot import Snapshot, get_snapshot
from ..readiness.source import _verified_text


class ImmutableSource:
    def __init__(self, bundle: dict):
        self.bundle = bundle
        self.evidence = {e["evidenceId"]: e for e in bundle["evidence"]}
        self.files: dict[str, bytes] = {}
        self.unavailable: list[str] = []
        rows = defaultdict(list)
        for item in self.evidence.values():
            rows[item["path"]].append(item)
        try:
            snapshot = get_snapshot(bundle["source"]["snapshotId"])
        except AnalyzerError as error:
            if error.code != "SNAPSHOT_UNAVAILABLE":
                raise
            snapshot = None
        manifests = {item["path"]: item for item in bundle["manifest"]}
        for selected in bundle["selectedFiles"]:
            path = selected["path"]
            expected = manifests[path]["digest"]
            raw = snapshot.files.get(path) if snapshot is not None else None
            if raw is not None and hashlib.sha256(raw).hexdigest() != expected:
                raise AnalyzerError(
                    "VERIFICATION_SOURCE_MISMATCH", "Captured source differs from context manifest"
                )
            if raw is None:
                text = _verified_text(rows[path], expected)
                if text is not None:
                    # _verified_text verifies raw bytes before normalizing newlines.
                    raw = text.encode()
            if raw is None:
                self.unavailable.append(path)
            else:
                self.files[path] = raw
                for row in rows[path]:
                    source_lines = raw.decode("utf-8-sig").splitlines()
                    expected_text, expected_redacted = redact(
                        "\n".join(source_lines[row["startLine"] - 1 : row["endLine"]]), path
                    )
                    if row["text"] != expected_text or row["redacted"] != expected_redacted:
                        raise AnalyzerError(
                            "VERIFICATION_SOURCE_MISMATCH", "Evidence text differs from captured source bytes"
                        )
        self.snapshot = Snapshot(
            bundle["source"]["snapshotId"],
            bundle["source"].get("commit"),
            MappingProxyType(self.files),
            tuple(bundle["manifest"]),
            tuple(bundle.get("policy", {}).get("excludedPaths", [])),
        )
        self.facts: list[dict] = []
        self.candidates: list[dict] = []
        self.fact_evidence: dict[str, dict] = {}
        if self.files:
            from ..preprocess.extractors.connections import extract_connections
            from ..preprocess.extractors.docker import extract_docker
            from ..preprocess.extractors.execution import extract_execution
            from ..preprocess.extractors.express import extract_express
            from ..preprocess.extractors.node import extract_node

            selection = Selection(self.snapshot).run(list(self.files))
            observations = Observations(self.snapshot)
            components = extract_node(selection, observations)
            extract_express(selection, observations)
            extract_connections(selection, observations)
            self.candidates = extract_docker(selection, observations, components)
            extract_execution(selection, observations)
            self.facts = observations.facts
            self.fact_evidence = observations.evidence

    def paths(self, identifiers: list[str]) -> list[str]:
        return sorted(
            {self.evidence[identifier]["path"] for identifier in identifiers if identifier in self.evidence}
        )

    def cited(self, identifiers: list[str], path: str, start: int, end: int) -> bool:
        return any(
            self.evidence[i]["path"] == path
            and not self.evidence[i]["redacted"]
            and self.evidence[i]["startLine"] <= end
            and start <= self.evidence[i]["endLine"]
            for i in identifiers
            if i in self.evidence
        )

    def supports_fact(self, identifiers: list[str], fact: dict) -> bool:
        rows = [self.fact_evidence[i] for i in fact["evidenceIds"] if i in self.fact_evidence]
        if not rows:
            return False
        for row in rows:
            covered = set()
            for identifier in identifiers:
                cited = self.evidence[identifier]
                if cited["path"] == row["path"] and not cited["redacted"]:
                    covered.update(range(cited["startLine"], cited["endLine"] + 1))
            if not set(range(row["startLine"], row["endLine"] + 1)) <= covered:
                return False
        return True
