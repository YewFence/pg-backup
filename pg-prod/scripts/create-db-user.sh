#!/bin/bash
# 在宿主机运行，调用容器内的 psql 交互式创建 db/user

read -p "db: " DB
read -p "user: " USER

echo -n "pass: "
PASS=""
while IFS= read -r -n1 -s char; do
  if [[ "$char" == $'\0' ]]; then
    break
  fi
  if [[ "$char" == $'\177' ]]; then
    if [[ -n "$PASS" ]]; then
      PASS="${PASS%"${PASS##?}"}"
      echo -ne "\b \b"
    fi
  else
    PASS+="$char"
    echo -n "*"
  fi
done
echo

docker exec postgres psql -U postgres \
  -c "CREATE USER \"${USER}\" WITH PASSWORD '${PASS}';" \
  -c "CREATE DATABASE \"${DB}\" OWNER \"${USER}\";" \
  -c "GRANT ALL PRIVILEGES ON DATABASE \"${DB}\" TO \"${USER}\";" \
  -c "GRANT ALL ON SCHEMA public TO \"${USER}\";"

echo ""
echo "done: db=${DB} user=${USER}"
