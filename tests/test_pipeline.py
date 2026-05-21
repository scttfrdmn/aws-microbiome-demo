"""
test_pipeline.py  --  tests for pipeline monitoring helpers.

No AWS calls.  Tests focus on pure logic (cost calculation, elapsed formatting).
"""

from __future__ import annotations

from microbiome_demo.pipeline import _fmt_elapsed, _instance_price


def test_instance_price_c7g():
    """c7g.4xlarge should return the expected hourly rate."""
    price = _instance_price("c7g.4xlarge")
    assert abs(price - 0.6528) < 0.0001


def test_instance_price_fallback():
    """Unknown instance types should fall back to c7g.4xlarge price."""
    price = _instance_price("x9z.99xlarge")
    assert price == _instance_price("c7g.4xlarge")


def test_fmt_elapsed_seconds():
    assert _fmt_elapsed(45) == "45s"


def test_fmt_elapsed_minutes():
    assert _fmt_elapsed(125) == "2m 5s"


def test_fmt_elapsed_zero():
    assert _fmt_elapsed(0) == "0s"
