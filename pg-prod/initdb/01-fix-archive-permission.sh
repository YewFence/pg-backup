#!/bin/bash
# 修复 /archive 目录权限，避免 WAL 归档时出现权限错误

mkdir -p /archive
chown postgres:postgres /archive
chmod 700 /archive

echo "[init] /archive permission fixed"
