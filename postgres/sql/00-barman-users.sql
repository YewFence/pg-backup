-- Barman 协调用户和 streaming_barman 复制用户的统一授权脚本。
-- 初始化和修复入口都复用这一份 SQL，避免把权限逻辑散落在 shell 里。

SELECT format('CREATE ROLE barman WITH LOGIN PASSWORD %L', :'barman_password')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = 'barman'
)
\gexec

SELECT format('CREATE ROLE streaming_barman WITH LOGIN REPLICATION PASSWORD %L', :'streaming_barman_password')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = 'streaming_barman'
)
\gexec

ALTER ROLE barman WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION PASSWORD :'barman_password';
GRANT pg_read_all_settings TO barman;
GRANT pg_read_all_stats TO barman;
GRANT EXECUTE ON FUNCTION pg_backup_start(text, boolean) TO barman;
GRANT EXECUTE ON FUNCTION pg_backup_stop(boolean) TO barman;
GRANT EXECUTE ON FUNCTION pg_switch_wal() TO barman;
GRANT EXECUTE ON FUNCTION pg_create_restore_point(text) TO barman;

DO $$
BEGIN
  IF current_setting('server_version_num')::integer >= 150000 THEN
    EXECUTE 'GRANT pg_checkpoint TO barman';
  END IF;
END
$$;

ALTER ROLE streaming_barman WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT REPLICATION PASSWORD :'streaming_barman_password';
