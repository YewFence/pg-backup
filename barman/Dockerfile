FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y \
    curl \
    gnupg \
    lsb-release \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && \
    apt-get install -y \
    barman \
    barman-cli \
    postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

# 安装 supercronic（容器友好的 cron 替代品）
ARG SUPERCRONIC_VERSION=v0.2.33
RUN curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic

RUN mkdir -p /var/lib/barman /recover && \
    chown -R barman:barman /var/lib/barman /recover

COPY setup-pgpass.sh /usr/local/bin/setup-pgpass.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/setup-pgpass.sh /usr/local/bin/entrypoint.sh

USER barman
WORKDIR /var/lib/barman

CMD ["/usr/local/bin/entrypoint.sh"]
