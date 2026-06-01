FROM postgres:17

# 预创建 /archive 目录并设置权限
# 避免 Docker 自动创建时使用 root:root 导致 archive_command 权限错误
RUN mkdir -p /archive && chown postgres:postgres /archive
