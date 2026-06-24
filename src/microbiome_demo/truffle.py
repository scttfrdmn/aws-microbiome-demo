"""
truffle.py  --  query AWS EC2 quotas, vCPUs, and pricing via the truffle CLI.

truffle CLI: brew install spore-host/tap/truffle
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

# Conservative floor — always allow at least this many concurrent tasks
# even if quota query fails or returns a suspiciously low number.
_MIN_QUEUE_SIZE = 4


@dataclass
class QuotaInfo:
    """vCPU quota details for a single instance family."""

    family: str
    region: str
    limit: int
    used: int
    available: int


@dataclass
class InstanceSpec:
    """vCPU count and on-demand price for a single instance type."""

    instance_type: str
    vcpus: int
    on_demand_price_usd: float  # per hour


def _run_truffle(*args: str, timeout: int = 15) -> list[dict] | None:
    try:
        proc = subprocess.run(
            ["truffle", *args, "-o", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        return data if isinstance(data, list) else [data]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


@lru_cache(maxsize=64)
def get_instance_spec(instance_type: str, region: str) -> InstanceSpec | None:
    """Return vCPU count and on-demand price from truffle spot."""
    items = _run_truffle("spot", instance_type, "--regions", region)
    if not items:
        return None
    for item in items:
        if not item or item.get("instance_type") != instance_type:
            continue
        if item.get("region") != region:
            continue
        vcpus_raw = item.get("vcpus")
        price_raw = item.get("on_demand_price")
        if vcpus_raw is None or price_raw is None:
            continue
        return InstanceSpec(
            instance_type=instance_type,
            vcpus=int(vcpus_raw),
            on_demand_price_usd=float(price_raw),
        )
    return None


def get_instance_specs(instance_types: list[str], region: str) -> dict[str, InstanceSpec]:
    """Return InstanceSpec for each type that truffle knows about."""
    specs = {}
    for itype in instance_types:
        spec = get_instance_spec(itype, region)
        if spec:
            specs[itype] = spec
    return specs


def query_quotas(region: str, families: list[str]) -> dict[str, QuotaInfo]:
    """Call truffle quotas for the Standard family (covers all Graviton/x86).

    Returns dict mapping instance family → QuotaInfo.
    """
    results: dict[str, QuotaInfo] = {}
    items = _run_truffle("quotas", "--regions", region, "--family", "Standard")
    if not items:
        return results

    for item in items:
        if not item or not isinstance(item, dict):
            continue
        if item.get("region") != region:
            continue
        if item.get("type") != "On-Demand":
            continue
        limit = int(item.get("quota_vcpus", 0))
        used = int(item.get("usage_vcpus", 0))
        available = int(item.get("available_vcpus", max(0, limit - used)))
        quota = QuotaInfo(
            family="Standard",
            region=region,
            limit=limit,
            used=used,
            available=available,
        )
        # Apply this quota to every requested family (all fall under Standard)
        for family in families:
            results[family] = quota
        break

    return results


def derive_queue_size(
    quotas: dict[str, QuotaInfo],
    instance_types: list[str],
    region: str,
    headroom_pct: float = 0.80,
) -> int:
    """Compute a safe Nextflow queueSize from quota and instance spec data.

    Uses the most constrained family, reserves headroom_pct of available
    capacity, then divides by the largest vCPU count in the task mix.
    """
    if not quotas:
        return _MIN_QUEUE_SIZE

    min_available = min(q.available for q in quotas.values())
    usable_vcpus = int(min_available * headroom_pct)

    specs = get_instance_specs(instance_types, region)
    max_vcpus = max((s.vcpus for s in specs.values()), default=2)

    return max(_MIN_QUEUE_SIZE, usable_vcpus // max_vcpus)


def quota_summary(quotas: dict[str, QuotaInfo], queue_size: int) -> str:
    """Return a one-line human-readable summary for the dashboard."""
    if not quotas:
        return f"Queue size: {queue_size} (quota unavailable — using default)"

    # All families share the same Standard quota — show it once
    q = next(iter(quotas.values()))
    return f"Queue size: {queue_size}  ({q.available} vCPUs free, Standard On-Demand)"
