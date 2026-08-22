#!/usr/bin/env bash
# user-revoke <id> — отозвать подписку (ротация ключей и sub-ссылки).
source "$(dirname "$0")/_lib.sh"
id="${1:?usage: user-revoke <id>}"
need_id "$id"
api POST "/api/users/$id/actions/revoke" '{"revokeOnlyPasswords":false}' \
  | jq '.response | {id, username, status, subscriptionUrl}'
