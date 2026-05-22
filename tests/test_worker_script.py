"""
test_worker_script.py  --  tests for head node script generation.

No AWS calls.  Verifies that render() substitutes config values correctly.
render() now takes three args: cfg, nf_config_key, srr_list_key.
"""

from __future__ import annotations

import types

import pytest

from microbiome_demo.worker_script import render

NF_CFG_KEY = "config/test-job/nextflow.config"
SRR_KEY = "slices/test-job/srr_list.json"


@pytest.fixture
def mock_cfg():
    return types.SimpleNamespace(
        BUCKET="test-bucket",
        REGION="us-east-1",
        JOB_NAME="test-job",
        INSTANCE_COUNT=4,
    )


def test_render_substitutes_bucket(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert "test-bucket" in script


def test_render_substitutes_region(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert "us-east-1" in script


def test_render_substitutes_job_name(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert "test-job" in script


def test_render_substitutes_slice_key(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, "slices/test-job/srr_list_002.json")
    assert "srr_list_002.json" in script


def test_render_reads_from_roda(mock_cfg):
    """Head script must reference RODA — workers pull SRA files from there."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert "sra-pub-run-odp" in script


def test_render_is_bash(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert script.startswith("#!/bin/bash")


def test_render_has_completion_signal(mock_cfg):
    """Head must touch /tmp/SPAWN_COMPLETE so spawn knows it's done."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert "SPAWN_COMPLETE" in script


def test_render_runs_nextflow(mock_cfg):
    """Head script must invoke nextflow run."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY)
    assert "nextflow run" in script
