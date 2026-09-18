#!/usr/bin/env bash
# user-history <id> — последние (до 24) запросы подписки: когда, с какого IP, каким клиентом.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-history <id>}"
need_id "$id"
api GET "/api/users/$id/subscription-request-history" \
  | jq '.response | {total, records: (.records | map({requestAt, requestIp, userAgent}))}'
