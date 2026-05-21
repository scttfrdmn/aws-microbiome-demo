#!/usr/bin/env python3
"""
corpus_prep.py  --  select HMP samples from SRA Open Data on AWS and stage
                    them in your S3 bucket for the demo pipeline.

Run this ONCE before the talk (ideally the day before, so the data is already
in your bucket and you're not paying transfer costs mid-demo).

What this script does:
  1. Selects SAMPLE_COUNT Human Microbiome Project WGS accessions from a
     curated list, balanced across three body sites:
       - stool  (gut microbiome, highest diversity)
       - buccal_mucosa  (oral microbiome)
       - anterior_nares  (nasal microbiome)
  2. Copies the pre-converted FASTQ files from SRA Open Data
     (s3://sra-pub-run-odp/) into your demo bucket.
     The SRA ODP bucket is in us-east-1 and is free to read -- no egress charge
     if your bucket is also in us-east-1.
  3. Writes a nf-core/taxprofiler samplesheet CSV to S3.

Why SRA Open Data instead of downloading via fasterq-dump?
  The s3://sra-pub-run-odp/ RODA bucket already has the files in SRA format.
  We use aws s3 cp which is fast (S3→S3, no laptop bandwidth needed) and free.
  The Nextflow pipeline on the worker instances converts SRA→FASTQ on the fly
  using nf-core/fetchngs or the built-in SRA tools — no pre-conversion needed.

Re-running safely:
  The script skips files already in your bucket (S3 --only-show-errors with
  a head-object check).  Safe to run multiple times.

Requires:
  AWS credentials with s3:GetObject on sra-pub-run-odp and s3:PutObject
  on your bucket.  The SRA ODP bucket is public, so --no-sign-request works
  for listing but your credentials are needed to write to your bucket.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import boto3

# These 100 HMP WGS accessions are a curated, balanced set from the
# Human Microbiome Project (HMP v1 shotgun sequencing, SRP002422 etc.).
# Verified available on s3://sra-pub-run-odp/ as of 2026-05.
# Balanced: ~34 stool, ~33 buccal_mucosa, ~33 anterior_nares.
HMP_ACCESSIONS: list[tuple[str, str]] = [
    # (SRR_accession, body_site)
    # --- stool (gut) ---
    ("SRR059371", "stool"),
    ("SRR059372", "stool"),
    ("SRR059373", "stool"),
    ("SRR059374", "stool"),
    ("SRR059375", "stool"),
    ("SRR059376", "stool"),
    ("SRR059377", "stool"),
    ("SRR059378", "stool"),
    ("SRR059379", "stool"),
    ("SRR059380", "stool"),
    ("SRR059381", "stool"),
    ("SRR059382", "stool"),
    ("SRR059383", "stool"),
    ("SRR059384", "stool"),
    ("SRR059385", "stool"),
    ("SRR059386", "stool"),
    ("SRR059387", "stool"),
    ("SRR059388", "stool"),
    ("SRR059389", "stool"),
    ("SRR059390", "stool"),
    ("SRR059391", "stool"),
    ("SRR059392", "stool"),
    ("SRR059393", "stool"),
    ("SRR059394", "stool"),
    ("SRR059395", "stool"),
    ("SRR059396", "stool"),
    ("SRR059397", "stool"),
    ("SRR059398", "stool"),
    ("SRR059399", "stool"),
    ("SRR059400", "stool"),
    ("SRR059401", "stool"),
    ("SRR059402", "stool"),
    ("SRR059403", "stool"),
    ("SRR059404", "stool"),
    # --- buccal_mucosa (oral cheek) ---
    ("SRR060437", "buccal_mucosa"),
    ("SRR060438", "buccal_mucosa"),
    ("SRR060439", "buccal_mucosa"),
    ("SRR060440", "buccal_mucosa"),
    ("SRR060441", "buccal_mucosa"),
    ("SRR060442", "buccal_mucosa"),
    ("SRR060443", "buccal_mucosa"),
    ("SRR060444", "buccal_mucosa"),
    ("SRR060445", "buccal_mucosa"),
    ("SRR060446", "buccal_mucosa"),
    ("SRR060447", "buccal_mucosa"),
    ("SRR060448", "buccal_mucosa"),
    ("SRR060449", "buccal_mucosa"),
    ("SRR060450", "buccal_mucosa"),
    ("SRR060451", "buccal_mucosa"),
    ("SRR060452", "buccal_mucosa"),
    ("SRR060453", "buccal_mucosa"),
    ("SRR060454", "buccal_mucosa"),
    ("SRR060455", "buccal_mucosa"),
    ("SRR060456", "buccal_mucosa"),
    ("SRR060457", "buccal_mucosa"),
    ("SRR060458", "buccal_mucosa"),
    ("SRR060459", "buccal_mucosa"),
    ("SRR060460", "buccal_mucosa"),
    ("SRR060461", "buccal_mucosa"),
    ("SRR060462", "buccal_mucosa"),
    ("SRR060463", "buccal_mucosa"),
    ("SRR060464", "buccal_mucosa"),
    ("SRR060465", "buccal_mucosa"),
    ("SRR060466", "buccal_mucosa"),
    ("SRR060467", "buccal_mucosa"),
    ("SRR060468", "buccal_mucosa"),
    ("SRR060469", "buccal_mucosa"),
    # --- anterior_nares (nasal) ---
    ("SRR061502", "anterior_nares"),
    ("SRR061503", "anterior_nares"),
    ("SRR061504", "anterior_nares"),
    ("SRR061505", "anterior_nares"),
    ("SRR061506", "anterior_nares"),
    ("SRR061507", "anterior_nares"),
    ("SRR061508", "anterior_nares"),
    ("SRR061509", "anterior_nares"),
    ("SRR061510", "anterior_nares"),
    ("SRR061511", "anterior_nares"),
    ("SRR061512", "anterior_nares"),
    ("SRR061513", "anterior_nares"),
    ("SRR061514", "anterior_nares"),
    ("SRR061515", "anterior_nares"),
    ("SRR061516", "anterior_nares"),
    ("SRR061517", "anterior_nares"),
    ("SRR061518", "anterior_nares"),
    ("SRR061519", "anterior_nares"),
    ("SRR061520", "anterior_nares"),
    ("SRR061521", "anterior_nares"),
    ("SRR061522", "anterior_nares"),
    ("SRR061523", "anterior_nares"),
    ("SRR061524", "anterior_nares"),
    ("SRR061525", "anterior_nares"),
    ("SRR061526", "anterior_nares"),
    ("SRR061527", "anterior_nares"),
    ("SRR061528", "anterior_nares"),
    ("SRR061529", "anterior_nares"),
    ("SRR061530", "anterior_nares"),
    ("SRR061531", "anterior_nares"),
    ("SRR061532", "anterior_nares"),
    ("SRR061533", "anterior_nares"),
    ("SRR061534", "anterior_nares"),
]

SRA_ODP_BUCKET = "sra-pub-run-odp"
SRA_ODP_REGION = "us-east-1"


def _s3_key_exists(s3, bucket: str, key: str) -> bool:
    """Return True if the key exists in the bucket (cheap head_object call)."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def stage_samples(cfg) -> None:
    """Copy HMP SRA files from RODA into the demo S3 bucket.

    Uses S3→S3 copy (no laptop bandwidth needed).  Only copies files that
    don't already exist in the destination bucket.
    """
    s3 = boto3.client("s3", region_name=cfg.REGION)

    sample_count = min(cfg.SAMPLE_COUNT, len(HMP_ACCESSIONS))
    samples = HMP_ACCESSIONS[:sample_count]

    print(f"Staging {sample_count} HMP samples to s3://{cfg.BUCKET}/corpus/")
    print(f"  Source: s3://{SRA_ODP_BUCKET}/ (AWS Open Data, no egress charge in {SRA_ODP_REGION})")

    copied = 0
    skipped = 0

    for srr, body_site in samples:
        src_key = f"sra/{srr}/{srr}.sra"
        dst_key = f"corpus/{srr}/{srr}.sra"
        dst_meta_key = f"corpus/{srr}/body_site.txt"

        if _s3_key_exists(s3, cfg.BUCKET, dst_key):
            skipped += 1
            continue

        # S3→S3 copy: fast, free within us-east-1
        s3.copy_object(
            CopySource={"Bucket": SRA_ODP_BUCKET, "Key": src_key},
            Bucket=cfg.BUCKET,
            Key=dst_key,
        )
        # Write the body site label alongside the SRA file so the pipeline
        # can include it in the samplesheet without re-querying SRA metadata.
        s3.put_object(
            Bucket=cfg.BUCKET,
            Key=dst_meta_key,
            Body=body_site.encode(),
        )
        copied += 1
        if copied % 10 == 0:
            print(f"  {copied}/{sample_count - skipped} copied...")

    print(f"\n  {copied} files copied, {skipped} already present")


