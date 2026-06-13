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
import statistics
import sys

# us-east-1 on-demand Linux $/hr. The x86↔arm64 pairs are identical vCPU/RAM
# spec, differing ONLY in instance family — the controlled variable.
PRICE_USD_PER_HR: dict[str, float] = {
    "c7i.large": 0.089250,
    "c7i.2xlarge": 0.357000,
    "r7i.2xlarge": 0.529200,
    "c7g.large": 0.072500,
    "c7g.2xlarge": 0.290000,
    "r7g.2xlarge": 0.428400,
}

# Map a taxprofiler process name (trace `name`, e.g. "FASTP_PAIRED (SRR059371)")
# to (stage_label, x86_instance, arm64_instance). Per the protocol's fairness
# controls, EVERY measured stage runs on a matched non-burstable family pair
# differing only in arch — NO t4g anywhere (burstable credit state would corrupt
# timing). process_single stages are c7i.large↔c7g.large (NOT a constant): they
# include MultiQC/krakentools/standard-report, which are measured. FETCH_FASTQ is
# also process_single; its container is ncbi/sra-tools (multi-arch, not aarchbio)
# — reported, but flagged as not part of the aarchbio-unblocks-it claim.
STAGE_MAP: list[tuple[str, str, str, str]] = [
    # match-substring (upper),    stage label,        x86,           arm64
    ("FETCH_FASTQ",               "FETCH_FASTQ",       "c7i.large",   "c7g.large"),
    ("FASTP",                     "fastp",             "c7i.2xlarge", "c7g.2xlarge"),
    ("FASTQC",                    "FastQC",            "c7i.2xlarge", "c7g.2xlarge"),
    ("METAPHLAN_METAPHLAN",       "MetaPhlAn",         "c7i.2xlarge", "c7g.2xlarge"),
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("x86_trace", help="trace.tsv from the all-x86 (c7i/r7i) N=5 run")
    ap.add_argument("arm64_trace", help="trace.tsv from the all-Graviton (c7g/r7g) N=5 run")
    ap.add_argument("--json", help="also write the full comparison as JSON to this path")
    ap.add_argument("--n", type=int, default=5, help="samples per leg (for the pilot note)")
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

    print("\n    Reading: per-stage is the real story — Kraken2 (memory-bound) is the")
    print("    swing factor we couldn't predict from price alone. Negative (arm64-")
    print("    slower) stages, if any, are flagged above and kept in.")
    print(f"    N={args.n} is a PILOT — variance is real; one run/leg is anecdote-grade.\n")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({
                "n_samples_per_leg": args.n,
                "note": "PILOT. Both legs native (no emulation). Median realtime; "
                        "cost = Σ task duration × on-demand $/hr.",
                "prices_usd_per_hr": PRICE_USD_PER_HR,
                "per_stage": comparison,
                "total_cost_usd": {"x86": round(tot_x, 4), "arm64": round(tot_a, 4)},
            }, f, indent=2)
        print(f"    Wrote {args.json}")


if __name__ == "__main__":
    main()
