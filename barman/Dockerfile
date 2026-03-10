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

RUN mkdir -p /var/lib/barman && \
    chown -R barman:barman /var/lib/barman

COPY setup-pgpass.sh /usr/local/bin/setup-pgpass.sh
RUN chmod +x /usr/local/bin/setup-pgpass.sh

USER barman
WORKDIR /var/lib/barman

CMD ["/bin/sh", "-c", "setup-pgpass.sh && tail -f /dev/null"]
