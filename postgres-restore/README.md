# 临时 PostgreSQL 恢复实例

本目录是灾难恢复工具使用的一次性 Compose 模板，不单独安装，也不要直接复用生产 `.env`。

正常入口是：

```bash
mise run barman:restore:start -- \
  --restore-root /srv/native-docker/postgres-restore \
  --postgres-image postgres:17.10
```

恢复工具会先验证 `restore.json`、PostgreSQL 主版本、镜像 ID、权限状态、固定 bind-backed volume 与外部网络，然后显式调用本模板。实例固定使用独立容器身份，不声明 `postgres` 网络别名，只把端口发布到回环地址，并设置 `restart: "no"`。
