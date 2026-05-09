FROM debian:bookworm-slim

# renovate: datasource=deb depName=barman suite=bookworm-pgdg
ARG BARMAN_VERSION=3.18.0-2.pgdg12+2
# renovate: datasource=deb depName=postgresql-client-17 suite=bookworm-pgdg
ARG POSTGRESQL_CLIENT_VERSION=17.9-1.pgdg12+1

RUN apt-get update && \
    apt-get install -y \
    curl \
    gnupg \
    lsb-release \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && \
    apt-get install -y \
    barman=${BARMAN_VERSION} \
    barman-cli=${BARMAN_VERSION} \
    postgresql-client-17=${POSTGRESQL_CLIENT_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# renovate: datasource=github-releases depName=aptible/supercronic versioning=semver
ARG SUPERCRONIC_VERSION=v0.2.45
RUN curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

# 保持 Debian 包创建的 barman 用户不变，只修正运行时需要写入的目录权限
RUN mkdir -p /var/lib/barman /recover /var/log/barman && \
    touch /var/log/barman/barman.log && \
    chown -R barman:barman /var/lib/barman /recover /var/log/barman

COPY setup-pgpass.sh /usr/local/bin/setup-pgpass.sh
COPY barman-for-each-server.sh /usr/local/bin/barman-for-each-server
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY health-check.py /usr/local/bin/health-check.py
RUN chmod +x /usr/local/bin/setup-pgpass.sh /usr/local/bin/barman-for-each-server /usr/local/bin/entrypoint.sh

USER barman
WORKDIR /var/lib/barman

CMD ["/usr/local/bin/entrypoint.sh"]
