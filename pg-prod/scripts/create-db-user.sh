#!/bin/bash
# 在宿主机运行，调用容器内的 psql 交互式创建 db/user

read -p "db: " DB
read -p "user: " USER
read -sp "pass: " PASS
echo

docker exec -it postgres psql -U postgres -c \
  "CREATE USER \"${USER}\" WITH PASSWORD '${PASS}'; CREATE DATABASE \"${DB}\" OWNER \"${USER}\"; GRANT ALL PRIVILEGES ON DATABASE \"${DB}\" TO \"${USER}\"; GRANT ALL ON SCHEMA public TO \"${USER}\";"

echo ""
echo "done: db=${DB} user=${USER}"
