#!/usr/bin/env bash
# user-devices <uuid> — список HWID-устройств пользователя.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-devices <uuid>}"
api GET "/api/hwid/devices/$uuid" | jq '.response'
