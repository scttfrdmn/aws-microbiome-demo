#!/usr/bin/env python3
"""
build_fsx_db.py — stage the reference DBs onto FSx FROM THEIR CANONICAL SOURCES,
timing the staging as a measured one-time cost.

The benchmark measures data copy/staging. So the DBs are NOT laundered through old
EBS snapshots — they are fetched from their authoritative upstreams onto a staging
instance, into s3://BUCKET/dbs-fsx/{kraken2,metaphlan}/, which an S3-backed FSx for
Lustre then imports. Every classifier task mounts that one shared FS read-only (no
per-volume FSR credit limit — the reason EBS-snapshot volumes don't scale past ~10
concurrent readers).

Canonical sources (provenance, not a copy of a copy):
  - Kraken2 k2_pluspf_16GB : s3://genome-idx/kraken/k2_pluspf_16_GB_20260226.tar.gz
                             (the public genome-idx bucket) → download + extract.
  - MetaPhlAn vJan25       : `metaphlan --install --index <ver> --bowtie2db` pulls
                             the CHOCOPhlAnSGB index from the official MetaPhlAn host.

Each phase is TIMED (download_s, extract_s, install_s, sync_s, bytes) and the
timings written to s3://BUCKET/dbs-fsx/staging_timings.json — the measured
"DB staging from source" cost, reported alongside the per-run numbers.

This is a ONE-TIME, amortized setup. The per-run/per-task path stays zero-copy:
tasks read the DB in place off /fsx (the shared Lustre mount).

  Stage 1 (this script's user-data): a c7g staging instance fetches both DBs from
    source, times each phase, syncs to s3://BUCKET/dbs-fsx/, writes timings, exits.
  Stage 2 (manual): spawn launch ... --fsx-create --fsx-s3-bucket BUCKET
    --fsx-import-path s3://BUCKET/dbs-fsx ... → capture fs-id → set FSX_ID.

⚠ GATED: --plan prints the plan and the staging user-data; it takes NO action.
  Running the staging incurs real EC2 + S3 spend; launch the printed command
  yourself so the cost is explicitly approved.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys

if importlib.util.find_spec("config") is None:
    sys.exit("config.py not found")
import config as cfg  # type: ignore[import]

S3_PREFIX = f"s3://{cfg.BUCKET}/dbs-fsx"
KRAKEN2_SRC = "s3://genome-idx/kraken/k2_pluspf_16_GB_20260226.tar.gz"
METAPHLAN_INDEX = "mpa_vJan25_CHOCOPhlAnSGB_202503"
METAPHLAN_IMAGE = "quay.io/aarchbio/metaphlan:4.1.1--pyhdfd78af_0"

# Staging user-data: fetch each DB from its canonical source, time every phase,
# sync to S3, emit timings. Placeholders substituted in build_userdata().
_STAGING_USERDATA = r"""#!/bin/bash
set -euxo pipefail
exec > /var/log/fsx-stage.log 2>&1
echo "=== FSx DB staging from canonical sources: $(date) ==="
REGION="@@REGION@@"
S3_PREFIX="@@S3_PREFIX@@"
KRAKEN2_SRC="@@KRAKEN2_SRC@@"
MPA_INDEX="@@MPA_INDEX@@"
MPA_IMAGE="@@MPA_IMAGE@@"
WORK=/mnt/stage
mkdir -p "$WORK/kraken2" "$WORK/metaphlan"
# Stock AL2023 has no Docker; metaphlan --install runs via the container. Install
# + start Docker before the MetaPhlAn phase (Kraken2 phase is pure aws-cli + tar).
dnf install -y docker >/dev/null 2>&1
systemctl enable --now docker
docker --version
T() { date +%s.%N; }
JQADD() { python3 -c "import json,sys; d=json.load(open('/tmp/timings.json')) if __import__('os').path.exists('/tmp/timings.json') else {}; d[sys.argv[1]]=float(sys.argv[2]); json.dump(d,open('/tmp/timings.json','w'))" "$1" "$2"; }

