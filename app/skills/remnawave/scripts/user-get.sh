#!/usr/bin/env bash
# user-get <uuid> — полная карточка пользователя.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-get <uuid>}"
api GET "/api/users/$uuid" | jq '.response | {
  uuid, username, status, expireAt,
  trafficLimitBytes, usedTrafficBytes, trafficLimitStrategy,
  hwidDeviceLimit, telegramId, email, subscriptionUrl
}'
