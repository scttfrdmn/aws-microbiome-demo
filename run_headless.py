#!/usr/bin/env python3
"""
run_headless.py  --  run the full microbiome pipeline without the web UI.

Useful for debugging the pipeline end-to-end before polishing the dashboard.
Prints every event to stdout so you can see exactly what's happening.

Usage:
    AWS_PROFILE=aws uv run python run_headless.py

Optional env vars:
    SAMPLE_COUNT=5   override config.py for a quick test run
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

if importlib.util.find_spec("config") is None:
    sys.exit("config.py not found — copy config.example.py to config.py and fill it in.")

import config as cfg  # type: ignore[import]

# Allow quick override for testing
sample_count_override = os.environ.get("SAMPLE_COUNT")
if sample_count_override:
    cfg.SAMPLE_COUNT = int(sample_count_override)


def emit(event: dict) -> None:
    ts = time.strftime("%H:%M:%S")
    t = event.get("type", "?")

    if t == "phase":
        print(f"  [{ts}] PHASE    {event['label']}")
    elif t == "quota":
        print(f"  [{ts}] QUOTA    {event['summary']}")
    elif t == "head_launched":
        print(f"  [{ts}] LAUNCHED head={event['instance_id']}")
    elif t == "progress":
        done  = event.get("tasks_done", 0)
        total = event.get("tasks_total", 0)
        run   = event.get("tasks_running", 0)
        cost  = event.get("ec2_cost_usd", 0)
        roda  = event.get("roda_gb", 0)
        print(f"  [{ts}] PROGRESS {done}/{total} tasks · {run} running · "
              f"${cost:.4f} · RODA {roda:.2f} GB")
    elif t == "model":
        state = event.get("state", "")
        if state == "start":
            print(f"  [{ts}] MODEL    {event['label']} started")
        elif state == "done":
            cost = event.get("cost", 0)
            print(f"  [{ts}] MODEL    {event['label']} done · ${cost:.6f}")
    elif t == "insight":
        print(f"\n  [{ts}] INSIGHT\n")
        for line in event["text"].splitlines():
            print(f"    {line}")
        print()
    elif t == "cost":
        print(f"  [{ts}] COST     total=${event['total']:.6f}")
    elif t == "done":
        print(f"\n  [{ts}] ✓ DONE\n")
    elif t == "error":
        print(f"\n  [{ts}] ✗ ERROR  {event['message']}\n", file=sys.stderr)
    else:
        print(f"  [{ts}] {t.upper():8} {json.dumps(event)[:120]}")

    sys.stdout.flush()


def main() -> None:
    from src.microbiome_demo import agent, nextflow_config, pipeline, spawn, truffle, worker_script
    from src.microbiome_demo.accessions import HMP_ACCESSIONS

    print("\n=== Microbiome Demo — Headless Run ===")
    print(f"  Region:       {cfg.REGION}")
    print(f"  Bucket:       {cfg.BUCKET}")
    print(f"  AMI:          {cfg.AMI_ID}")
    print(f"  Sample count: {cfg.SAMPLE_COUNT}")
    print()

    # 0. Validate config
    ami = getattr(cfg, "AMI_ID", "")
    if not ami:
        sys.exit("ERROR: AMI_ID is not set in config.py — run `make ami` first.")

    # 1. Quota
    emit({"type": "phase", "label": "Querying vCPU quotas via truffle…"})
    families = list({t.split(".")[0] for t in nextflow_config.ALL_INSTANCE_TYPES})
    quotas = truffle.query_quotas(cfg.REGION, families)
    queue_size = truffle.derive_queue_size(quotas, nextflow_config.ALL_INSTANCE_TYPES)
    emit({"type": "quota", "queue_size": queue_size,
          "summary": truffle.quota_summary(quotas, queue_size)})

    # 2. Ensure bucket
    emit({"type": "phase", "label": f"Ensuring S3 bucket s3://{cfg.BUCKET}…"})
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=cfg.REGION)
    try:
        if cfg.REGION == "us-east-1":
            s3.create_bucket(Bucket=cfg.BUCKET)
        else:
            s3.create_bucket(Bucket=cfg.BUCKET,
                             CreateBucketConfiguration={"LocationConstraint": cfg.REGION})
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise

    # 3. Upload configs
    emit({"type": "phase", "label": "Uploading SRR list and Nextflow config…"})
    sample_count = min(cfg.SAMPLE_COUNT, len(HMP_ACCESSIONS))
    accessions = HMP_ACCESSIONS[:sample_count]
    srr_key = worker_script.write_srr_slice(cfg, accessions)
    nf_cfg_str = nextflow_config.render(cfg, queue_size)
    nf_cfg_key = worker_script.upload_nextflow_config(cfg, nf_cfg_str)
    emit({"type": "phase", "label": f"Config ready — queueSize={queue_size}, {sample_count} samples"})  # noqa: E501

    # 4. Launch head
    emit({"type": "phase", "label": "Launching Nextflow head instance (t4g.small)…"})
    import types
    head_cfg = types.SimpleNamespace(**{
        k: getattr(cfg, k) for k in dir(cfg) if not k.startswith("_")
    })
    head_cfg.INSTANCE_TYPE = getattr(cfg, "HEAD_INSTANCE_TYPE", "t4g.small")
    head_cfg.INSTANCE_COUNT = 1

    head_script = worker_script.render(cfg, nf_cfg_key, srr_key)
    head_script_path = worker_script.write_temp(head_script)
    wg = spawn.launch_workers(head_cfg, head_script_path, emit=emit)
    head_id = wg.instance_ids[0] if wg.instance_ids else None
    start_time = time.time()
    emit({"type": "head_launched", "instance_id": head_id})

    # 5. Poll
    print(f"\n  Polling every 15s (head={head_id})...\n")
    max_polls = (90 * 60) // 15
    for poll in range(max_polls):
        time.sleep(15)

        if head_id:
            statuses = spawn.poll_workers([head_id])
            head_status = statuses.get(head_id, "running")
            if head_status == "failed":
                emit({"type": "error", "message": "Head instance failed."})
                sys.exit(1)
        else:
            head_status = "running"

        prog = pipeline.poll_progress(cfg, start_time, queue_size)
        emit({
            "type": "progress",
            "tasks_done":      prog.tasks_done,
            "tasks_total":     prog.tasks_total,
            "tasks_running":   prog.tasks_running,
            "ec2_cost_usd":    prog.ec2_cost_usd,
            "elapsed_seconds": prog.elapsed_seconds,
            "roda_gb":         prog.data.roda_gb,
            "fastq_gb":        prog.data.fastq_gb,
        })

        if head_status == "complete":
            emit({"type": "phase", "label": "Nextflow complete. Building summary…"})
            break

        if (poll + 1) % 4 == 0:
            emit({"type": "phase", "label":
                  f"{prog.tasks_done}/{prog.tasks_total} tasks · "
                  f"{prog.elapsed_seconds/60:.1f} min elapsed"})
    else:
        emit({"type": "error", "message": "Timed out after 90 minutes."})
        sys.exit(1)

    # 6. Synthesis
    summary = pipeline.read_summary(cfg)
    if summary:
        agent.synthesize(summary, emit)
    else:
        emit({"type": "error", "message": "No summary.json found in S3."})
        sys.exit(1)


if __name__ == "__main__":
    main()
