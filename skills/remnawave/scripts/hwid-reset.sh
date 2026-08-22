#!/usr/bin/env bash
# hwid-reset <id> [hwid] — сброс устройства: конкретного (если задан hwid) или всех.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: hwid-reset <id> [hwid]}"
need_id "$id"
hwid="${2:-}"
if [ -n "$hwid" ]; then
  api POST "/api/hwid/devices/delete" "{\"userId\":$id,\"hwid\":\"$hwid\"}" | jq '.response'
else
  api POST "/api/hwid/devices/delete-all" "{\"userId\":$id}" | jq '.response'
fi
