# Local container build (Docker available). On the GCP VM use
# scripts/pack_oci.py instead — the Review-1 testbed has no dockerd/buildkit.
# Expects: musl binary staged at .docker-bin/imgpipe (bench.py stages it),
# datasets generated under dataset/.
FROM scratch
COPY .docker-bin/imgpipe /imgpipe
COPY dataset/ /data/
ENTRYPOINT ["/imgpipe"]
