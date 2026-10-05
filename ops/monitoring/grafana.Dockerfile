ARG UBUNTU_IMAGE=public.ecr.aws/docker/library/ubuntu@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401
FROM ${UBUNTU_IMAGE}
LABEL org.opencontainers.image.title="Grafana OSS from verified official 12.2.0 archive"
LABEL org.opencontainers.image.source="https://grafana.com/grafana/download/12.2.0?edition=oss"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libfontconfig1 wget \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 472 grafana \
    && useradd --uid 472 --gid 472 --home-dir /var/lib/grafana --no-create-home --shell /usr/sbin/nologin grafana
COPY --from=grafana_archive /grafana-12.2.0.verified.tar.gz /tmp/grafana.tar.gz
RUN echo 'c4f53551ed4887c792caeb9d02fa0c1a36e3db9ee8bdda32b1ced810cb135a93  /tmp/grafana.tar.gz' | sha256sum --check \
    && mkdir -p /usr/share/grafana /var/lib/grafana /var/log/grafana /etc/grafana/provisioning \
    && tar -xzf /tmp/grafana.tar.gz --strip-components=1 --directory=/usr/share/grafana \
    && rm /tmp/grafana.tar.gz \
    && chown -R 472:472 /var/lib/grafana /var/log/grafana
COPY ops/monitoring/grafana-entrypoint.sh /entrypoint.sh
ENV GF_PATHS_HOME=/usr/share/grafana \
    GF_PATHS_CONFIG=/usr/share/grafana/conf/defaults.ini \
    GF_PATHS_DATA=/var/lib/grafana \
    GF_PATHS_LOGS=/var/log/grafana \
    GF_PATHS_PLUGINS=/var/lib/grafana/plugins \
    GF_PATHS_PROVISIONING=/etc/grafana/provisioning
USER 472:472
EXPOSE 3000
ENTRYPOINT ["sh", "/entrypoint.sh"]
