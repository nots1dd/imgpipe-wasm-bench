# Local container build (Docker available). On the GCP VM use
# scripts/pack_oci.py instead — the Review-1 testbed has no dockerd/buildkit.
# Build args: LIBC=musl (static musl) or LIBC=glibc (crt-static gnu).
# bench scripts stage binaries into .docker-bin/<libc>/ first.
FROM scratch
ARG LIBC=musl
COPY .docker-bin/${LIBC}/imgpipe /imgpipe
COPY .docker-bin/${LIBC}/edgeserve /edgeserve
COPY dataset/ /data/
ENTRYPOINT ["/imgpipe"]
