FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y \
    barman \
    barman-cli \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/lib/barman && \
    chown -R barman:barman /var/lib/barman

USER barman
WORKDIR /var/lib/barman

CMD ["tail", "-f", "/dev/null"]
