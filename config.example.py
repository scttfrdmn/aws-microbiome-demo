"""
config.example.py  --  copy to config.py and fill in before running anything.

    cp config.example.py config.py

config.py is git-ignored. Never commit real account IDs or bucket names.
"""

# --- account / region -------------------------------------------------------
REGION = "us-east-1"  # must match your spawn region
ACCOUNT_ID = "000000000000"  # your 12-digit AWS account ID
BUCKET = "your-microbiome-demo-bucket"  # S3 bucket for corpus + results

# --- corpus -----------------------------------------------------------------
# How many HMP WGS samples to use.  100 gives the full story; 20 works for
# a quick rehearsal run.  The corpus_prep.py script selects this many
# samples across three body sites (stool, buccal_mucosa, anterior_nares).
SAMPLE_COUNT = 100

# SRA accession list for the Human Microbiome Project WGS samples.
# corpus_prep.py populates this automatically; you can also override with your
# own list of SRR accessions.
SRA_ACCESSIONS_FILE = "hmp_accessions.txt"  # created by corpus_prep.py

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
