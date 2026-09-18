#!/usr/bin/env bash
# user-extend <id> <days> — продлить подписку на N дней.
# Считает панель: активному — от текущего срока, истёкшему — от сегодня и
# возвращает статус ACTIVE; у DISABLED/LIMITED статус не трогает.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-extend <id> <days>}"
days="${2:?usage: user-extend <id> <days>}"
need_id "$id"
[[ "$days" =~ ^[1-9][0-9]*$ ]] || { echo '{"error":"days должно быть целым числом от 1"}'; exit 1; }

api POST "/api/users/$id/actions/extend" "{\"days\":$days}" \
  | jq '.response | {id, username, status, expireAt}'
