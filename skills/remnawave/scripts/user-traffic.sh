#!/usr/bin/env bash
# user-traffic <id> — трафик/лимит/стратегия сброса.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-traffic <id>}"
api GET "/api/users/$id" | jq '.response | {
  id, username, status,
  usedTrafficBytes: .userTraffic.usedTrafficBytes,
  trafficLimitBytes, trafficLimitStrategy,
  lifetimeUsedTrafficBytes: .userTraffic.lifetimeUsedTrafficBytes,
  onlineAt: .userTraffic.onlineAt
}'
