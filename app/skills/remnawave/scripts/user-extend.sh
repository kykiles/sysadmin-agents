#!/usr/bin/env bash
# user-extend <uuid> <days> — продлить подписку на N дней.
# База отсчёта: max(текущий expireAt, сейчас) — не «съедаем» остаток.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-extend <uuid> <days>}"
days="${2:?usage: user-extend <uuid> <days>}"
[[ "$days" =~ ^[0-9]+$ ]] || { echo '{"error":"days должно быть целым числом"}'; exit 1; }

cur=$(api GET "/api/users/$uuid" | jq -r '.response.expireAt // empty')
now=$(date -u +%s)
cur_s=$(date -u -d "$cur" +%s 2>/dev/null || echo 0)
if [ "$cur_s" -gt "$now" ]; then base=$cur_s; else base=$now; fi
new=$(date -u -d "@$((base + days * 86400))" +%Y-%m-%dT%H:%M:%S.000Z)

api PATCH "/api/users" "{\"uuid\":\"$uuid\",\"expireAt\":\"$new\"}" \
  | jq '.response | {uuid, username, status, expireAt}'
