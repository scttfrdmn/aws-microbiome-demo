#!/usr/bin/env python3
"""
lifecycle_metrics.py — record + analyze the full from-scratch lifecycle of an
FSx-backed taxprofiler run, measuring TIME, DATA MOVED, and COST for every phase:

  provision (FSx create) → stage (DBs from canonical source) → run (@ N) → teardown

The point (per the AWS-demo goal): show a user exactly what it costs to do this
properly, broken into ONE-TIME (provision + stage, amortized over R runs) vs
PER-RUN (the run @ a given fan-out N), with N as the knob.

A measurement is a JSON record under benchmark/results/lifecycle/<run-id>.json with
phases[], each: {name, t_start, t_end, seconds, bytes_moved, instances:[{type,
seconds}], notes}. Cost is computed from instance billed-seconds × on-demand $/hr
plus FSx GB-hours + S3 request/storage where they matter.

This module is the recorder/analyzer; the orchestration (launching instances,
polling) is driven by the operator/scripts that call record_phase(). Pure stdlib.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

# ── on-demand $/hr (us-east-1), matched arch pairs + the staging/head types ──
PRICE_USD_HR = {
    "c7g.large": 0.0725, "c7g.xlarge": 0.1450, "c7g.2xlarge": 0.29,
    "c7i.large": 0.0850, "c7i.xlarge": 0.1700, "c7i.2xlarge": 0.3400,
    "r7g.2xlarge": 0.4288, "r7g.4xlarge": 0.8576,
    "r7i.2xlarge": 0.5292, "r7i.4xlarge": 1.0584,
    "t4g.small": 0.0134,
}
# FSx for Lustre PERSISTENT_2 SSD: storage $/GB-month + throughput $/MBps-month.
# us-east-1: ~$0.145/GB-mo storage; PERSISTENT_2 250 MB/s/TiB tier ≈ included per
# the per-GB SSD price for 250. We bill storage GB-hours (the dominant term for a
# short-lived 1200 GiB FS) and note throughput separately.
FSX_SSD_USD_GB_MONTH = 0.145
HOURS_PER_MONTH = 730.0
# S3: PUT $0.005/1k, GET $0.0004/1k, storage $0.023/GB-mo (standard). Staging is
# PUT-heavy but small object counts; storage of dbs-fsx is amortized.
S3_PUT_USD_PER_1K = 0.005
S3_GET_USD_PER_1K = 0.0004
S3_STORAGE_USD_GB_MONTH = 0.023


def fsx_cost(storage_gib: int, hours: float, throughput_mbps_per_tib: int = 250) -> float:
    """FSx Lustre storage cost for `hours` of a `storage_gib` filesystem."""
    return storage_gib * FSX_SSD_USD_GB_MONTH * (hours / HOURS_PER_MONTH)


def instances_cost(instances: list[dict]) -> float:
    """Σ over instances of billed_seconds/3600 × $/hr."""
    total = 0.0
    for inst in instances:
        price = PRICE_USD_HR.get(inst.get("type", ""), 0.0)
        total += (inst.get("seconds", 0) / 3600.0) * price
    return total


@dataclass
class Phase:
    name: str
    seconds: float = 0.0
    bytes_moved: int = 0
    instances: list = field(default_factory=list)   # [{type, seconds}]
    fsx_gib_hours: object = None                     # (gib, hours) tuple if FSx billed in this phase
    notes: dict = field(default_factory=dict)

    def cost(self) -> float:
        c = instances_cost(self.instances)
        if self.fsx_gib_hours:
            c += fsx_cost(self.fsx_gib_hours[0], self.fsx_gib_hours[1])
        # S3 PUTs during staging (object count in notes['s3_puts'])
        c += self.notes.get("s3_puts", 0) / 1000.0 * S3_PUT_USD_PER_1K
        return round(c, 4)


@dataclass
class Lifecycle:
    run_id: str
    arch: str
    n_samples: int
    samples_per_site: object = None
    phases: list = field(default_factory=list)

    def add(self, phase: Phase) -> None:
        self.phases.append(phase)

    def to_dict(self) -> dict:
        d = {"run_id": self.run_id, "arch": self.arch, "n_samples": self.n_samples,
             "samples_per_site": self.samples_per_site, "phases": []}
        one_time = 0.0
        per_run = 0.0
        for p in self.phases:
            pc = p.cost()
            pd = asdict(p)
            pd["cost_usd"] = pc
            d["phases"].append(pd)
            if p.name in ("provision", "stage"):
                one_time += pc
            elif p.name in ("run", "head", "teardown"):
                per_run += pc
        d["totals"] = {
            "one_time_usd": round(one_time, 4),
            "per_run_usd": round(per_run, 4),
            "total_usd": round(one_time + per_run, 4),
            "wall_clock_s": round(sum(p.seconds for p in self.phases), 1),
            "bytes_moved": sum(p.bytes_moved for p in self.phases),
        }
        return d

    def save(self, base="benchmark/results/lifecycle") -> str:
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"{self.run_id}.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path


def amortize(one_time_usd: float, per_run_usd: float, runs: int) -> float:
    """Effective $/run when the one-time provision+stage is spread over `runs` runs."""
    return round(one_time_usd / max(runs, 1) + per_run_usd, 4)


def render_report(d: dict) -> str:
    """Human-readable end-to-end report from a saved lifecycle dict."""
    L = []
    L.append("=" * 76)
    L.append(f"END-TO-END LIFECYCLE — {d['arch']} @ N={d['n_samples']}"
             + (f" ({d['samples_per_site']}/site)" if d.get("samples_per_site") else ""))
    L.append("=" * 76)
    L.append(f"{'phase':12} {'time':>10} {'data moved':>14} {'cost $':>10}")
    for p in d["phases"]:
        gb = p["bytes_moved"] / 1e9
        L.append(f"{p['name']:12} {p['seconds']/60:>8.1f}m {gb:>11.1f} GB {p['cost_usd']:>10.4f}")
    t = d["totals"]
    L.append("-" * 76)
    L.append(f"{'TOTAL':12} {t['wall_clock_s']/60:>8.1f}m "
             f"{t['bytes_moved']/1e9:>11.1f} GB {t['total_usd']:>10.4f}")
    L.append(f"\n  one-time (provision+stage): ${t['one_time_usd']:.4f}")
    L.append(f"  per-run (run @ N={d['n_samples']}): ${t['per_run_usd']:.4f}")
    L.append("\n  amortized $/run as one-time spreads over R runs:")
    for r in (1, 5, 10, 50):
        L.append(f"    R={r:<3} → ${amortize(t['one_time_usd'], t['per_run_usd'], r):.4f}/run")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(render_report(json.load(open(sys.argv[1]))))
    else:
        print(__doc__)
