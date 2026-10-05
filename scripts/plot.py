#!/usr/bin/env python3
"""Render Review-2 figures from results/*.csv into figs/.

  fig1_throughput.png    baseline vs proposed, same axes (full pipeline)
  fig2_latency.png       p50/p99 per-image latency (full pipeline)
  fig3_ablation.png      which stage causes the gap (per-mode mean)
  fig4_size_sweep.png    throughput vs image resolution (scale)
  fig5_coldstart.png     noop cold-start wall time
  fig6_rss.png           peak RSS (full pipeline)
  fig7_concurrency.png   throughput vs parallel instances (load)

Usage: python3 scripts/plot.py [--host LABEL]
"""

import argparse
import csv
import glob
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGS = os.path.join(ROOT, "figs")

COLORS = {
    "native": "#7f7f7f",
    "musl": "#9e9e9e",
    "docker": "#1f77b4",
    "ctr-ctr": "#17becf",
    "wasmtime": "#d62728",
    "ctr-wasm": "#ff7f0e",
    "wasmedge": "#e377c2",
    "firecracker": "#2ca02c",
}
ORDER = ["native", "musl", "docker", "ctr-ctr", "wasmtime", "ctr-wasm",
         "wasmedge", "firecracker"]


def load_rows(host_filter=None):
    rows = []
    for path in sorted(glob.glob(os.path.join(ROOT, "results", "*.csv"))):
        with open(path) as f:
            for r in csv.DictReader(f):
                rows.append(r)
    if host_filter is None and rows:
        counts = defaultdict(int)
        for r in rows:
            counts[r["host"]] += 1
        host_filter = max(counts, key=counts.get)
    if host_filter:
        rows = [r for r in rows if r["host"] == host_filter]
    return rows, host_filter or "?"


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick(rows, mode, dataset=None, parallel="1"):
    out = defaultdict(list)
    for r in rows:
        if r["mode"] != mode:
            continue
        if dataset and r["dataset"] != dataset:
            continue
        if parallel is not None and str(r["parallel"]) != str(parallel):
            continue
        out[r["target"]].append(r)
    return out


def mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def bar_labels(ax, bars, fmt="{:.1f}"):
    for b in bars:
        h = b.get_height()
        ax.annotate(fmt.format(h), (b.get_x() + b.get_width() / 2, h),
                    ha="center", va="bottom", fontsize=8)


def fig_throughput(rows, host):
    grouped = pick(rows, "full", parallel="1")
    datasets = sorted({r["dataset"] for r in rows if r["mode"] == "full"})
    targets = [t for t in ORDER if t in grouped]
    fig, ax = plt.subplots(figsize=(7, 4))
    width = 0.8 / max(len(targets), 1)
    for i, t in enumerate(targets):
        vals, xs = [], []
        for j, ds in enumerate(datasets):
            v = mean([fnum(r["imgs_per_s"]) for r in grouped[t] if r["dataset"] == ds])
            if v is not None:
                xs.append(j + i * width)
                vals.append(v)
        bars = ax.bar(xs, vals, width * 0.9, label=t, color=COLORS[t])
        bar_labels(ax, bars)
    ax.set_xticks([j + 0.4 - width / 2 for j in range(len(datasets))])
    ax.set_xticklabels(datasets)
    ax.set_ylabel("throughput (images/s)")
    ax.set_title(f"Full pipeline throughput — baseline vs proposed (host: {host})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig1_throughput.png"), dpi=150)
    plt.close(fig)


def fig_latency(rows, host):
    ds = "d640"
    grouped = pick(rows, "full", dataset=ds)
    targets = [t for t in ORDER if t in grouped]
    fig, ax = plt.subplots(figsize=(7, 4))
    width = 0.35
    for i, metric in enumerate(["p50_ms", "p99_ms"]):
        vals = [mean([fnum(r[metric]) for r in grouped[t]]) for t in targets]
        xs = [j + i * width for j in range(len(targets))]
        ax.bar(xs, [v or 0 for v in vals], width * 0.9, label=metric.replace("_ms", ""))
    ax.set_xticks([j + width / 2 for j in range(len(targets))])
    ax.set_xticklabels(targets)
    ax.set_ylabel("per-image latency (ms)")
    ax.set_title(f"Tail latency, full pipeline, {ds} (host: {host})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig2_latency.png"), dpi=150)
    plt.close(fig)


