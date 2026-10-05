#!/usr/bin/env python3
"""Pack imgpipe artifacts as OCI image archives — no Docker/BuildKit required.

Produces a tar in OCI image layout that `sudo ctr images import` accepts on the
GCP VM (the Review-1 testbed has containerd but no dockerd/buildkit).

  python3 scripts/pack_oci.py container   # scratch + musl binary  -> imgpipe-container.tar
  python3 scripts/pack_oci.py wasm        # scratch + wasm module  -> imgpipe-wasm.tar

Import & run on the VM:
  sudo ctr images import imgpipe-container.tar
  sudo ctr run --rm localhost/cc-imgpipe:local t1 /imgpipe --mode full --in /data/d640 --out /data/out
  sudo ctr images import imgpipe-wasm.tar
  sudo ctr run --rm --runtime io.containerd.wasmtime.v1 --platform wasi/wasm \
      localhost/cc-imgpipe-wasm:local t1 /imgpipe.wasm --mode full --in /data/d640 --out /data/out
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_layer(files):
    """files: list of (arcname, src_path_or_None_for_dir, mode). Returns
    (compressed_bytes, diff_id)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for arcname, src, mode in files:
            if src is None:
                ti = tarfile.TarInfo(arcname)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.mtime = 0
                tf.addfile(ti)
            else:
                ti = tf.gettarinfo(src, arcname)
                ti.mode = mode
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                with open(src, "rb") as f:
                    tf.addfile(ti, f)
    raw = buf.getvalue()
    return gzip.compress(raw, mtime=0), f"sha256:{sha256(raw)}"


def blob_store(tmp, digest_hex, data):
    d = os.path.join(tmp, "blobs", "sha256")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, digest_hex), "wb") as f:
        f.write(data)
    return {"mediaType": None, "digest": f"sha256:{digest_hex}", "size": len(data)}


def pack(kind: str, datasets, out_path):
    if kind == "container":
        binary = os.path.join(ROOT, "target/x86_64-unknown-linux-musl/release/imgpipe")
        entry, os_name, arch = "/imgpipe", "linux", "amd64"
        ref = "localhost/cc-imgpipe:local"
    else:
        binary = os.path.join(ROOT, "target/wasm32-wasip1/release/imgpipe.wasm")
        entry = "/imgpipe.wasm"
        # runwasi historically accepts os/arch wasi/wasm; newer releases also
        # accept wasip1/wasm. Override with WASM_OCI_OS if your shim complains.
        os_name, arch = os.environ.get("WASM_OCI_OS", "wasi"), "wasm"
        ref = "localhost/cc-imgpipe-wasm:local"

    if not os.path.exists(binary):
        raise SystemExit(f"missing {binary} — build it first (see README)")

    files = [(os.path.basename(entry), binary, 0o755), ("data", None, 0o755),
             ("data/out", None, 0o755)]
    for ds in datasets:
        d = os.path.join(ROOT, "dataset", ds)
        if not os.path.isdir(d):
            raise SystemExit(f"missing dataset {d} — run: python3 scripts/bench.py gen --datasets {ds}")
        files.append((f"data/{ds}", None, 0o755))
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".jpg"):
                files.append((f"data/{ds}/{fn}", os.path.join(d, fn), 0o644))

    layer_gz, diff_id = build_layer(files)
    tmp = os.path.join(ROOT, "out", f"oci-{kind}")
    os.makedirs(tmp, exist_ok=True)

    layer_desc = blob_store(tmp, sha256(layer_gz), layer_gz)
    layer_desc["mediaType"] = "application/vnd.oci.image.layer.v1.tar+gzip"

    config = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(0)),
        "architecture": arch,
        "os": os_name,
        "config": {"Entrypoint": [entry], "WorkingDir": "/"},
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        "history": [{"created": config_time(), "created_by": "pack_oci.py"}],
    }
    config_b = json.dumps(config, separators=(",", ":")).encode()
    config_desc = blob_store(tmp, sha256(config_b), config_b)
    config_desc["mediaType"] = "application/vnd.oci.image.config.v1+json"

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config_desc,
        "layers": [layer_desc],
    }
    manifest_b = json.dumps(manifest, separators=(",", ":")).encode()
    man_desc = blob_store(tmp, sha256(manifest_b), manifest_b)
    man_desc.update({
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "annotations": {"org.opencontainers.image.ref.name": ref},
    })

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [man_desc],
    }
    with open(os.path.join(tmp, "index.json"), "w") as f:
        json.dump(index, f)
    with open(os.path.join(tmp, "oci-layout"), "w") as f:
        json.dump({"imageLayoutVersion": "1.0.0"}, f)

    with tarfile.open(out_path, "w") as tf:
        for base, _, names in os.walk(tmp):
            for n in names:
                p = os.path.join(base, n)
                tf.add(p, os.path.relpath(p, tmp))
    print(f"wrote {out_path}  (ref: {ref}, platform: {os_name}/{arch}, "
          f"{os.path.getsize(out_path)/1e6:.1f} MB)")


def config_time():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["container", "wasm"])
    ap.add_argument("--datasets", default="d640,d1080")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(ROOT, f"imgpipe-{args.kind}.tar")
    pack(args.kind, args.datasets.split(","), out)


if __name__ == "__main__":
    main()
