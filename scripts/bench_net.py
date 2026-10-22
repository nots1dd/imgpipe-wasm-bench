#!/usr/bin/env python3
"""edgeserve network benchmark orchestrator.

Starts edgeserve on a given target, waits for readiness, drives it with
edgegun (client-side latency + throughput), captures cold start and server
RSS, tears the server down. Rows go to results/results-net-<host>.csv.

  python3 scripts/bench_net.py run \
      --targets native,musl,docker-musl,docker-glibc,wasmtime \
      --endpoints echo,thumb --conns 1,8,32 --reps 3

Targets:
  native        gnu dynamic binary (reference)
  musl          static musl binary, bare
  docker-musl   scratch container, musl binary (published port)
  docker-glibc  scratch container, crt-static gnu binary (published port)
  wasmtime      wasmtime CLI, wasip2 module, -S tcp=y -S inherit-network
  ctr-ctr-musl / ctr-ctr-glibc   GCP: nerdctl run -p (containerd baseline)
  ctr-wasm      GCP: nerdctl run --network host --runtime io.containerd.wasmtime.v1
"""

import argparse
import csv
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
WASMTIME = os.environ.get("WASMTIME_BIN", os.path.join(ROOT, "tools/wasmtime/wasmtime"))

CSV_FIELDS = ["ts", "host", "target", "endpoint", "conns", "rep", "req_per_s",
              "p50_ms", "p95_ms", "p99_ms", "requests", "errors",
              "cold_ms", "rss_kb"]

ENDPOINT_REQS = {"echo": ("/echo?n=1024", 3000), "thumb": ("/thumb?id=7", 100)}


def stage_docker_bins():
    m = os.path.join(ROOT, "target/x86_64-unknown-linux-musl/release")
    g = os.path.join(ROOT, "target-gnu/x86_64-unknown-linux-gnu/release")
    os.makedirs(os.path.join(ROOT, ".docker-bin/musl"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, ".docker-bin/glibc"), exist_ok=True)
    for b in ("imgpipe", "edgeserve"):
        subprocess.run(["cp", f"{m}/{b}", os.path.join(ROOT, ".docker-bin/musl/")], check=True)
        subprocess.run(["cp", f"{g}/{b}", os.path.join(ROOT, ".docker-bin/glibc/")], check=True)


def build_images():
    stage_docker_bins()
    for libc in ("musl", "glibc"):
        subprocess.run(["docker", "build", "-q", "--build-arg", f"LIBC={libc}",
                        "-t", f"img-bench-{libc}:local", "."], cwd=ROOT, check=True)


def server_cmd(target: str, port: int):
    data = os.path.join(ROOT, "dataset/d640")
    if target == "native":
        return [os.path.join(ROOT, "target/release/edgeserve"),
                "--port", str(port), "--data", data], None
    if target == "musl":
        return [os.path.join(ROOT, "target/x86_64-unknown-linux-musl/release/edgeserve"),
                "--port", str(port), "--data", data], None
    if target == "wasmtime":
        return [WASMTIME, "run", "-S", "tcp=y", "-S", "inherit-network",
                f"--dir={data}::/data/d640",
                os.path.join(ROOT, "target/wasm32-wasip2/release/edgeserve.wasm"),
                "--", "--port", str(port), "--data", "/data/d640"], None
    if target in ("docker-musl", "docker-glibc"):
        libc = target.split("-")[1]
        name = f"edgeserve-{libc}-{port}"
        return ["docker", "run", "-d", "--rm", "--name", name,
                "-p", f"{port}:8080", "--entrypoint", "/edgeserve",
                f"img-bench-{libc}:local",
                "--port", "8080", "--data", "/data/d640"], name
    if target in ("ctr-ctr-musl", "ctr-ctr-glibc"):
        libc = target.split("-")[2]
        name = f"edgeserve-{libc}-{port}"
        return ["sudo", "nerdctl", "run", "-d", "--name", name,
                "-p", f"{port}:8080", "--entrypoint", "/edgeserve",
                f"localhost/cc-img-bench-{libc}:local",
                "--port", "8080", "--data", "/data/d640"], name
    if target == "ctr-wasm":
        # the runwasi shim cannot publish ports; host netns gives the module
        # direct access (same as wasmtime CLI's -S inherit-network)
        name = f"edgeserve-wasm-{port}"
        return ["sudo", "nerdctl", "run", "-d", "--name", name,
                "--network", "host",
                "--runtime", "io.containerd.wasmtime.v1",
                "localhost/cc-img-bench-wasm:local",
                "/edgeserve.wasm", "--port", str(port), "--data", "/data/d640"], name
    raise ValueError(target)