def fig_ablation(rows, host):
    ds = "d640"
    modes = ["io-only", "decode-only", "resize-only", "no-io", "full"]
    targets = [t for t in ORDER if pick(rows, "full", dataset=ds).get(t)]
    fig, ax = plt.subplots(figsize=(8, 4))
    width = 0.8 / max(len(targets), 1)
    for i, t in enumerate(targets):
        vals, xs = [], []
        for j, m in enumerate(modes):
            g = pick(rows, m, dataset=ds).get(t, [])
            v = mean([fnum(r["mean_ms"]) for r in g])
            if v is not None:
                xs.append(j + i * width)
                vals.append(v)
        ax.bar(xs, vals, width * 0.9, label=t, color=COLORS[t])
    ax.set_xticks([j + 0.4 - width / 2 for j in range(len(modes))])
    ax.set_xticklabels(modes)
    ax.set_ylabel("mean per-image time (ms)")
    ax.set_title(f"Ablation: stage isolation, {ds} (host: {host})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig3_ablation.png"), dpi=150)
    plt.close(fig)


def fig_size_sweep(rows, host):
    grouped = pick(rows, "full", parallel="1")
    ds_px = {"d640": 640 * 480, "d1080": 1920 * 1080, "d4k": 3840 * 2160}
    fig, ax = plt.subplots(figsize=(7, 4))
    for t in [t for t in ORDER if t in grouped]:
        pts = []
        for ds, px in sorted(ds_px.items(), key=lambda kv: kv[1]):
            v = mean([fnum(r["imgs_per_s"]) for r in grouped[t] if r["dataset"] == ds])
            if v is not None:
                pts.append((px / 1e6, v, ds))
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "o-",
                    label=t, color=COLORS[t])
            for x, y, ds in pts:
                ax.annotate(ds, (x, y), textcoords="offset points",
                            xytext=(4, 4), fontsize=7)
    ax.set_xlabel("megapixels per image")
    ax.set_ylabel("throughput (images/s)")
    ax.set_title(f"Parameter sweep: image resolution (host: {host})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig4_size_sweep.png"), dpi=150)
    plt.close(fig)


def fig_coldstart(rows, host):
    grouped = pick(rows, "noop", parallel=None)
    targets = [t for t in ORDER if t in grouped]
    fig, ax = plt.subplots(figsize=(7, 4))
    means, lo, hi = [], [], []
    for t in targets:
        vals = [fnum(r["wall_ms"]) for r in grouped[t]]
        vals = [v for v in vals if v is not None]
        m = mean(vals) or 0
        means.append(m)
        lo.append(m - (min(vals) if vals else 0))
        hi.append((max(vals) if vals else 0) - m)
    bars = ax.bar(targets, means, yerr=[lo, hi], capsize=4,
                  color=[COLORS[t] for t in targets])
    bar_labels(ax, bars, "{:.0f}")
    ax.set_ylabel("cold start wall time (ms)")
    ax.set_title(f"Cold start: start -> module/binary initialised (host: {host})")
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig5_coldstart.png"), dpi=150)
    plt.close(fig)


def fig_rss(rows, host):
    grouped = pick(rows, "full", dataset="d640")
    targets = [t for t in ORDER if t in grouped]
    fig, ax = plt.subplots(figsize=(7, 4))
    vals = []
    for t in targets:
        v = mean([fnum(r["rss_kb"]) for r in grouped[t]])
        vals.append((v or 0) / 1024.0)
    bars = ax.bar(targets, vals, color=[COLORS[t] for t in targets])
    bar_labels(ax, bars, "{:.1f}")
    ax.set_ylabel("peak RSS (MB)")
    ax.set_title(f"Memory footprint, full pipeline d640 (host: {host})")
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig6_rss.png"), dpi=150)
    plt.close(fig)


def fig_concurrency(rows, host):
    fig, ax = plt.subplots(figsize=(7, 4))
    any_data = False
    for t in ORDER:
        pts = []
        pars = sorted({int(r["parallel"]) for r in rows
                       if r["mode"] == "full" and r["dataset"] == "d640"
                       and r["target"] == t})
        for p in pars:
            v = mean([fnum(r["imgs_per_s"]) for r in rows
                      if r["mode"] == "full" and r["dataset"] == "d640"
                      and r["target"] == t and int(r["parallel"]) == p])
            if v is not None:
                pts.append((p, v))
        if len(pts) >= 1:
            any_data = True
            ax.plot([p for p, _ in pts], [v for _, v in pts], "o-",
                    label=t, color=COLORS[t])
    if not any_data:
        plt.close(fig)
        return
    ax.set_xlabel("parallel instances")
    ax.set_ylabel("aggregate throughput (images/s)")
    ax.set_title(f"Parameter sweep: load (parallel instances), d640 (host: {host})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(FIGS, "fig7_concurrency.png"), dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    args = ap.parse_args()
    rows, host = load_rows(args.host)
    if not rows:
        raise SystemExit("no rows found in results/*.csv")
    os.makedirs(FIGS, exist_ok=True)
    fig_throughput(rows, host)
    fig_latency(rows, host)
    fig_ablation(rows, host)
    fig_size_sweep(rows, host)
    fig_coldstart(rows, host)
    fig_rss(rows, host)
    fig_concurrency(rows, host)
    print(f"figures written to {FIGS} (host filter: {host})")


if __name__ == "__main__":
    main()
