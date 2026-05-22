"""
config.example.py  --  copy to config.py and fill in before running anything.

    cp config.example.py config.py

config.py is git-ignored. Never commit real account IDs or bucket names.
"""

# --- account / region -------------------------------------------------------
REGION = "us-east-1"  # must be us-east-1 (RODA is there; no egress cost)
ACCOUNT_ID = "000000000000"  # your 12-digit AWS account ID
BUCKET = "your-microbiome-demo-bucket"  # S3 bucket for results + work dir

# --- samples ----------------------------------------------------------------
# How many HMP WGS samples to use.  100 gives the full story; 20 works for
# a quick rehearsal run.  Accessions are split evenly across three body sites
# (stool, buccal_mucosa, anterior_nares).
# HMP data is read DIRECTLY from RODA (s3://sra-pub-run-odp/) at runtime —
# no staging into your bucket needed.
SAMPLE_COUNT = 100

# --- AMI --------------------------------------------------------------------
# Filled in by build_ami.py after the AMI is baked.
# The AMI pre-installs: Nextflow, SRA toolkit (ARM64), nf-core/taxprofiler
# Singularity image, Kraken2 k2_pluspf_16GB database, MetaPhlAn 4,
# spawn CLI, and the nf-spawn Nextflow executor plugin.
AMI_ID = ""

# --- Nextflow head instance -------------------------------------------------
# A small instance that runs Nextflow + nf-spawn to orchestrate the pipeline.
# Each pipeline task gets its own purpose-sized instance (defined in
# nextflow_config.py by process label).
HEAD_INSTANCE_TYPE = "t4g.small"

# Auto-terminate after this long even if the pipeline hasn't completed.
INSTANCE_TTL = "3h"

# spawn job name — used to identify the running head instance via `spawn list`
JOB_NAME = "microbiome-demo"

# --- Bedrock / AI insights --------------------------------------------------
# Claude Sonnet synthesizes the analysis results into plain-language insights.
BEDROCK_REGION = "us-west-2"
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-6"

# --- web app ----------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8000
