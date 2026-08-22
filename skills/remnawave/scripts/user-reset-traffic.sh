#!/usr/bin/env bash
# user-reset-traffic <id> — обнулить счётчик трафика пользователя.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-reset-traffic <id>}"
need_id "$id"
api POST "/api/users/$id/actions/reset-traffic" \
  | jq '.response | {id, username, usedTrafficBytes: .userTraffic.usedTrafficBytes, trafficLimitBytes}'
