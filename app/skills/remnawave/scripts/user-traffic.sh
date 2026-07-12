#!/usr/bin/env bash
# user-traffic <uuid> — трафик/лимит/стратегия сброса.
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-traffic <uuid>}"
api GET "/api/users/$uuid" | jq '.response | {
  uuid, username, status,
  usedTrafficBytes, trafficLimitBytes, trafficLimitStrategy,
  lifetimeUsedTrafficBytes
}'
