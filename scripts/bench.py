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
}

BIN_NATIVE = os.path.join(ROOT, "target/release/imgpipe")
BIN_MUSL = os.path.join(ROOT, "target/x86_64-unknown-linux-musl/release/imgpipe")
BIN_WASM = os.path.join(ROOT, "target/wasm32-wasip1/release/imgpipe.wasm")
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


def build_cmd(target: str, mode: str, dataset: str, outdir: str):
    """Return (argv, stdin_note). Paths are translated per target."""
    host_in = dataset_dir(dataset)
    if target == "native":
        return [BIN_NATIVE, "--mode", mode, "--in", host_in, "--out", outdir]
    if target == "musl":
        return [BIN_MUSL, "--mode", mode, "--in", host_in, "--out", outdir]
    if target == "wasmtime":
        os.makedirs(outdir, exist_ok=True)
        return [
            WASMTIME, "run",
            f"--dir={host_in}::/data/in",
            f"--dir={outdir}::/data/out",
            BIN_WASM, "--",
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
                            row = run_parallel(target, argv, par, dataset)
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


def run_parallel(target, argv, par, dataset):
    """Run `par` instances of argv concurrently on hardlinked dataset shards;
    report aggregate throughput from external wall time."""
    shard_root = tempfile.mkdtemp(prefix=f"imgpipe-par{par}-")
    base_in = dataset_dir(dataset)
    shards = []
    for i in range(par):
        sin = os.path.join(shard_root, f"in{i}")
        os.makedirs(sin)
        for f in os.listdir(base_in):
            if f.endswith(".jpg"):
                src = os.path.join(base_in, f)
                try:
                    os.link(src, os.path.join(sin, f))
                except OSError:
                    import shutil
                    shutil.copy(src, os.path.join(sin, f))
        # re-point the input path in argv (host paths only; for docker/ctr the
        # image already contains the dataset, so shards are only used for the
        # host-path targets — for image targets par instances share the image)
        a = list(argv)
        if target in ("native", "musl", "wasmtime"):
            for j, tok in enumerate(a):
                if tok == base_in or tok.endswith(f"{dataset}::/data/in"):
                    if "::" in tok:
                        a[j] = f"{sin}::/data/in"
                    else:
                        a[j] = sin
                elif tok == "--out" or (j > 0 and a[j - 1] == "--out"):
                    pass
            if target == "wasmtime":
                sout = os.path.join(shard_root, f"out{i}")
                os.makedirs(sout, exist_ok=True)
                a = [f"--dir={sout}::/data/out" if t.startswith("--dir=") and "/data/out" in t else t for t in a]
        shards.append(a)

    t0 = time.monotonic()
    procs = [subprocess.Popen(a, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for a in shards]
    rc = [p.wait() for p in procs]
    wall_ms = (time.monotonic() - t0) * 1000.0
    size, count = DATASETS[dataset]
    total = count * par
    row = {
        "mode": "full",
        "n": total,
        "wall_ms": f"{wall_ms:.3f}",
        "imgs_per_s": f"{total / (wall_ms / 1000.0):.3f}",
    }
    if any(c != 0 for c in rc):
        print(f"[warn] parallel run had failures: {rc}", file=sys.stderr)
    import shutil
    shutil.rmtree(shard_root, ignore_errors=True)
    return row


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
    args = r.parse_args() if len(sys.argv) > 1 and sys.argv[1] == "run" else None

    if sys.argv[1] == "gen":
        gen_datasets(g.parse_args(sys.argv[2:]).datasets.split(","))
        return

    args.targets = args.targets.split(",")
    args.datasets = args.datasets.split(",")
    args.modes = args.modes.split(",")
    args.parallel = [int(x) for x in args.parallel.split(",")]
    gen_datasets([d for d in args.datasets if d in DATASETS])
    run_matrix(args)


if __name__ == "__main__":
    main()
