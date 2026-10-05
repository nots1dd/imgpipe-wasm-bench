#!/usr/bin/env python3
"""imgpipe benchmark orchestrator.

Runs the imgpipe binary across runtime targets (native / wasmtime / docker /
containerd / runwasi), captures per-run JSON from the binary's stdout plus
external wall time and peak RSS, and appends rows to results/results-<host>.csv.

Usage:
  python3 scripts/bench.py gen                              # build datasets
  python3 scripts/bench.py run --targets native,wasmtime,docker \
      --datasets d640,d1080 --modes noop,full,no-io,decode-only,resize-only,io-only
  python3 scripts/bench.py run --targets ctr-wasm --datasets d640 ...   # on GCP

Targets:
  native      host binary (target/release/imgpipe)
  musl        static musl binary (what ships in the container/rootfs)
  wasmtime    wasmtime CLI with WASI preopens (tools/wasmtime/wasmtime)
  wasmtime-simd  same module built with -C target-feature=+simd128
  docker      `docker run --rm imgpipe:local` (dataset baked into image)
  ctr-ctr     `sudo ctr run` runc container (image: localhost/cc-imgpipe:local)
  ctr-wasm    `sudo ctr run --runtime io.containerd.wasmtime.v1` (localhost/cc-imgpipe-wasm:local)
  wasmedge    same, io.containerd.wasmedge.v1
"""

import argparse
import csv
import json
import os
import platform
import shlex
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")

DATASETS = {
    "d640": ("640x480", 40),
    "d1080": ("1920x1080", 12),
    "d4k": ("3840x2160", 6),
    "dsmall": ("320x240", 2000),
}

BIN_NATIVE = os.path.join(ROOT, "target/release/imgpipe")
BIN_MUSL = os.path.join(ROOT, "target/x86_64-unknown-linux-musl/release/imgpipe")
BIN_WASM = os.path.join(ROOT, "target/wasm32-wasip1/release/imgpipe.wasm")
BIN_WASM_SIMD = os.path.join(ROOT, "target-simd/wasm32-wasip1/release/imgpipe.wasm")
WASMTIME = os.environ.get(
    "WASMTIME_BIN", os.path.join(ROOT, "tools/wasmtime/wasmtime")
)

CSV_FIELDS = [
    "ts", "host", "target", "mode", "dataset", "parallel", "rep",
    "wall_ms", "mean_ms", "p50_ms", "p95_ms", "p99_ms", "imgs_per_s",
    "bytes_in", "bytes_out", "rss_kb", "rss_source",
]


def dataset_dir(name: str) -> str:
    return os.path.join(ROOT, "dataset", name)


def gen_datasets(names):
    for name in names:
        size, count = DATASETS[name]
        d = dataset_dir(name)
        if os.path.isdir(d) and len([f for f in os.listdir(d) if f.endswith(".jpg")]) == count:
            print(f"[gen] {name}: exists, skipping")
            continue
        os.makedirs(d, exist_ok=True)
        subprocess.run(
            [BIN_NATIVE, "--mode", "gen", "--size", size, "--count", str(count), "--out", d],
            check=True,
        )


def build_cmd(target: str, mode: str, dataset: str, outdir: str, host_in: str = None):
    """Return argv. `dataset` names a dir under dataset/; image-based targets
    (docker/ctr) use the copy baked into the image at /data/<dataset>.
    `host_in` overrides the input dir for host-path targets (parallel shards)."""
    in_dir = host_in or dataset_dir(dataset)
    if target == "native":
        return [BIN_NATIVE, "--mode", mode, "--in", in_dir, "--out", outdir]
    if target == "musl":
        return [BIN_MUSL, "--mode", mode, "--in", in_dir, "--out", outdir]
    if target in ("wasmtime", "wasmtime-simd"):
        os.makedirs(outdir, exist_ok=True)
        wasm = BIN_WASM if target == "wasmtime" else BIN_WASM_SIMD
        return [
            WASMTIME, "run",
            f"--dir={in_dir}::/data/in",
            f"--dir={outdir}::/data/out",
            wasm, "--",
            "--mode", mode, "--in", "/data/in", "--out", "/data/out",
        ]
    if target == "docker":
        # dataset baked into the image under /data/<dataset>
        return ["docker", "run", "--rm", "imgpipe:local",
                "--mode", mode, "--in", f"/data/{dataset}", "--out", "/data/out"]
    if target == "ctr-ctr":
        return ["sudo", "ctr", "run", "--rm", "localhost/cc-imgpipe:local",
                f"bench-{int(time.time()*1000)}",
                "/imgpipe", "--mode", mode, "--in", f"/data/{dataset}", "--out", "/data/out"]
    if target in ("ctr-wasm", "wasmedge"):
        rt = ("io.containerd.wasmtime.v1" if target == "ctr-wasm"
              else "io.containerd.wasmedge.v1")
        plat = os.environ.get("WASM_OCI_PLATFORM", "wasi/wasm")
        return ["sudo", "ctr", "run", "--rm", "--runtime", rt, "--platform", plat,
                "localhost/cc-imgpipe-wasm:local", f"bench-{int(time.time()*1000)}",
                "/imgpipe.wasm", "--mode", mode, "--in", f"/data/{dataset}", "--out", "/data/out"]
    raise ValueError(f"unknown target {target}")


def rss_source_for(target: str) -> str:
    # The binary self-reports VmHWM on Linux (native, musl, docker, ctr-ctr).
    # Under wasmtime there is no /proc in the sandbox, so we use the external
    # rusage of the wasmtime process (includes the runtime itself — honest).
    return "self_vmhwm" if target in ("native", "musl", "docker", "ctr-ctr") else "child_rusage"


