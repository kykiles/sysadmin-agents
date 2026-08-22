#!/usr/bin/env bash
# user-disable <id> — приостановить доступ пользователя.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-disable <id>}"
need_id "$id"
api POST "/api/users/$id/actions/disable" | jq '.response | {id, username, status}'
