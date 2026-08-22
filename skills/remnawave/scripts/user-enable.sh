#!/usr/bin/env bash
# user-enable <id> — включить (снять приостановку) пользователя.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-enable <id>}"
need_id "$id"
api POST "/api/users/$id/actions/enable" | jq '.response | {id, username, status}'
