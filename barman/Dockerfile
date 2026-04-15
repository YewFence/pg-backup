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

# 将 barman 用户 UID/GID 改为 999，与 postgres:17 镜像中的 postgres 用户一致
# 这样无论本地恢复还是远程 SSH 恢复，文件权限都与 postgres 容器一致
# GID 999 被 systemd-journal 占用（容器内无用），先删除
RUN groupdel systemd-journal && \
    usermod -u 999 barman && \
    groupmod -g 999 barman && \
    mkdir -p /var/lib/barman /recover /var/log/barman && \
    touch /var/log/barman/barman.log && \
    chown -R barman:barman /var/lib/barman /recover /var/log/barman

COPY setup-pgpass.sh /usr/local/bin/setup-pgpass.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY health-check.py /usr/local/bin/health-check.py
RUN chmod +x /usr/local/bin/setup-pgpass.sh /usr/local/bin/entrypoint.sh

USER barman
WORKDIR /var/lib/barman

CMD ["/usr/local/bin/entrypoint.sh"]
