#!/usr/bin/env bash
# Build a Firecracker rootfs (ext4) containing the static imgpipe binary,
# the d640 dataset, and an init that runs the benchmark on boot.
# Usage: scripts/firecracker/build_rootfs.sh [workdir]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${1:-$HOME/fc-bench}"
BIN="$REPO_ROOT/target/x86_64-unknown-linux-musl/release/imgpipe"
DATA="$REPO_ROOT/dataset/d640"
ALPINE_VER="3.20.3"
ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/alpine-minirootfs-${ALPINE_VER}-x86_64.tar.gz"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/img/hello/kernel/hello-vmlinux.bin"

[ -x "$BIN" ] || { echo "missing $BIN — run: cargo build --release --target x86_64-unknown-linux-musl"; exit 1; }
[ -d "$DATA" ] || { echo "missing $DATA — run: python3 scripts/bench.py gen --datasets d640"; exit 1; }

mkdir -p "$WORK"
cd "$WORK"
[ -f vmlinux ] || curl -fsSL -o vmlinux "$KERNEL_URL"
[ -f alpine-minirootfs.tar.gz ] || curl -fsSL -o alpine-minirootfs.tar.gz "$ALPINE_URL"

rm -rf rootfs rootfs.ext4
mkdir rootfs
tar -xzf alpine-minirootfs.tar.gz -C rootfs
cp "$BIN" rootfs/imgpipe
mkdir -p rootfs/data/d640 rootfs/data/out
cp "$DATA"/*.jpg rootfs/data/d640/

cat > rootfs/init <<'INIT'
#!/bin/sh
mount -t proc proc /proc
echo "FC-INIT-UPTIME-S: $(cut -d' ' -f1 /proc/uptime)"
/imgpipe --mode full --in /data/d640 --out /data/out
echo "FC-DONE"
poweroff -f
INIT
chmod +x rootfs/init

# mkfs.ext4 -d populates the image from a directory (no loop mount needed)
dd if=/dev/zero of=rootfs.ext4 bs=1M count=256 status=none
mkfs.ext4 -q -d rootfs rootfs.ext4
echo "built $WORK/rootfs.ext4"
