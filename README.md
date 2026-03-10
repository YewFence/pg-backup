> 这是一个 Barman 测试项目，使用 Tailscale 连接 PostgreSQL 数据库。

# Barman with Tailscale Setup

## 环境变量

创建 `.env` 文件：

```bash
TS_AUTHKEY=tskey-auth-xxxxx
```

获取 Tailscale auth key: https://login.tailscale.com/admin/settings/keys

## 启动

```bash
docker compose -f docker-compose.barman.yml up -d
```

## 配置 Barman

进入容器：

```bash
docker exec -it barman bash
```

配置 PostgreSQL 连接（在容器内）：

```bash
# 编辑 /etc/barman.conf 或创建 /etc/barman.d/pg.conf
```

## 通过 Tailscale 连接

Barman 容器会加入你的 Tailscale 网络，可以直接使用宿主机的 Tailscale IP 或 hostname 连接 PostgreSQL。
