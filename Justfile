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
    TARGET_DIR="../pg"
    ENV_FILE="$SOURCE_DIR/.env"

    echo "=== PostgreSQL 服务端安装向导 ==="
    echo ""

    if [ -d "$TARGET_DIR" ]; then
        echo "错误：目标目录 $TARGET_DIR 已存在，请先删除或重命名。"
        exit 1
    fi

    echo "请输入 PostgreSQL 超级用户密码（POSTGRES_PASSWORD，留空自动生成）："
    read -rs POSTGRES_PASSWORD
    echo ""
    if [ -z "$POSTGRES_PASSWORD" ]; then
        POSTGRES_PASSWORD=$(openssl rand -base64 24)
        echo "已自动生成 POSTGRES_PASSWORD"
    fi

    echo "请输入 Barman 用户密码（BARMAN_PASSWORD，留空自动生成）："
    read -rs BARMAN_PASSWORD
    echo ""
    if [ -z "$BARMAN_PASSWORD" ]; then
        BARMAN_PASSWORD=$(openssl rand -base64 24)
        echo "已自动生成 BARMAN_PASSWORD"
    fi

    echo "请输入 Barman 流复制用户密码（STREAMING_BARMAN_PASSWORD，留空自动生成）："
    read -rs STREAMING_BARMAN_PASSWORD
    echo ""
    if [ -z "$STREAMING_BARMAN_PASSWORD" ]; then
        STREAMING_BARMAN_PASSWORD=$(openssl rand -base64 24)
        echo "已自动生成 STREAMING_BARMAN_PASSWORD"
    fi

    cp "$SOURCE_DIR/.env.example" "$ENV_FILE"
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${POSTGRES_PASSWORD}|" "$ENV_FILE"
    sed -i "s|^BARMAN_PASSWORD=.*|BARMAN_PASSWORD=${BARMAN_PASSWORD}|" "$ENV_FILE"
    sed -i "s|^STREAMING_BARMAN_PASSWORD=.*|STREAMING_BARMAN_PASSWORD=${STREAMING_BARMAN_PASSWORD}|" "$ENV_FILE"

    echo ""
    echo "配置已写入 $ENV_FILE"
    echo ""
    echo "===== 密码汇总（请妥善保存）====="
    echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
    echo "BARMAN_PASSWORD=${BARMAN_PASSWORD}"
    echo "STREAMING_BARMAN_PASSWORD=${STREAMING_BARMAN_PASSWORD}"
    echo "================================="
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
    if [ -z "$TS_AUTHKEY" ]; then
        echo "错误：TS_AUTHKEY 不能为空。"
        exit 1
    fi

    echo "请输入 PostgreSQL 服务器的 Tailscale 主机名或 IP（PG_HOST）："
    read -r PG_HOST
    if [ -z "$PG_HOST" ]; then
        echo "错误：PG_HOST 不能为空。"
        exit 1
    fi

    echo "请输入 Barman 用户密码（BARMAN_PASSWORD，需与 PG 端一致）："
    read -rs BARMAN_PASSWORD
    echo ""
    if [ -z "$BARMAN_PASSWORD" ]; then
        echo "错误：BARMAN_PASSWORD 不能为空，请填写与 PG 端一致的密码。"
        exit 1
    fi

    echo "请输入 Barman 流复制用户密码（STREAMING_BARMAN_PASSWORD，需与 PG 端一致）："
    read -rs STREAMING_BARMAN_PASSWORD
    echo ""
    if [ -z "$STREAMING_BARMAN_PASSWORD" ]; then
        echo "错误：STREAMING_BARMAN_PASSWORD 不能为空，请填写与 PG 端一致的密码。"
        exit 1
    fi

    cp "$SOURCE_DIR/.env.example" "$ENV_FILE"
    sed -i "s|^TS_AUTHKEY=.*|TS_AUTHKEY=${TS_AUTHKEY}|" "$ENV_FILE"
    sed -i "s|^PG_HOST=.*|PG_HOST=${PG_HOST}|" "$ENV_FILE"
    sed -i "s|^BARMAN_PASSWORD=.*|BARMAN_PASSWORD=${BARMAN_PASSWORD}|" "$ENV_FILE"
    sed -i "s|^STREAMING_BARMAN_PASSWORD=.*|STREAMING_BARMAN_PASSWORD=${STREAMING_BARMAN_PASSWORD}|" "$ENV_FILE"

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
