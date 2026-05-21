"""
config.example.py  --  copy to config.py and fill in before running anything.

    cp config.example.py config.py

config.py is git-ignored. Never commit real account IDs or bucket names.
"""

# --- account / region -------------------------------------------------------
REGION = "us-east-1"  # must match your spawn region
ACCOUNT_ID = "000000000000"  # your 12-digit AWS account ID
BUCKET = "your-microbiome-demo-bucket"  # S3 bucket for results only (not HMP data)

# --- samples ----------------------------------------------------------------
# How many HMP WGS samples to use.  100 gives the full story; 20 works for
# a quick rehearsal run.  Accessions are split evenly across
# three body sites (stool, buccal_mucosa, anterior_nares).
# HMP data is read DIRECTLY from RODA (s3://sra-pub-run-odp/) at runtime —
# no staging into your bucket needed.
SAMPLE_COUNT = 100

# --- AMI / instance ---------------------------------------------------------
# Filled in by build_ami.py after the AMI is baked.
AMI_ID = ""

# Instance type for each worker node.
# c7g.4xlarge = 16 vCPU, 32 GB RAM (Graviton3, ARM64)
# Kraken2 k2_pluspf_16_GB database needs ~12 GB RAM; 32 GB gives headroom.
INSTANCE_TYPE = "c7g.4xlarge"

# Number of parallel worker instances.
# 8 × 16 vCPU = 128 vCPU total → ~100 samples in 12-18 minutes.
INSTANCE_COUNT = 8

# Auto-terminate after this long even if the job hasn't completed.
# Set longer than your expected runtime so demos don't die mid-run.
INSTANCE_TTL = "2h"

# spawn job name — used to track the running instance via `spawn list`
JOB_NAME = "microbiome-demo"

# --- Bedrock / AI insights --------------------------------------------------
# Claude Sonnet for synthesizing the analysis results into plain-language
# insights at the end of the demo.
BEDROCK_REGION = "us-west-2"
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-6"

# --- web app ----------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8000
