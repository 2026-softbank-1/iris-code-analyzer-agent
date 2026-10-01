"""Reviewed EKS lifecycle snapshot; unknown/stale support cannot authorize compile."""

from datetime import datetime, timezone

OBSERVED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
SOURCE_URL = "https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html"
VERSIONS = {
    "1.31": ("2025-11-26", "2026-11-26"),
    "1.32": ("2026-03-23", "2027-03-23"),
    "1.33": ("2026-07-29", "2027-07-29"),
    "1.34": ("2026-12-02", "2027-12-02"),
    "1.35": ("2027-03-27", "2028-03-27"),
    "1.36": ("2027-08-02", "2028-08-02"),
}


def eks_support(version: str, *, now=None):
    now = now or datetime.now(timezone.utc)
    if not 0 <= (now - OBSERVED_AT).total_seconds() <= 30 * 86400 or version not in VERSIONS:
        return None
    standard, extended = [datetime.fromisoformat(d).replace(tzinfo=timezone.utc) for d in VERSIONS[version]]
    return "standard" if now < standard else "extended" if now < extended else None
