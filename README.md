# wasm-bench — Wasm vs container vs microVM edge benchmarks

One Rust codebase → five artifacts, so every runtime executes the *same*
workloads:

| Artifact | Target triple | Runs on |
| --- | --- | --- |
| `target/release/{imgpipe,edgeserve,edgegun}` | `x86_64-unknown-linux-gnu` | bare host (reference) |
| `target-gnu/.../{imgpipe,edgeserve}` | gnu + `crt-static` | glibc scratch container |
| `target/x86_64-unknown-linux-musl/...` | static musl | musl scratch container / Firecracker rootfs |
| `target/wasm32-wasip1/release/imgpipe.wasm` | WASI p1 (+ optional `+simd128` in `target-simd/`) | wasmtime / runwasi (batch) |
| `target/wasm32-wasip2/release/edgeserve.wasm` | WASI p2 (sockets) | wasmtime / runwasi (network service) |

## The workloads

**A. imgpipe — batch image pipeline.** JPEG decode → Lanczos3 256px thumbnail
→ re-encode → write, over datasets dsmall (320×240×2000), d640 (640×480×40),
d1080 (1920×1080×12), d4k (3840×2160×6). Ablation modes: `noop` (cold start),
`io-only`, `decode-only`, `resize-only`, `no-io` (pure compute), `full`.

**B. edgeserve — HTTP thumbnail service.** `GET /thumb?id=N` runs the full
pipeline per request (network + compute); `GET /echo?n=K` returns K bytes with
zero compute (pure network path — the control). `edgegun` drives it: C
concurrent keep-alive connections × R requests each, client-side p50/p95/p99.

**C. telemetry fan-in — the high-concurrency task.** The edgeserve service
under the IoT-broker pattern: C sensors each hold a connection and send a
64-byte reading every 25 ms (`edgegun --think 25 --path /echo?n=64`), at
C ∈ {32, 128, 256}. Measures aggregate throughput, tail latency, and lost
readings (30 s timeout). This is the workload where runtime concurrency
support — not per-request speed — decides success.

## Quickstart (local)

```bash
# build all artifacts
cargo build --release
cargo build --release --target wasm32-wasip1
cargo build --release --target wasm32-wasip2
cargo build --release --target x86_64-unknown-linux-musl
CARGO_TARGET_DIR=target-simd RUSTFLAGS="-C target-feature=+simd128" \
  cargo build --release --target wasm32-wasip1
CARGO_TARGET_DIR=target-gnu RUSTFLAGS="-C target-feature=+crt-static" \
  cargo build --release --target x86_64-unknown-linux-gnu

# A. batch
python3 scripts/bench.py gen --datasets d640,d1080,dsmall
python3 scripts/bench.py run --targets native,wasmtime,wasmtime-simd,docker-musl,docker-glibc \
    --datasets d640,d1080 --parallel 1,2,4

# B. network service
python3 scripts/bench_net.py run --targets native,musl,docker-musl,docker-glibc,wasmtime \
    --endpoints echo,thumb --conns 1,8,32

# C. telemetry fan-in (high concurrency)
python3 scripts/bench_net.py run --targets native,musl,docker-musl,docker-glibc,wasmtime \
    --endpoints telemetry --conns 32,128,256 --reps 2 --think 25

python3 scripts/plot.py        # -> figs/ (fig1..fig11)
```

`bench_net.py` targets: `native`, `musl`, `docker-musl`, `docker-glibc`,
`wasmtime` locally; `ctr-ctr-musl`, `ctr-ctr-glibc`, `ctr-wasm` on the GCP VM;
`firecracker-loopback` via `scripts/firecracker/run_fc_net.sh`.

## Figures

| File | Shows |
| --- | --- |
| fig1 | batch throughput, all runtimes, same axes |
| fig2 | batch tail latency (p50/p99) |
| fig3 | stage ablation (which stage causes the gap) |
| fig4 | scale sweep (image resolution) |
| fig5 | cold start |
| fig6 | memory footprint |
| fig7 | load sweep (parallel instances) |
| fig8 | service throughput vs connections (echo/thumb) |
| fig9 | service tail latency under load |
| fig10 | libc ablation: musl vs glibc container |
| fig11 | telemetry fan-in: throughput + lost readings vs sensor count |

## Platform notes (discovered while benchmarking — read before extending)

- `wasm32-wasip1` has **no sockets**; the service needs `wasm32-wasip2`
  (`wasi:sockets`), run with `wasmtime -S tcp=y -S inherit-network`.
- `wasi:sockets` has **no `TCP_NODELAY`**; `set_nodelay` is a silent no-op and
  a header-then-body two-write response stalls ~40 ms/req (Nagle/delayed-ACK).
  edgeserve therefore sends header+body in a single write.
- **No wasi-threads**: the Wasm server serves connections serially. Flat
  throughput under concurrency is the platform, not a bug.
- A bare Firecracker rootfs boots with **`lo` down**; `ip link set lo up`
  before loopback TCP works (handled in `scripts/firecracker/build_rootfs_net.sh`).

## Layout

- `src/main.rs` — imgpipe pipeline + dataset generator
- `src/bin/edgeserve.rs`, `src/bin/edgegun.rs` — HTTP service + load generator
- `scripts/bench.py` — batch orchestrator → `results/results-<host>.csv`
- `scripts/bench_net.py` — network orchestrator → `results/results-net-<host>.csv`
- `scripts/plot.py` — figures (fig1–fig11)
- `scripts/pack_oci.py` — OCI images without buildkit (container-musl /
  container-glibc / wasm / wasm-net)
- `scripts/firecracker/` — microVM rootfs builders + boot/bench runners
  (batch, and network-over-guest-loopback)
- `results/` — 471 measured rows (committed)

GCP reproduction: `../GCP_SETUP.md`; final-review changes: `../SETUP_CHANGES.md`.
