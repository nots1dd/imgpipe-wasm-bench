# imgpipe — Wasm vs container vs microVM edge benchmark (Review 2)

One Rust codebase → three artifacts, so every runtime executes the *same*
real workload: an edge image pipeline (JPEG decode → Lanczos3 256px thumbnail
→ JPEG re-encode → write), the canonical edge/FaaS image task.

| Artifact | Target | Runs on |
| --- | --- | --- |
| `target/release/imgpipe` | `x86_64-unknown-linux-gnu` | bare host (reference) |
| `target/x86_64-unknown-linux-musl/release/imgpipe` | static musl | scratch container / Firecracker rootfs |
| `target/wasm32-wasip1/release/imgpipe.wasm` | WASI | wasmtime CLI / runwasi shim |

## Quickstart (local)

```bash
cargo build --release
cargo build --release --target wasm32-wasip1
cargo build --release --target x86_64-unknown-linux-musl

python3 scripts/bench.py gen --datasets d640,d1080
python3 scripts/bench.py run --targets native,wasmtime,docker \
    --datasets d640,d1080 --parallel 1,2,4
python3 scripts/plot.py        # -> figs/
```

## Modes (ablation)

| Mode | What runs | Isolates |
| --- | --- | --- |
| `noop` | init only | cold start |
| `io-only` | read + write bytes | WASI/VFS syscall path |
| `decode-only` | read + JPEG decode | decode stage |
| `resize-only` | read + decode + resize | + Lanczos3 compute |
| `no-io` | decode + resize + encode in memory | pure compute (no fs) |
| `full` | the real pipeline | everything |

## Layout

- `src/main.rs` — the pipeline + dataset generator
- `scripts/bench.py` — orchestrator (targets, sweeps, CSV)
- `scripts/plot.py` — figures for the report
- `scripts/pack_oci.py` — OCI images without buildkit (for the GCP VM)
- `scripts/firecracker/` — microVM rootfs build + boot bench
- `results/` — CSV rows, one file per host
- `figs/` — rendered figures

Full GCP reproduction: see `../GCP_SETUP.md`.
