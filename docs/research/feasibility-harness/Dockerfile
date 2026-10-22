FROM mirror.gcr.io/library/debian:bookworm-slim
ARG https_proxy
COPY proxy-ca.crt /tmp/proxy-ca.crt
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::https::CAInfo=/tmp/proxy-ca.crt -o Acquire::Retries=3 update \
 && apt-get -o Acquire::https::CAInfo=/tmp/proxy-ca.crt install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/* /tmp/proxy-ca.crt
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin slot
