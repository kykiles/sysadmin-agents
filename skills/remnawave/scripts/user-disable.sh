#!/usr/bin/env bash
# user-disable <uuid> — приостановить доступ пользователя.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-disable <uuid>}"
api POST "/api/users/$uuid/actions/disable" | jq '.response | {uuid, username, status}'
