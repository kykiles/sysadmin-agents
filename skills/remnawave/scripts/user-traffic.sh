#!/usr/bin/env bash
# user-traffic <id> — трафик/лимит/стратегия сброса, последняя активность и нода.
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-traffic <id>}"
need_id "$id"
user=$(api GET "/api/users/$id")
# Панель отдаёт только uuid ноды — имя достаём здесь, чтобы агенту не ходить за ним вторым вызовом.
node_uuid=$(jq -r '.response.userTraffic.lastConnectedNodeUuid // empty' <<<"$user")
node=null
if [ -n "$node_uuid" ]; then
  # Ноду могли удалить — тогда отдаём хотя бы uuid, а не роняем весь ответ.
  node=$(api GET "/api/nodes/$node_uuid" | jq '.response | {name, countryCode, address}') \
    || node="{\"uuid\":\"$node_uuid\"}"
fi
jq --argjson node "$node" '.response | {
  id, username, status,
  usedTrafficBytes: .userTraffic.usedTrafficBytes,
  trafficLimitBytes, trafficLimitStrategy,
  lifetimeUsedTrafficBytes: .userTraffic.lifetimeUsedTrafficBytes,
  onlineAt: .userTraffic.onlineAt,
  firstConnectedAt: .userTraffic.firstConnectedAt,
  lastConnectedNode: $node
}' <<<"$user"
