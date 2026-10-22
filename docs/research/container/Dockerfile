# Research Mode participant image (spec D4, ISO-05).
#
# Deliberately minimal: Debian slim plus python3, which the in-container
# launcher (synchri/research/launcher.py) needs to bridge loopback to the relay
# socket. Provider CLIs are NOT baked in; the pinned binary is mounted
# read-only at run time from its hashed host path (CFG-03, PC-13).
#
# Build, then pin the resulting image by digest or image ID in the manifest.
# The "proxyca" build context is always required: an empty directory when no
# proxy is involved, or a directory holding the proxy's CA as ca.crt.
#   mkdir -p /tmp/no-proxy-ca
#   docker build --build-context proxyca=/tmp/no-proxy-ca \
#     -t synchri-research-runner:1 docs/research/container
#   docker image inspect synchri-research-runner:1 --format '{{.Id}}'
#
# Behind a CONNECT-only HTTPS proxy, pass the proxy and CA for the build step
# only (they never reach a participant container):
#   docker build --network host --build-arg https_proxy=$HTTPS_PROXY \
#     --build-context proxyca=/path/to/ca-dir -t synchri-research-runner:1 docs/research/container
FROM mirror.gcr.io/library/debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8
ARG https_proxy
RUN --mount=type=bind,from=proxyca,target=/proxyca \
    set -eu; \
    if [ -f /proxyca/ca.crt ]; then CA="-o Acquire::https::CAInfo=/proxyca/ca.crt"; else CA=""; fi; \
    sed -i 's#http://deb.debian.org#https://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources; \
    apt-get $CA -o Acquire::Retries=3 update; \
    apt-get $CA install -y --no-install-recommends python3 ca-certificates; \
    rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin slot
