FROM debian:bookworm-slim

# renovate: datasource=deb depName=barman suite=bookworm-pgdg
ARG BARMAN_VERSION=3.18.0-2.pgdg12+2
# renovate: datasource=deb depName=postgresql-client-17 suite=bookworm-pgdg
ARG POSTGRESQL_CLIENT_VERSION=17.9-1.pgdg12+1
ARG BARMAN_UID=1000
ARG BARMAN_GID=1000

RUN apt-get update && \
    apt-get install -y \
    curl \
    gosu \
    gnupg \
    lsb-release \
    passwd \
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

# Keep the Debian package user, but make its numeric identity explicit.
RUN groupmod --gid "${BARMAN_GID}" barman && \
    usermod --uid "${BARMAN_UID}" --gid "${BARMAN_GID}" --home /var/lib/barman barman && \
    mkdir -p /var/lib/barman /recover /var/log/barman && \
    touch /var/log/barman/barman.log && \
    chown -R barman:barman /var/lib/barman /recover /var/log/barman

ENV BARMAN_UID=${BARMAN_UID} \
    BARMAN_GID=${BARMAN_GID} \
    BARMAN_FIX_OWNERSHIP=true

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY setup-pgpass.sh /usr/local/bin/setup-pgpass.sh
COPY barman-for-each-server.sh /usr/local/bin/barman-for-each-server
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY health-check.py /usr/local/bin/health-check.py
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/setup-pgpass.sh /usr/local/bin/barman-for-each-server /usr/local/bin/entrypoint.sh

WORKDIR /var/lib/barman

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/usr/local/bin/entrypoint.sh"]
