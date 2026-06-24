"""
test_pipeline.py  --  tests for pipeline monitoring helpers.

No AWS calls.  Tests focus on pure logic (cost calculation, elapsed formatting).
"""

from __future__ import annotations

from microbiome_demo.pipeline import DataVolume, _fmt_elapsed


def test_fmt_elapsed_seconds():
    assert _fmt_elapsed(45) == "45s"


def test_fmt_elapsed_minutes():
    assert _fmt_elapsed(125) == "2m 5s"


def test_fmt_elapsed_zero():
    assert _fmt_elapsed(0) == "0s"


def test_data_volume_expansion_ratio():
    dv = DataVolume(roda_bytes_read=1_000_000, fastq_bytes=3_500_000)
    assert abs(dv.expansion_ratio - 3.5) < 0.001


def test_data_volume_expansion_ratio_zero():
    dv = DataVolume(roda_bytes_read=0, fastq_bytes=0)
    assert dv.expansion_ratio == 0.0


def test_data_volume_gb():
    dv = DataVolume(roda_bytes_read=2_000_000_000, fastq_bytes=7_000_000_000)
    assert abs(dv.roda_gb - 2.0) < 0.01
    assert abs(dv.fastq_gb - 7.0) < 0.01
