# Агентская система системного администрирования v1

Серверная агентная система (Директор + динамические субагенты), управляемая через Telegram одним пользователем. Управляет Docker-контейнерами, compose-проектами, базами данных и хостом.

## Возможности

- **Директор** — единственный постоянный агент. Принимает задачи на естественном языке, планирует и под каждую задачу собирает временных агентов через `spawn`. Только у Директора есть память команды.
- **Динамические субагенты** — заранее не существуют. Директор создаёт их на лету: роль одной фразой + набор **навыков** + задача. Агент живёт одну задачу, его контекст стирается после ответа. Несколько `spawn` в одном ответе идут параллельно; цепочку «A и B параллельно → C сводит результаты» строит сам Директор (межагентного messaging нет — шина это Директор).
- **Навыки (skill'ы)** — самодостаточный пакет: плейбук-инструкции в `SKILL.md` + инструменты в `tools.py`. Из них собирается любой субагент:
  - `docker` — контейнеры и compose: просмотр, логи, статистика, инспекция, перезапуск/остановка/запуск, `docker exec`, `compose up/down`
  - `db` — read-only запросы к БД в контейнерах (psql, mysql, sqlite3)
  - `host` — команды на хосте: firewall/iptables, systemd, диски
  - `deploy` — деплой сайтов под `/opt` на хосте: определяет способ (`deployments/deploy.sh` → `make deploy` → `docker compose up -d --build`) и выполняет его через `nsenter`
- **Безопасные операции** (чтение: `ps`, `logs`, `stats`, `inspect`, `docker_query`) — выполняются автоматически
- **Опасные операции** (мутации: `restart`, `stop`, `start`, `exec`, `compose up/down`, `shell_exec`) — требуют подтверждения через inline-кнопки в Telegram (таймаут 5 минут, авто-отклонение)
- **Масштабируемость** — новая возможность = один навык (markdown-плейбук + инструменты), без правок кода и без заведения агентов

## Требования

- Python 3.12+
- Docker + Docker Compose plugin
- Telegram Bot Token (получить у [@BotFather](https://t.me/BotFather))
- API-ключ LLM-провайдера (DeepSeek по умолчанию)

## Установка

### 1. Клонирование

```bash
git clone <repo-url> sysadmin-agents
cd sysadmin-agents
```

### 2. Настройка окружения

Скопируйте шаблон и заполните `.env`:

```bash
cp .env.example .env
```

Обязательные переменные:

| Переменная | Описание |
|---|---|
| `LLM_API_KEY` | API-ключ LLM-провайдера |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_USER_ID` | Ваш Telegram user ID (числовой) |

Опциональные (имеют значения по умолчанию):

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-совместимый endpoint |
| `LLM_MODEL` | `deepseek-chat` | ID модели |
| `COMPOSE_PROJECTS_DIR` | `/opt` | Директория с compose-проектами |
| `AGENT_MAX_ITERATIONS` | `10` | Лимит итераций LLM↔tools |
| `CONFIRMATION_TIMEOUT_SECONDS` | `300` | Таймаут подтверждения (сек) |
| `AUDIT_LOG_PATH` | `/data/audit.log` | Путь к аудит-логу |
| `DIALOG_DB_PATH` | `/data/dialog.db` | Путь к SQLite-базе диалоговой памяти |
| `DIALOG_HISTORY_LIMIT` | `20` | Сколько последних реплик Директор помнит |
| `DEPLOY_ALLOWED` | (пусто) | Сайты под `/opt`, разрешённые для деплоя (через запятую); пустой = деплой запрещён |

### 3. Получение Telegram User ID

Напишите боту [@userinfobot](https://t.me/userinfobot) — он вернёт ваш числовой ID.

### 4. Запуск

**Локально (для разработки):**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v          # запуск тестов
python -m app.main # запуск
```

**Через Docker Compose (продакшен):**

```bash
mkdir -p audit
docker compose up -d --build
```

## Использование

1. Напишите `/start` боту — он ответит «Система активна. Опишите задачу.»
2. Отправьте задачу на естественном языке, например:
   - `покажи все контейнеры`
   - `посмотри логи контейнера bot за последние 100 строк`
   - `перезапусти контейнер nginx`
   - `какие compose-проекты есть на сервере?`
3. Для опасных операций бот пришлёт inline-кнопки **Approve / Reject** — подтвердите или отклоните
4. Получите отчёт от Директора

**Память.** Память — зона ответственности только Директора. Диалоговую историю (последние `DIALOG_HISTORY_LIMIT` реплик, SQLite `/data/dialog.db`, переживает рестарт) он использует, чтобы ссылаться на контекст: «а теперь перезапусти его»; `/reset` её очищает. Долговременные факты об инфраструктуре Директор хранит и вспоминает сам: в промпт идёт только оглавление (области знаний и число фактов), сами факты дочитываются по запросу. Субагенты памяти не имеют — Директор вкладывает нужные факты прямо в их задачу, а устойчивые выводы из их отчётов сохраняет сам.

**Деплой сайтов.** Директор спавнит агента с навыком `deploy`; тот деплоит сайты под `/opt` **на самом хосте** (через `nsenter` в namespace хостового init — контейнеру нужны `SYS_ADMIN`/`SYS_PTRACE`). Способ определяется автоматически: свой `deployments/deploy.sh` → цель `deploy` в `Makefile` → `docker compose up -d --build`. Деплоить можно только сайты из `DEPLOY_ALLOWED`; сам запуск — опасная операция и требует подтверждения. Например: «покажи план деплоя glowshine» (безопасно, `deploy_plan`) → «задеплой glowshine» (подтверждение → `deploy_run`).

## Архитектура

```
Telegram User
     │
     ▼
┌─────────┐    Task     ┌───────────┐    spawn      ┌──────────────────────────┐
│  Bot    │ ──────────► │ Director  │ ─────────────► │ временный субагент(ы):    │
│(aiogram)│ ◄────────── │  (Agent)  │ ◄───────────── │ роль + навыки, one-shot   │
└─────────┘   Result    └───────────┘    Result      └──────────────────────────┘
                          │      │                        │  собран из навыков
                          │      │ recall/remember         │  (docker / db / host)
                          │      ▼                         ▼
                          │  ┌──────────┐        ┌──────────────────────────┐
                          ▼  │ Память   │        │ Docker API / host shell  │
                   ┌──────────┐(факты,   │        │ (aiodocker, subprocess)  │
                   │ LLM API  ││ диалог)  │        └──────────────────────────┘
                   └──────────┘└──────────┘
```

Заранее заданных агентов нет. Получив задачу, Директор через `spawn` собирает
временных субагентов: роль одной фразой + набор навыков + задача. Загрузчик
навыков (`load_all_skills`) отдаёт Директору библиотеку навыков; из них он на лету
компонует `system_prompt` (роль + плейбуки навыков) и набор инструментов субагента.
Навык `memory` в библиотеку субагентов не попадает — память только у Директора.

**Поток подтверждения опасных операций:** Agent → ConfirmationGateway → Telegram inline-кнопки → пользователь → Agent (выполняет или отклоняет).

## Структура проекта

```
opencode_agents_system/
├── pyproject.toml              # зависимости + конфиг pytest
├── .env.example                # шаблон переменных окружения
├── Dockerfile
├── docker-compose.yml
├── app/
│   ├── main.py                 # сборка + startup/shutdown
│   ├── config.py               # pydantic-settings
│   ├── logging.py              # structlog
│   ├── llm/
│   │   └── client.py           # generic LLM client (один round-trip)
│   ├── tools/
│   │   ├── base.py             # Tool dataclass, Safety, schema gen
│   │   └── docker.py           # низкоуровневые docker/compose/shell функции
│   ├── skills/
│   │   ├── loader.py           # Skill, load_skill, load_all_skills
│   │   ├── docker/             # SKILL.md (плейбук) + tools.py (build_tools)
│   │   ├── db/                 # read-only запросы к БД
│   │   └── host/              # shell на хосте + firewall-плейбук
│   ├── agents/
│   │   ├── messages.py         # Task, Result, ConfirmationRequest
│   │   ├── registry.py         # AgentRegistry + ConfirmationGateway
│   │   ├── base.py             # Agent: цикл LLM↔tools
│   │   ├── director.py         # Director + spawn (сборка временных субагентов)
│   │   └── loader.py           # compose_prompt: роль + плейбуки навыков
│   └── bot/
│       ├── filters.py          # WhitelistFilter
│       ├── keyboards.py        # approve/reject inline keyboard
│       ├── gateway.py          # TelegramConfirmationGateway
│       ├── handlers.py         # message→Task, callback→confirm
│       └── bot.py              # Bot+Dispatcher assembly
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_llm_client.py
    ├── test_tools_base.py
    ├── test_tools_docker.py
    ├── test_messages.py
    ├── test_registry.py
    ├── test_agent_base.py
    ├── test_director.py
    ├── test_sysadmin.py
    ├── test_bot_filters.py
    ├── test_bot_gateway.py
    └── test_bot_handlers.py
```

## Добавление нового skill'а

```
app/skills/<skill_name>/
├── SKILL.md      # фронтматтер (name, description) + markdown-плейбук
└── tools.py      # build_tools() -> list[Tool]
```

```markdown
<!-- SKILL.md -->
---
name: myskill
description: что делает навык (идёт в список навыков Директору)
---

## Навык: ...
Инструкции/плейбук для агента, которому Директор выдаст этот навык.
```

```python
# tools.py
from app.tools.base import Tool, Safety

def build_tools() -> list[Tool]:
    return [Tool("my_tool", "...", MyParams, my_fn, Safety.SAFE)]
```

## Добавление нового агента

Не нужно. Агентов заранее не существует — есть только Директор и единый механизм
`spawn`. Чтобы расширить систему, добавьте **навык** (см. выше): при старте (или по
`/reload`) `load_all_skills` подхватит его, и Директор сможет спавнить агентов с этим
навыком под подходящие задачи. Правок кода и заведения агентов не требуется.

## Лицензия

MIT
