"""
test_worker_script.py  --  tests for worker script generation.

No AWS calls.  Verifies that render() substitutes config values correctly.
"""

from __future__ import annotations

import types

import pytest

from microbiome_demo.worker_script import render


@pytest.fixture
def mock_cfg():
    cfg = types.SimpleNamespace(
        BUCKET="test-bucket",
        REGION="us-east-1",
        JOB_NAME="test-job",
        INSTANCE_COUNT=4,
    )
    return cfg


def test_render_substitutes_bucket(mock_cfg):
    script = render(mock_cfg, "slices/test-job/samplesheet_000.csv")
    assert "test-bucket" in script


def test_render_substitutes_region(mock_cfg):
    script = render(mock_cfg, "slices/test-job/samplesheet_000.csv")
    assert "us-east-1" in script


def test_render_substitutes_job_name(mock_cfg):
    script = render(mock_cfg, "slices/test-job/samplesheet_000.csv")
    assert "test-job" in script


def test_render_substitutes_samplesheet_key(mock_cfg):
    script = render(mock_cfg, "slices/test-job/samplesheet_002.csv")
    assert "samplesheet_002.csv" in script


def test_render_is_bash(mock_cfg):
    script = render(mock_cfg, "test.csv")
    assert script.startswith("#!/bin/bash")


def test_render_has_completion_signal(mock_cfg):
    """Worker must touch /tmp/SPAWN_COMPLETE so spawn knows it's done."""
    script = render(mock_cfg, "test.csv")
    assert "SPAWN_COMPLETE" in script
