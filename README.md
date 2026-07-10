# Агентская система системного администрирования v1

Серверная мульти-агентная система (Директор + доменные специалисты), управляемая через Telegram одним пользователем. Управляет Docker-контейнерами, compose-проектами, базами данных и хостом.

## Возможности

- **Директор** — принимает задачи на естественном языке, планирует и делегирует специалистам
- **Доменные специалисты** — собираются декларативно из **skill'ов**:
  - `dockeradmin` (skill `docker`) — контейнеры и compose: просмотр, логи, статистика, инспекция, перезапуск/остановка/запуск, `docker exec`, `compose up/down`
  - `dbadmin` (skill `db`) — read-only запросы к БД в контейнерах (psql, mysql, sqlite3)
  - `hostadmin` (skill `host`) — команды на хосте: firewall/iptables, systemd, диски
- **Skill** — самодостаточный пакет: плейбук-инструкции в `SKILL.md` + инструменты в `tools.py`
- **Безопасные операции** (чтение: `ps`, `logs`, `stats`, `inspect`, `docker_query`) — выполняются автоматически
- **Опасные операции** (мутации: `restart`, `stop`, `start`, `exec`, `compose up/down`, `shell_exec`) — требуют подтверждения через inline-кнопки в Telegram (таймаут 5 минут, авто-отклонение)
- **Масштабируемость** — новый агент = один markdown-файл (роль + список skill'ов), без правок кода

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

**Диалоговая память.** Директор помнит последние `DIALOG_HISTORY_LIMIT` реплик диалога (хранятся в SQLite `/data/dialog.db`, переживают рестарт контейнера), поэтому можно ссылаться на предыдущий контекст: «а теперь перезапусти его». Команда `/reset` очищает историю и начинает диалог заново. Специалисты остаются без памяти — каждая делегированная задача самодостаточна.

## Архитектура

```
Telegram User
     │
     ▼
┌─────────┐    Task     ┌───────────┐  delegate_to  ┌──────────────────────────┐
│  Bot    │ ──────────► │ Director  │ ─────────────► │ dockeradmin / dbadmin /  │
│(aiogram)│ ◄────────── │  (Agent)  │ ◄───────────── │ hostadmin  (Agents)      │
└─────────┘   Result    └───────────┘    Result      └──────────────────────────┘
                                                           │  composed from skills
                              ┌────────────────────────────┤  (docker / db / host)
                              │                             ▼
                              ▼                   ┌──────────────────────────┐
                       ┌─────────────┐            │ Docker API / host shell  │
                       │  LLM API    │            │ (aiodocker, subprocess)  │
                       └─────────────┘            └──────────────────────────┘
```

Каждый агент декларируется markdown-файлом (`app/agents/defs/<name>.md`): роль +
список skill'ов. При старте загрузчики собирают `system_prompt` (роль + плейбуки
skill'ов) и набор инструментов; `available_agents` Директора формируется автоматически.

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
│   │   ├── director.py         # Director + delegate_to
│   │   ├── loader.py           # load_agents: сборка агентов из defs + skills
│   │   └── defs/               # dockeradmin.md, dbadmin.md, hostadmin.md
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
description: что делает навык (идёт в подсказку Директору через агента)
---

## Навык: ...
Инструкции/плейбук для агента, владеющего этим навыком.
```

```python
# tools.py
from app.tools.base import Tool, Safety

def build_tools() -> list[Tool]:
    return [Tool("my_tool", "...", MyParams, my_fn, Safety.SAFE)]
```

## Добавление нового агента

Создать один markdown-файл — правки кода не нужны:

```markdown
<!-- app/agents/defs/myadmin.md -->
---
name: myadmin
description: чем занимается специалист (попадёт в available_agents Директора)
skills:
  - myskill
---

Ты — специалист по ... Получаешь подзадачи от Директора и возвращаешь отчёт.
```

При старте `load_all_skills` и `load_agents` подхватят skill и агента
автоматически, а Директор увидит его в списке специалистов.

## Лицензия

MIT
