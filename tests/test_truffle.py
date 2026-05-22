"""
test_truffle.py  --  tests for quota-based queueSize derivation.

No AWS calls, no truffle CLI invocations.
"""

from __future__ import annotations

from microbiome_demo.truffle import QuotaInfo, derive_queue_size, quota_summary


def _quota(family: str, available: int) -> QuotaInfo:
    return QuotaInfo(
        family=family,
        region="us-east-1",
        limit=available,
        used=0,
        available=available,
    )


def test_derive_queue_size_basic():
    quotas = {"c7g": _quota("c7g", 192), "t4g": _quota("t4g", 384)}
    # 192 × 0.8 = 153 usable vCPUs; largest instance = c7g.4xlarge = 16 vCPU → 9
    qs = derive_queue_size(quotas, ["c7g.4xlarge", "t4g.medium"])
    assert qs >= 4
    assert qs <= 30


def test_derive_queue_size_constrained():
    # Very low quota → floor at _MIN_QUEUE_SIZE
    quotas = {"c7g": _quota("c7g", 8)}
    qs = derive_queue_size(quotas, ["c7g.4xlarge"])
    assert qs == 4  # 8 × 0.8 = 6 / 16 = 0 → floor


def test_derive_queue_size_no_quotas():
    qs = derive_queue_size({}, ["c7g.4xlarge"])
    assert qs == 4


def test_derive_queue_size_unknown_family():
    # Instance family not in quotas → falls back to minimum
    quotas = {"m6i": _quota("m6i", 256)}
    qs = derive_queue_size(quotas, ["c7g.4xlarge"])
    assert qs == 4


def test_quota_summary_with_data():
    quotas = {"c7g": _quota("c7g", 192), "t4g": _quota("t4g", 64)}
    summary = quota_summary(quotas, 12)
    assert "12" in summary
    assert "c7g" in summary


def test_quota_summary_empty():
    summary = quota_summary({}, 4)
    assert "4" in summary
    assert "unavailable" in summary
