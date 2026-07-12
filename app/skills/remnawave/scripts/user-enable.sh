#!/usr/bin/env bash
# user-enable <uuid> — включить (снять приостановку) пользователя.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-enable <uuid>}"
api POST "/api/users/$uuid/actions/enable" | jq '.response | {uuid, username, status}'
