#!/usr/bin/env bash
# tag_db_snapshot.sh — apply the demo's standard provenance tags to a reference-DB
# EBS snapshot built with `spawn snapshot create`.
#
# spawn tags its snapshots with Name/spawn:snapshot-name/spawn:managed/spawn:source
# but has no --tag flag for provenance (see nf-spawn/spawn FR). Until it does, run
# this after building a DB snapshot so months later it's obvious what the blob is:
# which project, tool, DB version, where it came from, and where it mounts.
#
# Usage:
#   scripts/tag_db_snapshot.sh <snap-id> <tool> <db> <db-version> <source> <mount> [region]
# Example (Kraken2):
#   scripts/tag_db_snapshot.sh snap-05068c70e7ccf7974 kraken2 k2_pluspf_16GB \
#     k2_pluspf_16_GB_20260226 s3://genome-idx/kraken/k2_pluspf_16_GB_20260226.tar.gz \
#     /opt/databases/kraken2
# Example (MetaPhlAn):
#   scripts/tag_db_snapshot.sh snap-XXXX metaphlan CHOCOPhlAnSGB vJan25_CHOCOPhlAnSGB_202503 \
#     "metaphlan --install (cmprod1.cibio.unitn.it)" /opt/databases/metaphlan
set -euo pipefail

SNAP="${1:?snapshot id}"
TOOL="${2:?tool (kraken2|metaphlan|...)}"
DB="${3:?db short name}"
DBVER="${4:?db version}"
SOURCE="${5:?source URI/description}"
MOUNT="${6:?mount path}"
REGION="${7:-us-east-1}"
DATE="$(date +%Y-%m-%d)"

aws ec2 create-tags --region "$REGION" --resources "$SNAP" --tags \
  "Key=project,Value=aws-microbiome-demo" \
  "Key=role,Value=reference-db" \
  "Key=tool,Value=${TOOL}" \
  "Key=db,Value=${DB}" \
  "Key=db-version,Value=${DBVER}" \
  "Key=source,Value=${SOURCE}" \
  "Key=mount,Value=${MOUNT}" \
  "Key=built-by,Value=spawn-snapshot-create" \
  "Key=built-date,Value=${DATE}"

echo "Tagged ${SNAP}:"
aws ec2 describe-snapshots --snapshot-ids "$SNAP" --region "$REGION" \
  --query 'Snapshots[].Tags[].[Key,Value]' --output text | sort
