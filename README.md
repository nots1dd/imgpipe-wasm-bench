# wasm-bench — Wasm vs container vs microVM edge benchmarks

One Rust codebase → five artifacts, so every runtime executes the *same*
workloads:

| Artifact | Target triple | Runs on |
| --- | --- | --- |
| `target/release/{imgpipe,edgeserve}` | `x86_64-unknown-linux-gnu` | bare host (reference) |
| `target-gnu/.../{imgpipe,edgeserve}` | gnu + `crt-static` | glibc scratch container |
| `target/x86_64-unknown-linux-musl/...` | static musl | musl scratch container / Firecracker rootfs |
| `target/wasm32-wasip1/release/imgpipe.wasm` | WASI p1 | wasmtime / runwasi (batch) |
| `target/wasm32-wasip2/release/edgeserve.wasm` | WASI p2 (sockets) | wasmtime / runwasi (network service) |

## The two workloads

- **imgpipe** — batch image pipeline: JPEG decode → Lanczos3 256px thumbnail →
  re-encode → write. Ablation modes: `noop`, `io-only`, `decode-only`,
  `resize-only`, `no-io`, `full`.
- **edgeserve** — HTTP thumbnail service (`GET /thumb?id=N`) plus a
  zero-compute `GET /echo?n=K` for the pure network path. `edgegun` is the
  load generator (connections × keep-alive requests, client-side percentiles).

## Quickstart (local)

```bash
cargo build --release
cargo build --release --target wasm32-wasip1
cargo build --release --target wasm32-wasip2
cargo build --release --target x86_64-unknown-linux-musl
CARGO_TARGET_DIR=target-simd RUSTFLAGS="-C target-feature=+simd128" \
  cargo build --release --target wasm32-wasip1
CARGO_TARGET_DIR=target-gnu RUSTFLAGS="-C target-feature=+crt-static" \
  cargo build --release --target x86_64-unknown-linux-gnu

python3 scripts/bench.py gen --datasets d640,d1080,dsmall
python3 scripts/bench.py run --targets native,wasmtime,wasmtime-simd,docker-musl,docker-glibc \
    --datasets d640,d1080 --parallel 1,2,4
python3 scripts/bench_net.py run --targets native,musl,docker-musl,docker-glibc,wasmtime \
    --endpoints echo,thumb --conns 1,8,32
python3 scripts/plot.py        # -> figs/ (fig1..fig10)
```

## Layout

- `src/main.rs` — imgpipe pipeline + dataset generator
- `src/bin/edgeserve.rs`, `src/bin/edgegun.rs` — HTTP service + load generator
- `scripts/bench.py` — batch orchestrator → `results/results-<host>.csv`
- `scripts/bench_net.py` — network orchestrator → `results/results-net-<host>.csv`
- `scripts/plot.py` — figures
- `scripts/pack_oci.py` — OCI images without buildkit (container-musl /
  container-glibc / wasm / wasm-net)
- `scripts/firecracker/` — microVM rootfs builders + boot/bench runners
  (batch, and network-over-guest-loopback)

GCP reproduction: `../GCP_SETUP.md`; final-review changes: `../SETUP_CHANGES.md`.
