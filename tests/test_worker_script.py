"""
test_worker_script.py  --  tests for head node script generation.

No AWS calls.  Verifies that render() substitutes config values correctly.
render() takes four args: cfg, nf_config_key, srr_list_key, main_nf_key.
"""

from __future__ import annotations

import types

import pytest

from microbiome_demo.worker_script import render

NF_CFG_KEY  = "config/test-job/nextflow.config"
SRR_KEY     = "slices/test-job/srr_list.json"
MAIN_NF_KEY = "pipeline/test-job/main.nf"


@pytest.fixture
def mock_cfg():
    return types.SimpleNamespace(
        BUCKET="test-bucket",
        REGION="us-east-1",
        JOB_NAME="test-job",
        INSTANCE_COUNT=4,
    )


def test_render_substitutes_bucket(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "test-bucket" in script


def test_render_substitutes_region(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "us-east-1" in script


def test_render_substitutes_job_name(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "test-job" in script


def test_render_substitutes_slice_key(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, "slices/test-job/srr_list_002.json", MAIN_NF_KEY)
    assert "srr_list_002.json" in script


def test_render_reads_from_roda(mock_cfg):
    """Head script must reference RODA — workers pull SRA files from there."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "sra-pub-run-odp" in script


def test_render_is_bash(mock_cfg):
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert script.startswith("#!/bin/bash")


def test_render_has_completion_signal(mock_cfg):
    """Head must touch /tmp/SPAWN_COMPLETE so spawn knows it's done."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "SPAWN_COMPLETE" in script


def test_render_runs_nextflow(mock_cfg):
    """Head script must invoke nextflow run."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "nextflow run" in script


def test_render_uses_main_nf(mock_cfg):
    """Nextflow run command must reference main.nf (not nf-core/taxprofiler directly)."""
    script = render(mock_cfg, NF_CFG_KEY, SRR_KEY, MAIN_NF_KEY)
    assert "main.nf" in script
    # The run command targets main.nf; taxprofiler is included as a subworkflow
    assert "nextflow run /tmp/nf-head/main.nf" in script


def test_main_nf_contains_fetch_fastq():
    """The embedded main.nf (stage 1) must include the FETCH_FASTQ process."""
    from microbiome_demo.worker_script import _MAIN_NF

    assert "FETCH_FASTQ" in _MAIN_NF
    assert "sra-pub-run-odp" in _MAIN_NF
    assert "fasterq-dump" in _MAIN_NF
    # taxprofiler is stage 2, run separately via `nextflow run nf-core/taxprofiler`
    assert "samplesheet_for_taxprofiler.csv" in _MAIN_NF