# ── Kraken2: download tarball from genome-idx (public) + extract ─────────────
t0=$(T)
aws s3 cp "$KRAKEN2_SRC" "$WORK/k2.tar.gz" --no-sign-request --region "$REGION" --no-progress
t1=$(T); JQADD kraken2_download_s "$(awk -v a=$t0 -v b=$t1 'BEGIN{print b-a}')"
K2_BYTES=$(stat -c%s "$WORK/k2.tar.gz"); JQADD kraken2_tarball_bytes "$K2_BYTES"
tar -xzf "$WORK/k2.tar.gz" -C "$WORK/kraken2"
t2=$(T); JQADD kraken2_extract_s "$(awk -v a=$t1 -v b=$t2 'BEGIN{print b-a}')"
# Flatten if the tarball extracted into a subdir (hash.k2d must be at kraken2/ root)
if [ ! -f "$WORK/kraken2/hash.k2d" ]; then
    sub=$(find "$WORK/kraken2" -name hash.k2d -printf '%h\n' | head -1)
    [ -n "$sub" ] && mv "$sub"/* "$WORK/kraken2/"
fi
rm -f "$WORK/k2.tar.gz"
t3=$(T)
aws s3 sync "$WORK/kraken2/" "$S3_PREFIX/kraken2/" --region "$REGION" --no-progress --delete
t4=$(T); JQADD kraken2_sync_s "$(awk -v a=$t3 -v b=$t4 'BEGIN{print b-a}')"

# ── MetaPhlAn: `metaphlan --install` from the official host (via the image) ──
# --user root so the container can write the bind-mounted /db (matches the
# pipeline's docker.runOptions='--user root'); host dir also made writable.
chmod 0777 "$WORK/metaphlan"
t5=$(T)
docker run --rm --user root -v "$WORK/metaphlan:/db" "$MPA_IMAGE" \
    metaphlan --install --index "$MPA_INDEX" --bowtie2db /db
t6=$(T); JQADD metaphlan_install_s "$(awk -v a=$t5 -v b=$t6 'BEGIN{print b-a}')"
t7=$(T)
aws s3 sync "$WORK/metaphlan/" "$S3_PREFIX/metaphlan/" --region "$REGION" --no-progress --delete
t8=$(T); JQADD metaphlan_sync_s "$(awk -v a=$t7 -v b=$t8 'BEGIN{print b-a}')"

# ── provenance + emit timings ────────────────────────────────────────────────
python3 -c "import json; d=json.load(open('/tmp/timings.json')); d['kraken2_source']='$KRAKEN2_SRC'; d['metaphlan_source']='metaphlan --install $MPA_INDEX'; d['staged_at']='$(date -u +%Y-%m-%dT%H:%M:%SZ)'; json.dump(d,open('/tmp/timings.json','w'),indent=2)"
aws s3 cp /tmp/timings.json "$S3_PREFIX/staging_timings.json" --region "$REGION" --no-progress
echo "--- staged file inventory ---"
aws s3 ls "$S3_PREFIX/kraken2/" --region "$REGION" | wc -l
aws s3 ls "$S3_PREFIX/metaphlan/" --region "$REGION"
cat /tmp/timings.json
touch /tmp/SPAWN_COMPLETE
echo "=== staging complete: $(date) ==="
"""


def build_userdata() -> str:
    return (_STAGING_USERDATA
            .replace("@@REGION@@", cfg.REGION)
            .replace("@@S3_PREFIX@@", S3_PREFIX)
            .replace("@@KRAKEN2_SRC@@", KRAKEN2_SRC)
            .replace("@@MPA_INDEX@@", METAPHLAN_INDEX)
            .replace("@@MPA_IMAGE@@", METAPHLAN_IMAGE))


def plan() -> None:
    import tempfile
    ud = build_userdata()
    path = tempfile.mktemp(suffix="-fsx-stage.sh")
    with open(path, "w") as f:
        f.write(ud)
    print("=== build_fsx_db.py — PLAN (no actions taken) ===\n")
    print(f"Region/Bucket: {cfg.REGION} / {cfg.BUCKET}")
    print(f"FSx S3 import target: {S3_PREFIX}/{{kraken2,metaphlan}}/\n")
    print("DB staging FROM CANONICAL SOURCES (timed):")
    print(f"  kraken2   <- {KRAKEN2_SRC}  (download + extract)")
    print(f"  metaphlan <- metaphlan --install {METAPHLAN_INDEX}  (official host, via {METAPHLAN_IMAGE})")
    print(f"\nStaging user-data written to: {path}")
    print("\nStage 1 — launch the staging instance (needs Docker for metaphlan --install):")
    print(f"  spawn launch fsx-stage --instance-type c7g.2xlarge --region {cfg.REGION} \\")
    print(f"      --az us-east-1a --volume-size 200 \\")
    print(f"      --user-data-file {path} --ttl 3h --wait-for-ssh -y")
    print("  (c7g.2xlarge + 200GB root: room to extract the 12GB Kraken2 tarball +")
    print("   metaphlan --install's ~20GB index before syncing to S3.)")
    print(f"\nStage 2 — after staging completes, create the FSx FS:")
    print(f"  spawn launch fsx-host --instance-type c7g.large --region {cfg.REGION} --az us-east-1a \\")
    print(f"      --fsx-create --fsx-lifecycle durable --fsx-ttl 1d \\")
    print(f"      --fsx-s3-bucket {cfg.BUCKET} --fsx-import-path {S3_PREFIX} \\")
    print(f"      --fsx-storage-capacity 1200 --fsx-throughput 250 --fsx-mount-point /fsx ...")
    print("  Then scope the DRA to /fsx <- dbs-fsx, set FSX_ID = '<fs-id>'.")
    print(f"\nMeasured staging cost lands at: {S3_PREFIX}/staging_timings.json")
    print("\n⚠ Running these incurs real EC2 + S3 + FSx spend. Launch them yourself.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="print the staging plan + user-data (default)")
    ap.add_argument("--print-userdata", action="store_true", help="print only the staging user-data script")
    args = ap.parse_args()
    if args.print_userdata:
        print(build_userdata())
    else:
        plan()
