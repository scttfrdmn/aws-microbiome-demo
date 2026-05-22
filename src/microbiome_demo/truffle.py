"""
truffle.py  --  query AWS vCPU quotas via the truffle CLI.

Used before launch to derive a safe Nextflow queueSize that won't breach
the account's EC2 service quota.  truffle requires AWS credentials to
query quotas (unlike spot price lookups which are public).

truffle CLI: brew install spore-host/tap/truffle
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

# vCPU counts for instance types we actually use.
_VCPUS: dict[str, int] = {
    "t4g.medium": 2,
    "t4g.large": 2,
    "c7g.2xlarge": 8,
    "c7g.4xlarge": 16,
    "c7g.8xlarge": 32,
    "t4g.small": 2,
}

# Conservative floor — always allow at least this many concurrent tasks
# even if quota query fails or returns a suspiciously low number.
_MIN_QUEUE_SIZE = 4


@dataclass
class QuotaInfo:
    """vCPU quota details for a single instance family."""

    family: str
    region: str
    limit: int  # account's vCPU quota for this family
    used: int  # currently running vCPUs (if available)
    available: int  # limit - used


def query_quotas(region: str, families: list[str]) -> dict[str, QuotaInfo]:
    """Call truffle quotas for each instance family and return results.

    Args:
        region:   AWS region string (e.g. "us-east-1").
        families: list of instance families (e.g. ["c7g", "t4g"]).

    Returns:
        dict mapping family → QuotaInfo.  Missing families are omitted.
    """
    results: dict[str, QuotaInfo] = {}

    for family in families:
        try:
            proc = subprocess.run(
                [
                    "truffle",
                    "quotas",
                    "--regions",
                    region,
                    "--family",
                    family,
                    "-o",
                    "json",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                continue

            data = json.loads(proc.stdout)
            # truffle returns a list of quota objects; take the first matching
            # the requested region.
            for item in data if isinstance(data, list) else [data]:
                if item.get("region") == region or item.get("Region") == region:
                    limit = int(item.get("limit", item.get("Limit", 0)))
                    used = int(item.get("used", item.get("Used", 0)))
                    results[family] = QuotaInfo(
                        family=family,
                        region=region,
                        limit=limit,
                        used=used,
                        available=max(0, limit - used),
                    )
                    break

        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError):
            continue

    return results


def derive_queue_size(
    quotas: dict[str, QuotaInfo],
    instance_types: list[str],
    headroom_pct: float = 0.80,
) -> int:
    """Compute a safe Nextflow queueSize from quota data.

    Uses the most constrained family across all instance types we plan
    to run, reserves headroom_pct of available capacity (leaving some
    buffer for other workloads in the account), then converts to a task
    count using the largest instance type in the mix.

    Args:
        quotas:        dict from query_quotas().
        instance_types: all instance types that tasks may use.
        headroom_pct:  fraction of available quota to use (default 80%).

    Returns:
        Recommended queueSize integer (at least _MIN_QUEUE_SIZE).
    """
    if not quotas:
        return _MIN_QUEUE_SIZE

    # Find the available vCPUs for each family we care about.
    family_available: dict[str, int] = {}
    for itype in instance_types:
        family = itype.split(".")[0]  # "c7g.4xlarge" → "c7g"
        if family in quotas:
            family_available[family] = quotas[family].available

    if not family_available:
        return _MIN_QUEUE_SIZE

    # Use the most constrained family.
    min_available = min(family_available.values())
    usable_vcpus = int(min_available * headroom_pct)

    # Divide by the largest vCPU count in our mix (most constraining).
    max_vcpus_per_task = max((_VCPUS.get(itype, 2) for itype in instance_types), default=2)
    queue_size = max(_MIN_QUEUE_SIZE, usable_vcpus // max_vcpus_per_task)

    return queue_size


def quota_summary(quotas: dict[str, QuotaInfo], queue_size: int) -> str:
    """Return a one-line human-readable summary for the dashboard."""
    if not quotas:
        return f"Queue size: {queue_size} (quota unavailable — using default)"

    parts = [f"{f}: {q.available} vCPUs free" for f, q in sorted(quotas.items())]
    return f"Queue size: {queue_size}  ({', '.join(parts)})"
