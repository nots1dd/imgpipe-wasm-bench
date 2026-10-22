#!/usr/bin/env bash
# Boot the edgeserve Firecracker microVM, wait for the in-guest benchmark,
# and append the four result lines (echo/thumb × c1/c8) to results-net CSV.
# Usage: scripts/firecracker/run_fc_net.sh [host-label]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${WORK:-$HOME/fc-bench-net}"
SOCK=/tmp/fc-net.sock
LOG="$WORK/vm-net.log"
HOST_LABEL="${1:-$(hostname)}"
CSV="$REPO_ROOT/results/results-net-${HOST_LABEL}.csv"

[ -f "$WORK/rootfs.ext4" ] || { echo "run build_rootfs_net.sh first"; exit 1; }

sudo pkill -f firecracker 2>/dev/null || true
sudo rm -f "$SOCK"
sudo firecracker --api-sock "$SOCK" > "$LOG" 2>&1 &
FC_PID=$!
sleep 1

fcput() { sudo curl --max-time 5 --unix-socket "$SOCK" -X PUT "http://localhost$1" \
    -H 'Content-Type: application/json' -d "$2"; }

fcput /boot-source "{\"kernel_image_path\":\"$WORK/vmlinux\",\"boot_args\":\"console=ttyS0 reboot=k panic=1 pci=off init=/init\"}"
fcput /machine-config '{"vcpu_count":2,"mem_size_mib":2048}'
fcput /drives/rootfs "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$WORK/rootfs.ext4\",\"is_root_device\":true,\"is_read_only\":false}"
fcput /actions '{"action_type":"InstanceStart"}'

for _ in $(seq 1 600); do grep -q FC-DONE "$LOG" 2>/dev/null && break; sleep 1; done
sudo kill "$FC_PID" 2>/dev/null || true

grep -q FC-DONE "$LOG" || { echo "benchmark did not finish; see $LOG" >&2; exit 1; }
grep -E '^(===|\{")' "$LOG" | tee "$WORK/last-run.txt"

python3 - "$CSV" "$HOST_LABEL" "$WORK/last-run.txt" <<'PY'
import csv, json, os, sys, time

csv_path, host, log = sys.argv[1:4]
fields = ["ts","host","target","endpoint","conns","rep","req_per_s","p50_ms",
          "p95_ms","p99_ms","requests","errors","cold_ms","rss_kb"]
rows = []
section = None
for line in open(log):
    line = line.strip()
    if line.startswith("==="):
        section = line.strip("= ").strip()          # e.g. "echo c1"
    elif line.startswith('{"') and section:
        p = json.loads(line)
        ep, c = section.split()
        rows.append({
            "ts": int(time.time()), "host": host, "target": "firecracker-loopback",
            "endpoint": ep, "conns": int(c[1:]), "rep": 0,
            "req_per_s": p.get("req_per_s"), "p50_ms": p.get("p50_ms"),
            "p95_ms": p.get("p95_ms"), "p99_ms": p.get("p99_ms"),
            "requests": p.get("requests"), "errors": p.get("errors"),
            "cold_ms": "", "rss_kb": "",
        })
        section = None
new = not os.path.exists(csv_path)
os.makedirs(os.path.dirname(csv_path), exist_ok=True)
with open(csv_path, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    if new:
        w.writeheader()
    w.writerows(rows)
print(f"appended {len(rows)} firecracker-loopback rows -> {csv_path}")
PY
