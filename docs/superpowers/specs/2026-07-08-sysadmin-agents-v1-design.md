# Спека: Агентская система системного администрирования v1

**Дата:** 2026-07-08

**Стек:** Python 3.12 asyncio · aiogram 3.x · `openai` SDK → DeepSeek (generic, конфигурируется через env) · `aiodocker` · `structlog` · `pydantic` / `pydantic-settings` · Docker Compose деплой.

## Цели

- Серверный агент-сисадмин, управляемый через Telegram одним авторизованным пользователем.
- Итерация 1: Директор (планер/роутер) + Главный сисадмин (Docker-специалист).
- Масштабируемость в команду без переделки ядра.
- Чистый код, простота, минимум зависимостей, деплой через `docker compose`.

## Не-цели (вне scope v1)

- Постоянное хранилище/БД (состояние в памяти, аудит-лог через structlog).
- SSH-управление другими хостами.
- Облачные API.
- Webhook-режим бота (только long-polling).
- Мультипользовательская авторизация (whitelist из одного `TELEGRAM_USER_ID`).
- `shell_exec` на хосте — НЕ включать в v1. Только Docker API tools. `docker_exec` — dangerous (подтверждение).

## Архитектура

Паттерн: **иерархическая делегация через in-memory шину сообщений**.

- Bot (aiogram, long-polling, whitelist по `TELEGRAM_USER_ID`) → создаёт `Task` → inbox Директора.
- `Agent` runtime (`agents/base.py`): задача → LLM(system-prompt + tools) → tool_call | финал → выполнить tool → покормить результатом → повторить → финальный ответ автору.
- Реестр + шина (`agents/registry.py`): асинхронные inbox'ы, регистрация ролей. Точка масштабирования.
- Tools (`tools/`): классификация `safe` (чтение, авто) / `dangerous` (мутации, подтверждение через Telegram inline Approve/Reject, таймаут 5 мин → авто-отклонение). Схемы для LLM генерируются автоматически из Pydantic-моделей.
- LLM-клиент (`llm/client.py`): `openai` SDK с generic `base_url`/`model` (по умолчанию DeepSeek `https://api.deepseek.com` / `deepseek-chat`), хелпер одного round-trip; цикл tool-calling — в `agents/base.py`.
- Аудит-лог: `structlog` → stdout + append-only файл (том), без БД. Поля: `task_id, agent, tool, args, result, timestamp`.

## Роли

**Director** — получает задачу от бота, планирует, единственный инструмент `delegate_to(agent_name, task)`, собирает результат, формирует финальный ответ. Не имеет Docker-tools (чистое разделение ответственности).

**ChiefSysadmin** — техподзадачи от Директора. Tools:
- `safe` (авто): `docker_ps`, `docker_logs`, `docker_stats`, `docker_inspect`, `compose_ls`, `compose_ps`
- `dangerous` (подтверждение): `docker_restart`, `docker_stop`, `docker_start`, `compose_up`, `compose_down`, `docker_exec`

Возвращает структурированный отчёт.

## Поток данных (пример «посмотри логи docker телеграмм-бота и выдай отчёт»)

1. Вы пишете боту → bot-хендлер создаёт `Task` → отправляет в inbox Директора.
2. Директор: LLM решает делегировать → `delegate_to("sysadmin", "найди контейнер телеграмм-бота, дай отчёт по логам")`.
3. Главсисадмин: `docker_ps` (safe, авто) → находит контейнер → `docker_logs` (safe, авто) → LLM анализирует → формирует отчёт → возвращает Директору.
4. Директор: формирует финальный ответ → бот отправляет вам.
5. Все шаги — в аудит-логе под одним `task_id`.

Опасный путь («перезапусти бот»): `docker_restart` (dangerous) → runtime перехватывает → бот присылает inline «Approve / Reject» с точной командой → одобрили → выполняется → отчёт. Таймаут 5 мин → авто-отклонение.

## Масштабируемость

Новая роль = класс-агент (system-prompt + tools) + регистрация + обновление system-prompt Директора (чтобы знал, кому делегировать). Ядро (runtime, шина, подтверждения, бот) не меняется. Директор остаётся единым роутером/планером, специалисты изолированы.

## Структура проекта

```
opencode_agents_system/
  pyproject.toml
  .env.example
  Dockerfile
  docker-compose.yml
  app/
    __init__.py
    main.py                 # сборка + startup/shutdown
    config.py               # pydantic-settings (env)
    logging.py              # structlog setup
    llm/
      __init__.py
      client.py             # generic LLM client (один round-trip)
    tools/
      __init__.py
      base.py               # Tool dataclass, Safety, schema gen, execute
      docker.py             # docker + compose tools (aiodocker + subprocess)
    agents/
      __init__.py
      messages.py           # Task, Result, ConfirmationRequest
      registry.py           # AgentRegistry + ConfirmationGateway Protocol
      base.py               # Agent: цикл LLM↔tools + confirmation interception
      director.py           # Director + delegate_to tool
      sysadmin.py           # ChiefSysadmin + docker tools wiring
    bot/
      __init__.py
      filters.py            # WhitelistFilter
      keyboards.py          # approve/reject inline keyboard
      gateway.py            # TelegramConfirmationGateway (impl ConfirmationGateway)
      handlers.py           # message→Task, callback_query→confirm
      bot.py                # Bot+Dispatcher assembly
  tests/
    __init__.py
    conftest.py
    test_config.py
    test_llm_client.py
    test_tools_base.py
    test_tools_docker.py
    test_messages.py
    test_registry.py
    test_agent_base.py
    test_director.py
    test_sysadmin.py
    test_bot_filters.py
    test_bot_gateway.py
    test_bot_handlers.py
```

## Конфигурация (env)

| Env | Default | Назначение |
|-----|---------|------------|
| `LLM_API_KEY` | — | ключ API LLM-провайдера |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-совместимый endpoint |
| `LLM_MODEL` | `deepseek-chat` | model ID |
| `TELEGRAM_BOT_TOKEN` | — | токен бота |
| `TELEGRAM_USER_ID` | — | ваш Telegram user id (whitelist) |
| `COMPOSE_PROJECTS_DIR` | `/opt` | директория с compose-проектами |
| `AGENT_MAX_ITERATIONS` | `10` | лимит итераций цикла LLM↔tools |
| `CONFIRMATION_TIMEOUT_SECONDS` | `300` | таймаут подтверждения опасных действий |
| `AUDIT_LOG_PATH` | `/data/audit.log` | путь к append-only аудит-логу |

## Деплой (docker compose)

- Один сервис `sysadmin-agents` на образе `python:3.12-slim` + `docker-cli` + `compose plugin`.
- Маунт `/var/run/docker.sock` (управление контейнерами хоста).
- Маунт тома под аудит-лог (`./audit:/data`).
- Маунт `COMPOSE_PROJECTS_DIR` для discovery compose-проектов.
- Long-polling.
