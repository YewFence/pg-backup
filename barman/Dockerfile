# syntax=docker/dockerfile:1.7

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM debian:bookworm-slim

# renovate: datasource=deb depName=barman suite=bookworm-pgdg
ARG BARMAN_VERSION=3.20.0-2.pgdg12+1
# renovate: datasource=deb depName=postgresql-client-17 suite=bookworm-pgdg
ARG POSTGRESQL_CLIENT_VERSION=17.11-1.pgdg12+2
# renovate: datasource=pypi depName=boto3
ARG BOTO3_VERSION=1.43.56
ARG BARMAN_UID=1000
ARG BARMAN_GID=1000

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    cron \
    curl \
    gosu \
    gnupg \
    lsb-release \
    passwd \
    util-linux \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    barman=${BARMAN_VERSION} \
    barman-cli=${BARMAN_VERSION} \
    python3-barman=${BARMAN_VERSION} \
    postgresql-client-17=${POSTGRESQL_CLIENT_VERSION} \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT=600 uv pip install \
    --target /usr/local/lib/python3.11/dist-packages \
    "boto3==${BOTO3_VERSION}"

# Keep the Debian package user, but make its numeric identity explicit.
RUN groupmod --gid "${BARMAN_GID}" barman && \
    usermod --uid "${BARMAN_UID}" --gid "${BARMAN_GID}" --home /var/lib/barman barman && \
    mkdir -p /var/lib/barman /var/log/barman && \
    touch /var/log/barman/barman.log && \
    chown -R barman:barman /var/lib/barman /var/log/barman

ENV BARMAN_UID=${BARMAN_UID} \
    BARMAN_GID=${BARMAN_GID} \
    BARMAN_FIX_OWNERSHIP=true \
    HOME=/var/lib/barman \
    PGPASSFILE=/var/lib/barman/.pgpass

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY setup-pgpass.sh /usr/local/bin/setup-pgpass.sh
COPY barman-for-each-server.sh /usr/local/bin/barman-for-each-server
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY health-check.py /usr/local/bin/health-check.py
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/setup-pgpass.sh /usr/local/bin/barman-for-each-server /usr/local/bin/entrypoint.sh

WORKDIR /var/lib/barman

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/usr/local/bin/entrypoint.sh"]
