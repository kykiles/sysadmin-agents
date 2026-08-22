#!/usr/bin/env bash
# user-find <query> — поиск по telegram_id / id / username. Компактная карточка.
# Панель 3.x адресует пользователя числовым id; uuid в путях не используется.
source "$(dirname "$0")/_lib.sh"
q="${1:?usage: user-find <telegram_id|id|username>}"

_card='{id, username, status, expireAt, trafficLimitBytes, usedTrafficBytes: .userTraffic.usedTrafficBytes, telegramId}'

if [[ "$q" =~ ^[0-9]+$ ]]; then
  # Число — это либо telegramId (фильтр по списку), либо внутренний id.
  enc="%5B%7B%22id%22%3A%22telegramId%22%2C%22value%22%3A%22$q%22%7D%5D"
  found=$(api GET "/api/users?size=100&filters=$enc")
  if [ "$(jq '.response.users | length // 0' <<<"$found")" -gt 0 ]; then
    jq ".response.users | map($_card)" <<<"$found"
  else
    api GET "/api/users/$q" | jq ".response | $_card"
  fi
else
  api GET "/api/users/by-username/$q" | jq ".response | $_card"
fi
