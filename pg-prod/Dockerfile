FROM postgres:17

# 预创建 /archive 目录并设置权限，避免命名卷首次挂载后 WAL 归档权限异常
RUN mkdir -p /archive && chown postgres:postgres /archive