def spawn_collect(argv):
    """fork/exec argv, capture stdout+stderr to a temp file, return
    (ok, wall_ms, ru_maxrss_kb, parsed_json_or_None, raw_tail)."""
    fd, tmp = tempfile.mkstemp(prefix="imgpipe-", suffix=".log")
    os.close(fd)
    t0 = time.monotonic()
    pid = os.fork()
    if pid == 0:
        fd2 = os.open(tmp, os.O_WRONLY | os.O_TRUNC)
        os.dup2(fd2, 1)
        os.dup2(fd2, 2)
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os._exit(127)
    _, status, ru = os.wait4(pid, 0)
    wall_ms = (time.monotonic() - t0) * 1000.0
    with open(tmp, errors="replace") as f:
        raw = f.read()
    os.unlink(tmp)
    payload = None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return os.waitstatus_to_exitcode(status) == 0, wall_ms, ru.ru_maxrss, payload, raw[-400:]


def maybe_drop_caches(enabled: bool):
    if not enabled:
        return
    subprocess.run("sync && echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null",
                   shell=True, check=False)


def append_row(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def run_matrix(args):
    os.makedirs(RESULTS, exist_ok=True)
    csv_path = os.path.join(RESULTS, f"results-{args.host_label}.csv")
    outbase = os.path.join(ROOT, "out")
    n_rows = 0

    for mode in args.modes:
        reps = args.noop_reps if mode == "noop" else args.reps
        for dataset in args.datasets:
            if mode == "noop" and dataset != args.datasets[0]:
                continue  # cold start does not depend on dataset
            parallels = args.parallel if mode == "full" else [1]
            for par in parallels:
                for rep in range(reps):
                    for target in args.targets:
                        outdir = os.path.join(outbase, f"{target}-{mode}")
                        argv = build_cmd(target, mode, dataset, outdir)
                        if par > 1:
                            row = run_parallel(target, mode, dataset, par)
                        else:
                            maybe_drop_caches(args.drop_caches and mode == "noop")
                            ok, wall, ru_kb, payload, tail = spawn_collect(argv)
                            if not ok or payload is None:
                                print(f"[FAIL] {target} {mode} {dataset}: {tail}",
                                      file=sys.stderr)
                                continue
                            row = dict(payload)
                            row["wall_ms_ext"] = wall
                            if mode == "noop":
                                row["wall_ms"] = wall  # external cold-start time
                            row["rss_kb"] = payload.get("vm_hwm_kb") or ru_kb
                        row.update({
                            "ts": int(time.time()),
                            "host": args.host_label,
                            "target": target,
                            "dataset": dataset,
                            "parallel": par,
                            "rep": rep,
                            "rss_source": rss_source_for(target),
                        })
                        append_row(csv_path, row)
                        n_rows += 1
                        print(f"[ok] {target:9s} {mode:11s} {dataset:6s} par={par} "
                              f"rep={rep} wall={float(row.get('wall_ms', 0)):8.1f} ms")
    print(f"\nwrote {n_rows} rows -> {csv_path}")


def run_parallel(target, mode, dataset, par):
    """Run `par` instances concurrently and report aggregate throughput from
    external wall time. Host-path targets get hardlinked input shards; image
    targets (docker/ctr) share the read-only in-image dataset."""
    import shutil
    shard_root = tempfile.mkdtemp(prefix=f"imgpipe-par{par}-")
    host_path_target = target in ("native", "musl", "wasmtime", "wasmtime-simd")
    cmds = []
    for i in range(par):
        if host_path_target:
            sin = os.path.join(shard_root, f"in{i}")
            sout = os.path.join(shard_root, f"out{i}")
            os.makedirs(sin)
            os.makedirs(sout, exist_ok=True)
            for f in os.listdir(dataset_dir(dataset)):
                if f.endswith(".jpg"):
                    src = os.path.join(dataset_dir(dataset), f)
                    try:
                        os.link(src, os.path.join(sin, f))
                    except OSError:
                        shutil.copy(src, os.path.join(sin, f))
            cmds.append(build_cmd(target, mode, dataset, sout, host_in=sin))
        else:
            cmds.append(build_cmd(target, mode, dataset, "/data/out"))

    t0 = time.monotonic()
    procs = [subprocess.Popen(a, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for a in cmds]
    rc = [p.wait() for p in procs]
    wall_ms = (time.monotonic() - t0) * 1000.0
    shutil.rmtree(shard_root, ignore_errors=True)
    _, count = DATASETS[dataset]
    total = count * par
    if any(c != 0 for c in rc):
        print(f"[warn] parallel run had failures: {rc}", file=sys.stderr)
    return {
        "mode": mode,
        "n": total,
        "wall_ms": f"{wall_ms:.3f}",
        "imgs_per_s": f"{total / (wall_ms / 1000.0):.3f}",
    }


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--datasets", default="d640,d1080")
    r = sub.add_parser("run")
    r.add_argument("--targets", default="native,wasmtime")
    r.add_argument("--datasets", default="d640,d1080")
    r.add_argument("--modes", default="noop,full,no-io,decode-only,resize-only,io-only")
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--noop-reps", type=int, default=15)
    r.add_argument("--parallel", default="1")
    r.add_argument("--drop-caches", action="store_true")
    r.add_argument("--host-label", default=socket.gethostname())
    args = ap.parse_args()

    if args.cmd == "gen":
        gen_datasets(args.datasets.split(","))
        return

    args.targets = args.targets.split(",")
    args.datasets = args.datasets.split(",")
    args.modes = args.modes.split(",")
    args.parallel = [int(x) for x in args.parallel.split(",")]
    gen_datasets([d for d in args.datasets if d in DATASETS])
    run_matrix(args)


if __name__ == "__main__":
    main()