def stop_server(target, proc, cname):
    if cname:
        tool = "docker" if target.startswith("docker") else "sudo"
        cmd = ["docker", "rm", "-f", cname] if tool == "docker" else \
              ["sudo", "nerdctl", "rm", "-f", cname]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def probe_ready(port: int, timeout_s: float = 30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                if b"200 OK" in s.recv(4096):
                    return (time.monotonic() - t0) * 1000.0
        except OSError:
            time.sleep(0.01)
    return None


def server_rss_kb(target, proc, cname):
    try:
        if cname:
            tool = "docker" if target.startswith("docker") else "sudo nerdctl"
            pid = subprocess.check_output(
                tool.split() + ["inspect", "-f", "{{.State.Pid}}", cname],
                text=True).strip()
        else:
            pid = str(proc.pid)
        with open(f"/proc/{pid}/status") as f:
            for l in f:
                if l.startswith("VmHWM"):
                    return int(l.split()[1])
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--targets", default="native,musl,docker-musl,docker-glibc,wasmtime")
    r.add_argument("--endpoints", default="echo,thumb")
    r.add_argument("--conns", default="1,8,32")
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--host-label", default=socket.gethostname())
    r.add_argument("--base-port", type=int, default=18080)
    args = ap.parse_args()

    targets = args.targets.split(",")
    endpoints = args.endpoints.split(",")
    conns_list = [int(c) for c in args.conns.split(",")]
    if any(t.startswith("docker") for t in targets):
        build_images()
    os.makedirs(RESULTS, exist_ok=True)
    csv_path = os.path.join(RESULTS, f"results-net-{args.host_label}.csv")
    edgegun = os.path.join(ROOT, "target/release/edgegun")

    port = args.base_port
    for target in targets:
        port += 1
        cmd, cname = server_cmd(target, port)
        stderr = open(os.path.join(ROOT, "out", f"edgeserve-{target}.log"), "w") \
            if os.path.isdir(os.path.join(ROOT, "out")) else subprocess.DEVNULL
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)
        cold = probe_ready(port)
        if cold is None:
            print(f"[FAIL] {target}: server never ready", file=sys.stderr)
            stop_server(target, proc, cname)
            continue
        rss = server_rss_kb(target, proc, cname)
        print(f"[up] {target:14s} port={port} cold={cold:8.1f} ms rss={rss} kB")
        for ep in endpoints:
            path, reqs = ENDPOINT_REQS[ep]
            for conns in conns_list:
                for rep in range(args.reps):
                    out = subprocess.run(
                        [edgegun, "--host", "127.0.0.1", "--port", str(port),
                         "--path", path, "--conns", str(conns), "--reqs", str(reqs)],
                        capture_output=True, text=True, timeout=300)
                    try:
                        payload = json.loads(out.stdout.strip().splitlines()[-1])
                    except Exception:
                        print(f"[FAIL] {target} {ep} c{conns} r{rep}: {out.stdout[-200:]} {out.stderr[-200:]}",
                              file=sys.stderr)
                        continue
                    row = {"ts": int(time.time()), "host": args.host_label,
                           "target": target, "endpoint": ep, "conns": conns,
                           "rep": rep, "cold_ms": f"{cold:.1f}",
                           "rss_kb": rss or ""}
                    for k in ("req_per_s", "p50_ms", "p95_ms", "p99_ms",
                              "requests", "errors"):
                        row[k] = payload.get(k, "")
                    new = not os.path.exists(csv_path)
                    with open(csv_path, "a", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                        if new:
                            w.writeheader()
                        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})
                    print(f"  {ep:5s} c={conns:<3d} rep={rep} "
                          f"{float(payload['req_per_s']):9.0f} req/s "
                          f"p50={float(payload['p50_ms']):7.3f} ms "
                          f"p99={float(payload['p99_ms']):8.3f} ms")
        stop_server(target, proc, cname)
    print(f"done -> {csv_path}")


if __name__ == "__main__":
    main()