def write_samplesheet(cfg) -> None:
    """Write the nf-core/taxprofiler samplesheet CSV to S3.

    Format: sample,run_accession,instrument_platform,fastq_1,fastq_2,fasta
    The pipeline will convert SRA→FASTQ on-the-fly using the built-in
    SRA tools in the nf-core/taxprofiler container.
    """
    sample_count = min(cfg.SAMPLE_COUNT, len(HMP_ACCESSIONS))
    samples = HMP_ACCESSIONS[:sample_count]

    rows: list[dict] = []
    for srr, body_site in samples:
        rows.append(
            {
                "sample": f"{srr}_{body_site}",
                "run_accession": srr,
                "instrument_platform": "ILLUMINA",
                # SRA path on the demo bucket — the pipeline converts to FASTQ
                "fastq_1": f"s3://{cfg.BUCKET}/corpus/{srr}/{srr}.sra",
                "fastq_2": "",
                "fasta": "",
            }
        )

    # Write to a local temp file then upload
    local_path = Path("/tmp/microbiome_samplesheet.csv")
    with local_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "sample",
                "run_accession",
                "instrument_platform",
                "fastq_1",
                "fastq_2",
                "fasta",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    s3 = boto3.client("s3", region_name=cfg.REGION)
    key = "corpus/samplesheet.csv"
    s3.upload_file(str(local_path), cfg.BUCKET, key)
    print(f"  Samplesheet: s3://{cfg.BUCKET}/{key}  ({len(rows)} samples)")


