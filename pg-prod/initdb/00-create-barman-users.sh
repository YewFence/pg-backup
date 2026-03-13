#!/bin/bash
set -e

# 创建 barman 用户 (SUPERUSER) 和 streaming_barman 用户 (REPLICATION)
#
# 如果想最小权限，可将 barman 改为:
#   CREATE ROLE barman WITH LOGIN PASSWORD '...';
#   GRANT pg_read_all_settings, pg_read_all_stats TO barman;
#   GRANT EXECUTE ON FUNCTION pg_backup_start, pg_backup_stop, pg_switch_wal, pg_create_restore_point TO barman;
# 个人使用场景下 SUPERUSER 足够且省事。

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE barman WITH LOGIN SUPERUSER PASSWORD '${BARMAN_PASSWORD}';
    CREATE ROLE streaming_barman WITH LOGIN REPLICATION PASSWORD '${STREAMING_BARMAN_PASSWORD}';
EOSQL
