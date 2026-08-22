# Общая библиотека для скриптов remnawave. Подключается через `source`.
# Требует в окружении: REMNAWAVE_BASE_URL, REMNAWAVE_API_KEY, (опц.) REMNAWAVE_TIMEOUT.
set -euo pipefail

: "${REMNAWAVE_BASE_URL:?REMNAWAVE_BASE_URL не задан}"
: "${REMNAWAVE_API_KEY:?REMNAWAVE_API_KEY не задан}"

BASE="${REMNAWAVE_BASE_URL%/}"
TIMEOUT="${REMNAWAVE_TIMEOUT:-30}"

# api <METHOD> <path> [json-body]
api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS --max-time "$TIMEOUT" -X "$method" "$BASE$path"
    -H "Authorization: Bearer $REMNAWAVE_API_KEY"
    -H "Content-Type: application/json")
  if [ -n "$body" ]; then
    args+=(-d "$body")
  fi
  local out
  out=$(curl "${args[@]}")
  # Ошибки панели приходят без .response ({"message":...,"errorCode":...}).
  # Отдать их как есть и упасть: пустая карточка из null'ов молча врёт, что всё прошло.
  if [ "$(jq -r 'has("response")' <<<"$out" 2>/dev/null)" != "true" ]; then
    echo "$out" >&2
    return 1
  fi
  echo "$out"
}

# need_id <value> — панель адресует пользователя числовым id (не uuid).
need_id() {
  [[ "$1" =~ ^[0-9]+$ ]] || {
    echo "{\"error\":\"нужен числовой id пользователя (из user-find), получено: $1\"}"
    exit 1
  }
}
