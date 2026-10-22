#!/usr/bin/env bash
# Build a Firecracker rootfs for the edgeserve network benchmark.
# Guest runs the musl-static server + load generator over loopback (validates
# the full TCP path inside the microVM without needing host TAP setup).
# Usage: scripts/firecracker/build_rootfs_net.sh [workdir]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${1:-$HOME/fc-bench-net}"
BIN="$REPO_ROOT/target/x86_64-unknown-linux-musl/release"
DATA="$REPO_ROOT/dataset/d640"
ALPINE_VER="3.20.3"
ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/alpine-minirootfs-${ALPINE_VER}-x86_64.tar.gz"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/img/hello/kernel/hello-vmlinux.bin"

for b in edgeserve edgegun; do
    [ -x "$BIN/$b" ] || { echo "missing $BIN/$b — cargo build --release --target x86_64-unknown-linux-musl"; exit 1; }
done
[ -d "$DATA" ] || { echo "missing $DATA — python3 scripts/bench.py gen --datasets d640"; exit 1; }

mkdir -p "$WORK"
cd "$WORK"
[ -f vmlinux ] || curl -fsSL -o vmlinux "$KERNEL_URL"
[ -f alpine-minirootfs.tar.gz ] || curl -fsSL -o alpine-minirootfs.tar.gz "$ALPINE_URL"

rm -rf rootfs rootfs.ext4
mkdir rootfs
tar -xzf alpine-minirootfs.tar.gz -C rootfs
cp "$BIN/edgeserve" "$BIN/edgegun" rootfs/
mkdir -p rootfs/data/d640
cp "$DATA"/*.jpg rootfs/data/d640/

cat > rootfs/init <<'INIT'
#!/bin/sh
mount -t proc proc /proc
ip link set lo up    # loopback is DOWN on a bare kernel; gun needs 127.0.0.1
echo "FC-INIT-UPTIME-S: $(cut -d' ' -f1 /proc/uptime)"
/edgeserve --port 8080 --data /data/d640 &
sleep 2
echo "=== echo c1 ==="
/edgegun --host 127.0.0.1 --port 8080 --path /echo?n=1024 --conns 1 --reqs 3000
echo "=== thumb c1 ==="
/edgegun --host 127.0.0.1 --port 8080 --path /thumb?id=7 --conns 1 --reqs 100
echo "=== echo c8 ==="
/edgegun --host 127.0.0.1 --port 8080 --path /echo?n=1024 --conns 8 --reqs 3000
echo "=== thumb c8 ==="
/edgegun --host 127.0.0.1 --port 8080 --path /thumb?id=7 --conns 8 --reqs 100
echo "=== telemetry c32 ==="
/edgegun --host 127.0.0.1 --port 8080 --path /echo?n=64 --conns 32 --reqs 10 --think 25
echo "=== telemetry c128 ==="
/edgegun --host 127.0.0.1 --port 8080 --path /echo?n=64 --conns 128 --reqs 10 --think 25
echo "FC-DONE"
poweroff -f
INIT
chmod +x rootfs/init

dd if=/dev/zero of=rootfs.ext4 bs=1M count=256 status=none
mkfs.ext4 -q -d rootfs rootfs.ext4
echo "built $WORK/rootfs.ext4"
