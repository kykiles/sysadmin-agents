#!/usr/bin/env bash
# nodes — список нод: онлайн/офлайн, статус.
source "$(dirname "$0")/_lib.sh"
api GET "/api/nodes" | jq '.response | map({
  uuid, name, countryCode,
  isConnected, isNodeOnline, isDisabled,
  usersOnline
})'