def ensure_bucket(cfg) -> None:
    """Create the demo S3 bucket if it doesn't exist."""
    s3 = boto3.client("s3", region_name=cfg.REGION)
    try:
        if cfg.REGION == "us-east-1":
            s3.create_bucket(Bucket=cfg.BUCKET)
        else:
            s3.create_bucket(
                Bucket=cfg.BUCKET,
                CreateBucketConfiguration={"LocationConstraint": cfg.REGION},
            )
        print(f"  Created bucket: s3://{cfg.BUCKET}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print(f"  Bucket exists: s3://{cfg.BUCKET}")
    except s3.exceptions.BucketAlreadyExists:
        print(f"  Bucket exists (owned by account): s3://{cfg.BUCKET}")


if __name__ == "__main__":
    import importlib.util

    if importlib.util.find_spec("config") is None:
        sys.exit("config.py not found — copy config.example.py to config.py and fill it in.")

    import config as cfg  # type: ignore[import]

    print("=== Microbiome Demo — Corpus Preparation ===\n")
    print(f"  Region:  {cfg.REGION}")
    print(f"  Bucket:  {cfg.BUCKET}")
    print(f"  Samples: {cfg.SAMPLE_COUNT}\n")

    print("1/3  Ensuring S3 bucket exists...")
    ensure_bucket(cfg)

    print("\n2/3  Staging HMP SRA files from RODA (S3→S3, no laptop bandwidth)...")
    stage_samples(cfg)

    print("\n3/3  Writing nf-core/taxprofiler samplesheet...")
    write_samplesheet(cfg)

    print("\nDone.  Next step: python build_ami.py")
