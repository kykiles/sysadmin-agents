#!/usr/bin/env bash
# user-find <query> — поиск по username / telegram_id / uuid. Компактная карточка.
source "$(dirname "$0")/_lib.sh"
q="${1:?usage: user-find <username|telegram_id|uuid>}"

_card='{uuid, username, status, expireAt, trafficLimitBytes, usedTrafficBytes, telegramId}'

if [[ "$q" =~ ^[0-9]+$ ]]; then
  # by-telegram-id → массив
  api GET "/api/users/by-telegram-id/$q" | jq ".response | map($_card)"
elif [[ "$q" =~ ^[0-9a-fA-F-]{36}$ ]]; then
  api GET "/api/users/$q" | jq ".response | $_card"
else
  api GET "/api/users/by-username/$q" | jq ".response | $_card"
fi
