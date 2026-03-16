# pg-backup Justfile
# 两个独立服务：pg-prod（PostgreSQL 服务端）和 barman（备份接收端）

default:
    @just --list

# ── PG 服务端安装 ─────────────────────────────────────────────────────────────

# 初始化配置并部署 PG（生产环境首次部署）
pg-install:
    #!/usr/bin/env bash
    set -e
    SOURCE_DIR="pg-prod"
    TARGET_DIR="../pg-prod"
    ENV_FILE="$SOURCE_DIR/.env"

    echo "=== PostgreSQL 服务端安装向导 ==="
    echo ""

    if [ -d "$TARGET_DIR" ]; then
        echo "错误：目标目录 $TARGET_DIR 已存在，请先删除或重命名。"
        exit 1
    fi

    echo "请输入 PostgreSQL 超级用户密码（POSTGRES_PASSWORD）："
    read -rs POSTGRES_PASSWORD
    echo ""

    echo "请输入 Barman 用户密码（BARMAN_PASSWORD）："
    read -rs BARMAN_PASSWORD
    echo ""

    echo "请输入 Barman 流复制用户密码（STREAMING_BARMAN_PASSWORD）："
    read -rs STREAMING_BARMAN_PASSWORD
    echo ""

    cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
BARMAN_PASSWORD=${BARMAN_PASSWORD}
STREAMING_BARMAN_PASSWORD=${STREAMING_BARMAN_PASSWORD}
EOF

    echo "配置已写入 $ENV_FILE"
    echo ""
    echo "移动服务目录到 $TARGET_DIR..."
    cp -r "$SOURCE_DIR" "$TARGET_DIR"
    echo ""
    echo "✓ 完成！PostgreSQL 服务已部署到 $TARGET_DIR"
    echo ""
    echo "启动命令："
    echo "  cd $TARGET_DIR && docker compose up -d"

# ── Barman 备份端安装 ─────────────────────────────────────────────────────────

# 初始化配置并部署 Barman（备份服务器首次部署）
barman-install:
    #!/usr/bin/env bash
    set -e
    SOURCE_DIR="barman"
    TARGET_DIR="../barman"
    ENV_FILE="$SOURCE_DIR/.env"
    CONF_FILE="$SOURCE_DIR/config/streaming-backup-server.conf"
    CONF_EXAMPLE="$SOURCE_DIR/config/streaming-backup-server.conf.example"

    echo "=== Barman 备份端安装向导 ==="
    echo ""

    if [ -d "$TARGET_DIR" ]; then
        echo "错误：目标目录 $TARGET_DIR 已存在，请先删除或重命名。"
        exit 1
    fi

    echo "请输入 Tailscale Auth Key（TS_AUTHKEY，格式 tskey-auth-xxx）："
    read -rs TS_AUTHKEY
    echo ""

    echo "请输入 PostgreSQL 服务器的 Tailscale 主机名或 IP（PG_HOST）："
    read -r PG_HOST

    echo "请输入 Barman 用户密码（BARMAN_PASSWORD，需与 PG 端一致）："
    read -rs BARMAN_PASSWORD
    echo ""

    echo "请输入 Barman 流复制用户密码（STREAMING_BARMAN_PASSWORD，需与 PG 端一致）："
    read -rs STREAMING_BARMAN_PASSWORD
    echo ""

    cat > "$ENV_FILE" <<EOF
TS_AUTHKEY=${TS_AUTHKEY}
PG_HOST=${PG_HOST}
BARMAN_PASSWORD=${BARMAN_PASSWORD}
STREAMING_BARMAN_PASSWORD=${STREAMING_BARMAN_PASSWORD}
EOF

    echo "配置已写入 $ENV_FILE"

    # 如果 barman 服务器配置不存在，从 example 复制并替换 host
    if [ ! -f "$CONF_FILE" ] && [ -f "$CONF_EXAMPLE" ]; then
        sed "s/host=[^ ]*/host=${PG_HOST}/" "$CONF_EXAMPLE" > "$CONF_FILE"
        echo "Barman 服务器配置已生成：$CONF_FILE（host=${PG_HOST}）"
    fi

    echo ""
    echo "移动服务目录到 $TARGET_DIR..."
    cp -r "$SOURCE_DIR" "$TARGET_DIR"
    echo ""
    echo "✓ 完成！Barman 备份服务已部署到 $TARGET_DIR"
    echo ""
    echo "启动命令："
    echo "  cd $TARGET_DIR && docker compose up -d"
    echo ""
    echo "启动后可用以下命令验证："
    echo "  docker exec -it barman barman check all"

