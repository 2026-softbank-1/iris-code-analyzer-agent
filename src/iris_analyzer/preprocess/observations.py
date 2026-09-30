"""Evidence-backed observations accumulated before budget pruning."""

from __future__ import annotations

import hashlib

from iris_analyzer.contracts import canonical_bytes, digest

from .redaction import redact
from .snapshot import Snapshot


class Observations:
    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.evidence: dict[str, dict] = {}
        self.facts: list[dict] = []
        self.relations: list[dict] = []
        self.unresolved: list[dict] = []
        self.current_candidate_id: str | None = None
        self.current_condition: str | None = None

    def snippet(self, path: str, start: int = 1, end: int | None = None) -> str:
        text = self.snapshot.files[path].decode("utf-8-sig")
        source_lines = text.splitlines()
        count = max(1, len(source_lines))
        start = max(1, min(start, count))
        end = max(start, min(end or count, count))
        provided, redacted = redact("\n".join(source_lines[start - 1 : end]), path)
        source_digest = hashlib.sha256(self.snapshot.files[path]).hexdigest()
        content_digest = hashlib.sha256(provided.encode("utf-8")).hexdigest()
        identifier = (
            "e-"
            + digest(
                {
                    "path": path,
                    "sourceDigest": source_digest,
                    "startLine": start,
                    "endLine": end,
                    "contentDigest": content_digest,
                }
            )[:20]
        )
        self.evidence[identifier] = {
            "evidenceId": identifier,
            "path": path,
            "startLine": start,
            "endLine": end,
            "sourceDigest": source_digest,
            "contentDigest": content_digest,
            "redacted": redacted,
            "text": provided,
        }
        return identifier

    def fact(
        self,
        key: str,
        value,
        component: str,
        scope: str,
        evidence_ids: list[str],
        condition: str | None = None,
    ) -> None:
        if not evidence_ids:
            raise ValueError("Facts must carry source evidence")
        value = _mask_value(value)
        condition = condition if condition is not None else self.current_condition
        fact = {
            "key": key,
            "value": value,
            "scope": scope,
            "component": component,
            "evidenceIds": sorted(set(evidence_ids)),
        }
        if self.current_candidate_id is not None:
            fact["candidateId"] = self.current_candidate_id
        if condition is not None:
            fact["condition"] = condition
        identity = (key, canonical_bytes(value), component, scope, condition, self.current_candidate_id)
        for existing in self.facts:
            if (
                existing["key"],
                canonical_bytes(existing["value"]),
                existing.get("component", "."),
                existing["scope"],
                existing.get("condition"),
                existing.get("candidateId"),
            ) == identity:
                existing["evidenceIds"] = sorted(set(existing["evidenceIds"] + fact["evidenceIds"]))
                return
        self.facts.append(fact)

    def relation(self, kind: str, origin: str, target: str, evidence_ids: list[str], **details) -> None:
        value = {
            "type": kind,
            "from": origin,
            "to": target,
            "evidenceIds": sorted(set(evidence_ids)),
            **details,
        }
        if value not in self.relations:
            self.relations.append(value)

    def unknown(self, key: str, reason: str, **details) -> None:
        value = {"key": key, "reason": reason, **details}
        if value not in self.unresolved:
            self.unresolved.append(value)


def _mask_value(value):
    if isinstance(value, str):
        return redact(value, "observation")[0]
    if isinstance(value, list):
        return [_mask_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _mask_value(item) for key, item in value.items()}
    return value
