#!/usr/bin/env bash
# user-devices <id> — список HWID-устройств пользователя.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-devices <id>}"
api GET "/api/hwid/devices/$id" | jq '.response'
