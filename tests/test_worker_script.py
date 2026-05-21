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
    script = render(mock_cfg, "slices/test-job/srr_list_000.json")
    assert "test-bucket" in script


def test_render_substitutes_region(mock_cfg):
    script = render(mock_cfg, "slices/test-job/srr_list_000.json")
    assert "us-east-1" in script


def test_render_substitutes_job_name(mock_cfg):
    script = render(mock_cfg, "slices/test-job/srr_list_000.json")
    assert "test-job" in script


def test_render_substitutes_slice_key(mock_cfg):
    script = render(mock_cfg, "slices/test-job/srr_list_002.json")
    assert "srr_list_002.json" in script


def test_render_reads_from_roda(mock_cfg):
    """Worker must pull SRA files from RODA, not from our S3 bucket."""
    script = render(mock_cfg, "test.json")
    assert "sra-pub-run-odp" in script
    assert "--no-sign-request" in script


def test_render_is_bash(mock_cfg):
    script = render(mock_cfg, "test.json")
    assert script.startswith("#!/bin/bash")


def test_render_has_completion_signal(mock_cfg):
    """Worker must touch /tmp/SPAWN_COMPLETE so spawn knows it's done."""
    script = render(mock_cfg, "test.json")
    assert "SPAWN_COMPLETE" in script
