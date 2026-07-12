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
  curl "${args[@]}"
}
