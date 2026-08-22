#!/usr/bin/env bash
# user-get <id> — полная карточка пользователя (числовой id из user-find).
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-get <id>}"
api GET "/api/users/$id" | jq '.response | {
  id, username, status, expireAt,
  trafficLimitBytes, usedTrafficBytes: .userTraffic.usedTrafficBytes, trafficLimitStrategy,
  hwidDeviceLimit, telegramId, email, subscriptionUrl
}'
