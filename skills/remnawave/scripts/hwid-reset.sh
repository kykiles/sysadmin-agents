#!/usr/bin/env bash
# hwid-reset <uuid> [hwid] — сброс устройства: конкретного (если задан hwid) или всех.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: hwid-reset <uuid> [hwid]}"
hwid="${2:-}"
if [ -n "$hwid" ]; then
  api POST "/api/hwid/devices/delete" "{\"userUuid\":\"$uuid\",\"hwid\":\"$hwid\"}" | jq '.response'
else
  api POST "/api/hwid/devices/delete-all" "{\"userUuid\":\"$uuid\"}" | jq '.response'
fi
