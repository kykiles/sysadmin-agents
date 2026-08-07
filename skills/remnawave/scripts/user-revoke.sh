#!/usr/bin/env bash
# user-revoke <uuid> — отозвать подписку (ротация ключей и sub-ссылки).
source "$(dirname "$0")/_lib.sh"
uuid="${1:?usage: user-revoke <uuid>}"
api POST "/api/users/$uuid/actions/revoke" '{"revokeOnlyPasswords":false}' \
  | jq '.response | {uuid, username, status, subscriptionUrl}'
