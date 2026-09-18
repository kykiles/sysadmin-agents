# API панели remnawave 3.4 — для rw_curl_read / rw_curl_write

Сверено с официальной спецификацией (docs.rw/api, 3.4.4) и кодом панели 3.4.3 — между ними
пользователи, устройства и ноды не менялись. Пути ниже существуют; всего, чего здесь нет,
лучше не угадывать.

## Общее
- Префикс — `/api/users` (множественное число). `/api/user/...` не существует.
- Клиент адресуется **числовым `id`** (`GET /api/users/95`). Поля `uuid` у клиента нет.
- Ноды, наоборот, адресуются `uuid` (`GET /api/nodes/<uuid>`).
- Ответ — `{response: ...}`. Ошибка приходит без `response` (`{message, errorCode}`).
- Swagger в этой инсталляции выключен: `/api-json` и `/api/docs` отдают HTML фронтенда.
- 404 `Cannot GET ...` значит, что роута нет. Тот же путь методом POST не пробуй: подтверждение
  уйдёт пользователю впустую, а ответ будет тем же 404.

## Чтение (GET)
- `GET /api/users?size=100&start=0` → `{response: {total, users: [...]}}`; `size` до 1000.
- Поиск по полю — `filters` (URL-кодированный JSON):
  `GET /api/users?filters=[{"id":"telegramId","value":"923973582"}]`.
- `GET /api/users/by-username/<username>`, `GET /api/users/by-short-uuid/<shortUuid>`.
- `GET /api/users/<id>` — карточка. Трафик во вложенном `userTraffic`: `usedTrafficBytes`,
  `lifetimeUsedTrafficBytes`, `onlineAt`, `firstConnectedAt`, `lastConnectedNodeUuid`.
- `GET /api/users/<id>/accessible-nodes` — к каким нодам у клиента есть доступ (через сквады).
- `GET /api/users/<id>/subscription-request-history` — последние 24 запроса подписки.
- `GET /api/hwid/devices/<id>` → `{total, devices: [...]}`.
- `GET /api/nodes`, `GET /api/nodes/<uuid>`.
- `GET /api/system/stats/nodes`, `GET /api/system/metadata` (версия панели).

Единственное чтение через POST — `POST /api/users/resolve` (id / shortUuid / username →
клиент). Его заменяет `user-find`, отдельно не нужен.

## Изменения (с подтверждением)
- `POST /api/users/<id>/actions/{enable,disable,reset-traffic}` — без тела.
- `POST /api/users/<id>/actions/revoke` — `{"revokeOnlyPasswords": false}`
  (`true` — сменить только пароли, sub-ссылка останется прежней).
- `POST /api/users/<id>/actions/extend` — `{"days": N}`, N ≥ 1.
- `PATCH /api/users` — `{"id": <число>, ...поля}`: `status` (ACTIVE/DISABLED), `expireAt`,
  `trafficLimitBytes`, `trafficLimitStrategy`, `hwidDeviceLimit`, `telegramId`, `email`, `tag`.
- `POST /api/hwid/devices/delete` — `{"userId": <число>, "hwid": "..."}`;
  `POST /api/hwid/devices/delete-all` — `{"userId": <число>}`.
- Ноды: `POST /api/nodes/<uuid>/actions/{enable,disable,restart}`.

Массовые операции (`/api/users/bulk/...`, `/api/nodes/actions/restart-all`) задевают всех
клиентов сразу — без прямой просьбы пользователя их не вызывай.
