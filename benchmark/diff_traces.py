#!/usr/bin/env python3
"""
diff_traces.py  --  x86-vs-arm64 per-stage benchmark from two Nextflow traces.

This is the analysis half of the demo Graviton benchmark (the aarchbio
"Graviton leg", run against this demo's nf-core/taxprofiler pipeline). It does
NOT launch anything — it reads two trace.tsv files captured from two N=5 runs
over the SAME 5 HMP accessions:

    x86.tsv    all-x86 leg  (c7i/r7i, native amd64 containers)
    arm64.tsv  all-Graviton leg (c7g/r7g, aarchbio native arm64 containers)

Both legs are NATIVE — neither emulates — so this measures native price/perf,
the honest number (per aarchbio benchmark/METHODOLOGY.md).

Honest-benchmark rules carried over from METHODOLOGY.md:
  - report MEDIAN + min–max, never a mean or a single number;
  - report PER-STAGE (Kraken2 is the memory-bound swing factor), not just total;
  - cost = measured task duration (hours) × on-demand $/hr for that stage's
    instance — real prices only;
  - negative results stay in (a stage slower on arm64 is reported as such);
  - N=5 is a PILOT — stated loudly, not buried.

Usage:
    python benchmark/diff_traces.py x86.tsv arm64.tsv [--json out.json]

The Nextflow trace columns we rely on (set in nextflow_config.py trace.fields):
    task_id name status exit start complete duration realtime cpus memory
    rss vmem rchar wchar
We use `realtime` (pure task execution, ms) as the primary timing signal and
`duration` (incl. scheduling) as a secondary line, matching METHODOLOGY's
"wall-clock per run" with scheduling noise called out separately.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

# us-east-1 on-demand Linux $/hr. The x86↔arm64 pairs are identical vCPU/RAM
# spec, differing ONLY in instance family — the controlled variable.
PRICE_USD_PER_HR: dict[str, float] = {
    "c7i.large": 0.089250,
    "c7i.2xlarge": 0.357000,
    "r7i.2xlarge": 0.529200,
    "r7i.4xlarge": 1.058400,
    "c7g.large": 0.072500,
    "c7g.2xlarge": 0.290000,
    "r7g.2xlarge": 0.428400,
    "r7g.4xlarge": 0.856800,
}

# EBS pricing (us-east-1): gp3 volume storage, and snapshot storage.
EBS_GP3_USD_PER_GB_MO = 0.08
EBS_SNAPSHOT_USD_PER_GB_MO = 0.05
HOURS_PER_MONTH = 730.0

# Reference-DB EBS volumes (the volume-backed-DB architecture). Each is a
# snapshot (standing storage) that every consuming task attaches as a gp3 volume
# for the task's duration. Sizes = the `--size` the snapshots were built at.
DB_VOLUMES: dict[str, dict] = {
    # stage label that consumes it : {snapshot GiB, which stage attaches it}
    "Kraken2":   {"gib": 24, "snapshot": "kraken2-k2pluspf-16gb"},
    "MetaPhlAn": {"gib": 40, "snapshot": "metaphlan-vJan25"},
}

# Map a taxprofiler process name (trace `name`, e.g. "FASTP_PAIRED (SRR059371)")
# to (stage_label, x86_instance, arm64_instance). Per the protocol's fairness
# controls, EVERY measured stage runs on a matched non-burstable family pair
# differing only in arch — NO t4g anywhere (burstable credit state would corrupt
# timing). process_single stages are c7i.large↔c7g.large (NOT a constant): they
# include MultiQC/krakentools/standard-report, which are measured. FETCH_FASTQ is
# also process_single; its container is ncbi/sra-tools (multi-arch, not aarchbio)
# — reported, but flagged as not part of the aarchbio-unblocks-it claim.
# MetaPhlAn + Kraken2 are MEMORY-optimized (r-family): Kraken2 holds k2_pluspf in
# RAM; MetaPhlAn's bowtie2 against the full CHOCOPhlAnSGB index OOMs below ~64 GB
# (it runs on r7g.4xlarge / 128 GB — see nextflow_config METAPHLAN_INSTANCE).
STAGE_MAP: list[tuple[str, str, str, str]] = [
    # match-substring (upper),    stage label,        x86,           arm64
    ("FETCH_FASTQ",               "FETCH_FASTQ",       "c7i.large",   "c7g.large"),
    ("FASTP",                     "fastp",             "c7i.2xlarge", "c7g.2xlarge"),
    ("FASTQC",                    "FastQC",            "c7i.2xlarge", "c7g.2xlarge"),
    ("METAPHLAN_METAPHLAN",       "MetaPhlAn",         "r7i.4xlarge", "r7g.4xlarge"),
    ("KRAKEN2_KRAKEN2",           "Kraken2",           "r7i.2xlarge", "r7g.2xlarge"),
    ("KRAKEN2STANDARDREPORT",     "Kraken2StdReport",  "c7i.large",   "c7g.large"),
    ("COMBINEKREPORTS",           "krakentools",       "c7i.large",   "c7g.large"),
    ("MULTIQC",                   "MultiQC",           "c7i.large",   "c7g.large"),
]


def _classify(name: str) -> tuple[str, str, str] | None:
    """Return (stage_label, x86_inst, arm64_inst) for a trace task name."""
    up = name.upper()
    for needle, label, x86, arm in STAGE_MAP:
        if needle in up:
            return label, x86, arm
    return None


def _parse_realtime_ms(val: str) -> float | None:
    """Parse a Nextflow trace duration field to milliseconds.

    Nextflow may emit either a raw integer (ms, our trace config) or a
    human string like '3m 5s' / '45.2s' / '1h 2m'. Handle both.
    """
    if val is None:
        return None
    val = val.strip()
    if val in ("", "-", "N/A"):
        return None
    # Raw integer milliseconds (our trace.fields emits numeric).
    try:
        return float(val)
    except ValueError:
        pass
    # Human-readable fallback: sum h/m/s tokens.
    total = 0.0
    num = ""
    for ch in val:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch == "h":
            total += float(num) * 3600_000
            num = ""
        elif ch == "m" and num:
            total += float(num) * 60_000
            num = ""
        elif ch == "s":
            total += float(num) * 1000
            num = ""
        else:
            num = ""  # skip spaces/unknown
    return total or None


def _parse_bytes(val: str) -> float | None:
    """Parse a Nextflow trace memory field to bytes (int, or '1.2 GB'/'512 MB')."""
    if val is None:
        return None
    val = val.strip()
    if val in ("", "-", "N/A"):
        return None
    try:
        return float(val)  # raw bytes
    except ValueError:
        pass
    units = {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
             "B": 1, "K": 1e3, "M": 1e6, "G": 1e9}
    parts = val.split()
    if len(parts) == 2:
        try:
            return float(parts[0]) * units.get(parts[1].upper(), 1)
        except ValueError:
            return None
    return None


def load_trace(path: str) -> list[dict]:
    with open(path) as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        if len(cells) != len(header):
            continue
        row = dict(zip(header, cells, strict=False))
        if row.get("status") != "COMPLETED":
            continue  # only successful tasks count toward timing
        rows.append(row)
    return rows


def stage_stats(rows: list[dict], leg: str) -> dict[str, dict]:
    """Group COMPLETED tasks by stage; return per-stage timing/cost stats."""
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        c = _classify(r.get("name", ""))
        if not c:
            continue
        label, x86, arm = c
        buckets.setdefault(label, []).append(r)

    out: dict[str, dict] = {}
    for label, tasks in buckets.items():
        c = _classify(tasks[0]["name"])
        assert c
        _, x86, arm = c
        inst = x86 if leg == "x86" else arm
        price = PRICE_USD_PER_HR.get(inst, 0.0)

        realtimes = [v for v in (_parse_realtime_ms(t.get("realtime", "")) for t in tasks) if v]
        durations = [v for v in (_parse_realtime_ms(t.get("duration", "")) for t in tasks) if v]
        if not realtimes:
            continue

        sec = [r / 1000 for r in realtimes]
        # Cost: sum the per-task durations (wall-clock the instance was alive)
        # × that stage's $/hr. Duration (not realtime) because billing tracks
        # instance lifetime, not pure CPU; fall back to realtime if missing.
        bill_ms = durations if durations else realtimes
        cost = sum(bill_ms) / 1000 / 3600 * price

        # Peak RSS (bytes) — memory behavior can differ by arch (esp. Kraken2).
        rss_vals = [_parse_bytes(t.get("rss", "")) for t in tasks]
        rss_vals = [v for v in rss_vals if v is not None]
        # Exit-code success: a "faster" run that silently failed a step isn't faster.
        exits = [t.get("exit", "") for t in tasks]
        nonzero = [e for e in exits if e not in ("0", "", "-")]

        out[label] = {
            "instance": inst,
            "price_usd_per_hr": price,
            "n_tasks": len(tasks),
            "realtime_sec_median": round(statistics.median(sec), 2),
            "realtime_sec_min": round(min(sec), 2),
            "realtime_sec_max": round(max(sec), 2),
            "peak_rss_mb": round(max(rss_vals) / 1e6, 1) if rss_vals else None,
            "nonzero_exits": len(nonzero),
            "cost_usd": round(cost, 4),
        }
    return out


def load_staging_timings(path: str) -> list[dict]:
    """Load per-sample timings.json files (one dir of FETCH_FASTQ data-movement
    records emitted by the pipeline to results/<job>/staging/).

    Each record carries environment provenance (instance_type, vcpus, net_driver,
    az, lifecycle, arch) plus the data-movement phases: roda_download_s/_bytes/
    _mbps (the source-staging cost), fasterq_dump_s, pigz_s, fastq_gz_bytes.
    Accepts a directory of *.timings.json or a single concatenated JSON-lines file.
    """
    import glob
    records = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.timings.json")))
        for f in files:
            try:
                with open(f) as fh:
                    records.append(json.load(fh))
            except Exception:  # noqa: BLE001
                continue
    elif os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue
    return records


def _med(vals: list[float]) -> float | None:
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(statistics.median(vals), 2) if vals else None


def report_staging(records: list[dict], leg: str) -> None:
    """Print the data-movement (source staging + convert + compress) summary for
    one leg, with environment provenance so the times are interpretable."""
    if not records:
        return
    envs = sorted({r.get("instance_type", "?") for r in records})
    drivers = sorted({r.get("net_driver", "?") for r in records})
    azs = sorted({r.get("az", "?") for r in records})
    lifecycles = sorted({r.get("lifecycle", "?") for r in records})
    arches = sorted({r.get("arch", "?") for r in records})
    print(f"\n  ── Data movement — {leg} leg (N={len(records)}) ──")
    print(f"     env: instance={','.join(envs)} arch={','.join(arches)} "
          f"net={','.join(drivers)} az={','.join(azs)} lifecycle={','.join(lifecycles)}")
    dl_s = _med([r.get('roda_download_s') for r in records])
    dl_mbps = _med([r.get('roda_mbps') for r in records])
    dl_gb = _med([r.get('roda_bytes', 0)/1e9 for r in records])
    fq_s = _med([r.get('fasterq_dump_s') for r in records])
    gz_s = _med([r.get('pigz_s') for r in records])
    gz_gb = _med([r.get('fastq_gz_bytes', 0)/1e9 for r in records])
    print(f"     RODA download (source pull): median {dl_s}s, {dl_mbps} MB/s, {dl_gb} GB median")
    print(f"     fasterq-dump (SRA→FASTQ):    median {fq_s}s")
    print(f"     pigz (compress):             median {gz_s}s, {gz_gb} GB out median")
    print("     (times are instance/network-dependent — qualified by the env line above)")


def db_delivery_comparison(stages: dict, leg: str, dl_mbps: float | None) -> dict:
    """Compare the THREE ways to get a reference DB onto each task, focused on
    COPYING — how long it takes and what that time costs.

    The core trade the user wants surfaced: on the per-task-download approach the
    worker sits *running* (billed) while it copies the DB — that wait is wasted
    compute-$ on the task instance. The volume approach skips the copy (symlink +
    bind-mount) and instead pays EBS-$ for the attached volume. Baked-AMI skips
    both but inflates the AMI + every task root.

    For each consuming stage (Kraken2 r7g.2xlarge, MetaPhlAn r7g.4xlarge) we
    report, PER RUN (× n_tasks) and on the leg's instance $/hr:

      A. baked-AMI         copy_s=0; compute_$=0; +EBS: DB GiB on each task root
                           for task duration + standing AMI snapshot.
      B. per-task download copy_s = DB_GiB / throughput; compute_$ = copy_s ×
                           instance_$/hr × n_tasks (THE WASTED COMPUTE); EBS ~0.
      C. zero-copy volume  copy_s ≈ mount (~seconds, ~0 compute_$); +EBS: DB GiB ×
                           gp3 × task-hours (volume attached) + standing snapshot.

    `dl_mbps` is the measured S3→instance throughput (from staging timings RODA
    MB/s, a same-region proxy) used to estimate the download time in (B). If None,
    download time/$ is left null (can't estimate without a throughput).
    Returns a dict of per-stage, per-approach line items.
    """
    gp3_per_gb_hr = EBS_GP3_USD_PER_GB_MO / HOURS_PER_MONTH
    out: dict = {"throughput_mbps_used": dl_mbps, "per_stage": {}}
    standing_snapshot_mo = 0.0
    run_download_compute = 0.0
    run_volume_ebs = 0.0
    for label, spec in DB_VOLUMES.items():
        st = stages.get(label)
        if not st:
            continue
        gib = spec["gib"]
        n = st["n_tasks"]
        inst = st["instance"]
        price_hr = st["price_usd_per_hr"]
        task_h_total = (st["realtime_sec_median"] * n) / 3600.0  # all consuming tasks

        # (B) per-task download: time + the compute-$ of waiting on a billed box
        dl_s = (gib * 1024 / dl_mbps) if dl_mbps else None  # GiB→MiB / (MiB/s)
        dl_compute = ((dl_s * n) / 3600.0 * price_hr) if dl_s else None

        # (C) zero-copy volume: EBS gp3 for the attach window (per task duration)
        vol_ebs = gib * gp3_per_gb_hr * task_h_total
        # standing snapshot (shared, $/mo) — counted once below, not per run

        # (A) baked-AMI: DB GiB ride each task's ROOT for the task window (≈ same
        # gp3 as the volume), plus a bigger standing AMI snapshot.
        baked_root = gib * gp3_per_gb_hr * task_h_total

        standing_snapshot_mo += gib * EBS_SNAPSHOT_USD_PER_GB_MO
        if dl_compute:
            run_download_compute += dl_compute
        run_volume_ebs += vol_ebs

        out["per_stage"][label] = {
            "instance": inst, "n_tasks": n, "db_gib": gib,
            "A_baked_ami":      {"copy_s": 0.0, "compute_usd": 0.0,
                                 "per_run_root_ebs_usd": round(baked_root, 6)},
            "B_per_task_dl":    {
                "copy_s_each": round(dl_s, 1) if dl_s else None,
                "copy_s_total": round(dl_s * n, 1) if dl_s else None,
                "wasted_compute_usd": round(dl_compute, 6) if dl_compute else None},
            "C_zero_copy_vol":  {"copy_s": "~mount (seconds)",
                                 "compute_usd": 0.0,
                                 "per_run_volume_ebs_usd": round(vol_ebs, 6)},
        }
    out["per_run_totals"] = {
        "download_wasted_compute_usd": round(run_download_compute, 6) if dl_mbps else None,
        "zero_copy_volume_ebs_usd": round(run_volume_ebs, 6),
        "standing_snapshot_usd_per_mo": round(standing_snapshot_mo, 4),
    }
    out["reading"] = (
        "Per-task-download (B) burns compute_$ while the billed worker waits on the "
        "copy; zero-copy (C) replaces that with a few $ of EBS gp3 for the attach "
        "window. Bigger DB or pricier instance → (B)'s wasted compute grows, (C)'s "
        "EBS barely moves. Snapshot storage is standing (amortized across runs).")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("x86_trace", help="trace.tsv from the all-x86 (c7i/r7i) N=5 run")
    ap.add_argument("arm64_trace", help="trace.tsv from the all-Graviton (c7g/r7g) N=5 run")
    ap.add_argument("--json", help="also write the full comparison as JSON to this path")
    ap.add_argument("--n", type=int, default=5, help="samples per leg (for the pilot note)")
    ap.add_argument("--x86-staging", help="dir of x86 leg *.timings.json (data-movement records)")
    ap.add_argument("--arm64-staging",
                    help="dir of arm64 leg *.timings.json (data-movement records)")
    args = ap.parse_args()

    x86_rows = load_trace(args.x86_trace)
    arm_rows = load_trace(args.arm64_trace)
    if not x86_rows or not arm_rows:
        sys.exit("ERROR: one or both traces had no COMPLETED tasks — check the files.")

    x86 = stage_stats(x86_rows, "x86")
    arm = stage_stats(arm_rows, "arm64")
    stages = [lbl for _, lbl, _, _ in STAGE_MAP if lbl in x86 or lbl in arm]

    print("\n=== Demo Graviton benchmark — x86 vs arm64, per stage ===")
    print(f"    N={args.n} samples/leg (PILOT — not a population estimate).")
    print("    Both legs NATIVE (no QEMU): x86=native amd64, arm64=aarchbio native arm64.")
    print("    Timing = task `realtime` median [min–max], seconds. Cost = Σ duration×$/hr.\n")

    hdr = (f"{'stage':17} {'x86 med[min-max]s':22} {'arm64 med[min-max]s':22} "
           f"{'speedup':>8} {'x86$':>8} {'arm64$':>8} {'save':>7}")
    print(hdr)
    print("-" * len(hdr))

    tot_x = tot_a = 0.0
    comparison: dict[str, dict] = {}
    for s in stages:
        xs, as_ = x86.get(s), arm.get(s)
        if not xs and not as_:
            continue
        xm = xs["realtime_sec_median"] if xs else None
        am = as_["realtime_sec_median"] if as_ else None
        xcost = xs["cost_usd"] if xs else 0.0
        acost = as_["cost_usd"] if as_ else 0.0
        tot_x += xcost
        tot_a += acost
        speed = (xm / am) if (xm and am) else None
        xstr = (f"{xm:.1f}[{xs['realtime_sec_min']:.0f}-{xs['realtime_sec_max']:.0f}]"
                if xs else "—")
        astr = (f"{am:.1f}[{as_['realtime_sec_min']:.0f}-{as_['realtime_sec_max']:.0f}]"
                if as_ else "—")
        spd = f"{speed:.2f}x" if speed else "—"
        flag = ""
        if speed and speed < 1.0:
            flag = "  ⚠ arm64 SLOWER"
        # A stage with any nonzero exit on either leg didn't fully succeed —
        # its timing is not a valid comparison. Flag loudly.
        bad = (xs and xs["nonzero_exits"]) or (as_ and as_["nonzero_exits"])
        if bad:
            flag += "  ✗ FAILED TASKS"
        save = xcost - acost
        print(f"{s:17} {xstr:22} {astr:22} {spd:>8} {xcost:8.4f} {acost:8.4f} {save:7.4f}{flag}")
        comparison[s] = {"x86": xs, "arm64": as_, "speedup_x86_over_arm64": speed,
                         "cost_save_usd": round(save, 4)}

    print("-" * len(hdr))
    print(f"{'TOTAL (sum)':17} {'':22} {'':22} {'':>8} "
          f"{tot_x:8.4f} {tot_a:8.4f} {tot_x-tot_a:7.4f}")
    if tot_x:
        print(f"\n    Whole-pipeline cost: x86 ${tot_x:.4f} → arm64 ${tot_a:.4f} "
              f"= {100*(tot_x-tot_a)/tot_x:.1f}% cheaper (measured, N={args.n}).")
    # Peak RSS per stage (memory behavior can differ by arch — esp. Kraken2).
    print("\n    Peak RSS (MB) per stage — x86 / arm64:")
    for s in stages:
        xs, as_ = x86.get(s), arm.get(s)
        xr = xs["peak_rss_mb"] if xs and xs["peak_rss_mb"] is not None else "—"
        ar = as_["peak_rss_mb"] if as_ and as_["peak_rss_mb"] is not None else "—"
        print(f"      {s:17} {xr} / {ar}")

    # Data-movement (source staging + convert + compress) per leg, if timings
    # were captured. This is the "how long / how much to stage from sources"
    # view, separate from the per-stage compute table above.
    x86_staging = load_staging_timings(args.x86_staging) if args.x86_staging else []
    arm_staging = load_staging_timings(args.arm64_staging) if args.arm64_staging else []
    if x86_staging or arm_staging:
        report_staging(x86_staging, "x86")
        report_staging(arm_staging, "arm64")

    # DB-delivery comparison — copy time + the compute-$ that copy-time costs on
    # a billed worker, vs. the EBS-$ of the zero-copy volume. Uses the arm64 leg's
    # consuming-stage instances (Kraken2/MetaPhlAn) + the measured S3 throughput
    # from staging timings (RODA MB/s, same-region proxy for the DB download rate).
    arm_dl_mbps = _med([r.get("roda_mbps") for r in arm_staging]) if arm_staging else None
    db_cmp = db_delivery_comparison(arm, "arm64", arm_dl_mbps)
    print("\n  ── DB delivery: copy time & cost (arm64 leg) ──")
    print(f"     throughput for download estimate: {arm_dl_mbps} MB/s (measured, same-region)"
          if arm_dl_mbps else "     (no throughput measured — download time/$ left null)")
    for label, d in db_cmp["per_stage"].items():
        b = d["B_per_task_dl"]
        c = d["C_zero_copy_vol"]
        a = d["A_baked_ami"]
        print(f"     {label} ({d['instance']}, {d['db_gib']} GiB DB, n={d['n_tasks']}):")
        print(f"        A baked-AMI       : copy 0s, compute $0, "
              f"root-EBS ${a['per_run_root_ebs_usd']}/run")
        if b["copy_s_each"] is not None:
            print(f"        B per-task dl     : copy ~{b['copy_s_each']}s/task "
                  f"({b['copy_s_total']}s total), WASTED compute ${b['wasted_compute_usd']}/run")
        print(f"        C zero-copy vol   : copy ~0 (symlink+mount), compute $0, "
              f"vol-EBS ${c['per_run_volume_ebs_usd']}/run")
    t = db_cmp["per_run_totals"]
    print(f"     per-run: download wasted-compute ${t['download_wasted_compute_usd']} "
          f"vs zero-copy volume-EBS ${t['zero_copy_volume_ebs_usd']} "
          f"(+ standing snapshot ${t['standing_snapshot_usd_per_mo']}/mo)")
    print("     → " + db_cmp["reading"].replace("\n", " "))

    print("\n    Reading: per-stage is the real story — Kraken2 (memory-bound) is the")
    print("    swing factor we couldn't predict from price alone. Negative (arm64-")
    print("    slower) stages, if any, are flagged above and kept in.")
    print(f"    N={args.n} is a PILOT — variance is real; one run/leg is anecdote-grade.\n")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({
                "n_samples_per_leg": args.n,
                "note": "PILOT. Both legs native (no emulation). Median realtime; "
                        "cost = Σ task duration × on-demand $/hr. Staging records carry "
                        "per-instance env (instance_type/net_driver/az) — times are "
                        "placement-dependent and qualified by it.",
                "prices_usd_per_hr": PRICE_USD_PER_HR,
                "per_stage": comparison,
                "total_cost_usd": {"x86": round(tot_x, 4), "arm64": round(tot_a, 4)},
                "data_movement": {"x86": x86_staging, "arm64": arm_staging},
                "db_delivery_comparison": db_cmp,
            }, f, indent=2)
        print(f"    Wrote {args.json}")


if __name__ == "__main__":
    main()
