#!/usr/bin/env bash
# user-reset-traffic <uuid> — обнулить счётчик трафика пользователя.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-reset-traffic <uuid>}"
api POST "/api/users/$uuid/actions/reset-traffic" \
  | jq '.response | {uuid, username, usedTrafficBytes, trafficLimitBytes}'
