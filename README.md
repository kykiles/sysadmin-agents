# Агентская система системного администрирования v1

Серверная мульти-агентная система (Директор + Главный сисадмин), управляемая через Telegram одним пользователем. Управляет Docker-контейнерами и compose-проектами на хосте.

## Возможности

- **Директор** — принимает задачи на естественном языке, планирует и делегирует специалистам
- **Главный сисадмин** — управляет Docker: просмотр контейнеров, логов, статистики, инспекция, перезапуск/остановка/запуск, `docker exec`, `compose up/down`
- **Безопасные операции** (чтение: `ps`, `logs`, `stats`, `inspect`) — выполняются автоматически
- **Опасные операции** (мутации: `restart`, `stop`, `start`, `exec`, `compose up/down`) — требуют подтверждения через inline-кнопки в Telegram (таймаут 5 минут, авто-отклонение)
- **Масштабируемость** — добавление нового специалиста = класс-агент + регистрация в реестре

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

## Архитектура

```
Telegram User
     │
     ▼
┌─────────┐    Task     ┌───────────┐  delegate_to  ┌───────────────┐
│  Bot    │ ──────────► │ Director  │ ─────────────► │ ChiefSysadmin │
│(aiogram)│ ◄────────── │  (Agent)  │ ◄───────────── │   (Agent)     │
└─────────┘   Result    └───────────┘    Result      └───────────────┘
                                                           │
                              ┌────────────────────────────┤
                              │                        Docker API
                              ▼                        (aiodocker)
                       ┌─────────────┐              ┌─────────────┐
                       │  LLM API    │              │  Docker     │
                       │ (DeepSeek)  │              │  Engine     │
                       └─────────────┘              └─────────────┘
```

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
│   │   └── docker.py           # docker + compose tools
│   ├── agents/
│   │   ├── messages.py         # Task, Result, ConfirmationRequest
│   │   ├── registry.py         # AgentRegistry + ConfirmationGateway
│   │   ├── base.py             # Agent: цикл LLM↔tools
│   │   ├── director.py         # Director + delegate_to
│   │   └── sysadmin.py         # ChiefSysadmin + docker tools
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

## Добавление нового специалиста

```python
# app/agents/new_role.py
from app.agents.base import Agent
from app.tools.base import Tool

TOOLS = [...]

class NewSpecialist(Agent):
    def __init__(self, llm, registry):
        super().__init__(
            name="new_specialist",
            system_prompt="Ты — специалист по ...",
            tools=TOOLS,
            llm=llm,
            registry=registry,
        )
```

Затем зарегистрировать в `app/main.py` и добавить в `available_agents` Директора.

## Лицензия

MIT
