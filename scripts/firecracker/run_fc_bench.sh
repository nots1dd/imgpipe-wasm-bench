#!/usr/bin/env bash
# Boot the imgpipe Firecracker microVM, wait for the benchmark to finish,
# extract the JSON result line, and append a CSV row to results/.
# Usage: scripts/firecracker/run_fc_bench.sh [host-label]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${WORK:-$HOME/fc-bench}"
SOCK=/tmp/fc-bench.sock
LOG="$WORK/vm.log"
HOST_LABEL="${1:-$(hostname)}"
CSV="$REPO_ROOT/results/results-${HOST_LABEL}.csv"

[ -f "$WORK/rootfs.ext4" ] || { echo "run build_rootfs.sh first"; exit 1; }

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

T0=$(date +%s%N)
fcput /actions '{"action_type":"InstanceStart"}'

for _ in $(seq 1 300); do grep -q FC-DONE "$LOG" 2>/dev/null && break; sleep 1; done
T1=$(date +%s%N)

JSON=$(grep -E '^\{"mode"' "$LOG" | tail -1 || true)
UPTIME=$(grep FC-INIT-UPTIME-S "$LOG" | tail -1 | awk '{print $2}' || true)
sudo kill "$FC_PID" 2>/dev/null || true

if [ -z "$JSON" ]; then
    echo "no result line captured; check $LOG" >&2
    exit 1
fi
echo "guest result: $JSON"
echo "guest uptime at init: ${UPTIME}s; host wall: $(( (T1 - T0) / 1000000 )) ms"

python3 - "$CSV" "$HOST_LABEL" "$JSON" "$UPTIME" <<'PY'
import csv, json, os, sys

csv_path, host, js, uptime = sys.argv[1:5]
p = json.loads(js)
fields = ["ts","host","target","mode","dataset","parallel","rep","wall_ms","mean_ms",
          "p50_ms","p95_ms","p99_ms","imgs_per_s","bytes_in","bytes_out","rss_kb","rss_source"]
row = {k: "" for k in fields}
row.update({
    "ts": int(__import__("time").time()), "host": host, "target": "firecracker",
    "mode": p.get("mode","full"), "dataset": "d640", "parallel": 1, "rep": 0,
    "wall_ms": p.get("wall_ms"), "mean_ms": p.get("mean_ms"), "p50_ms": p.get("p50_ms"),
    "p95_ms": p.get("p95_ms"), "p99_ms": p.get("p99_ms"),
    "imgs_per_s": p.get("imgs_per_s"), "bytes_in": p.get("bytes_in"),
    "bytes_out": p.get("bytes_out"), "rss_kb": p.get("vm_hwm_kb") or "",
    "rss_source": "self_vmhwm",
})
new = not os.path.exists(csv_path)
os.makedirs(os.path.dirname(csv_path), exist_ok=True)
with open(csv_path, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    if new:
        w.writeheader()
    w.writerow(row)
print(f"appended firecracker row -> {csv_path}")
PY
