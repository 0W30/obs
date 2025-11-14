# Sentry Error Collector

Сервис для сбора и управления ошибками из Sentry через webhook.

## 🚀 Быстрый старт

### Локальный запуск

```bash
# Установка зависимостей через uv
uv pip install -r requirements.txt

# Запуск сервера
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Запуск через Docker Compose

```bash
docker-compose up --build
```

## ⚙️ Конфигурация Sentry

### Переменные окружения

Создайте файл `.env` или установите переменные окружения:

```bash
# Обязательные (для фильтрации по проекту)
SENTRY_PROJECT=my-project          # Slug проекта в Sentry
SENTRY_ORGANIZATION=my-org         # Slug организации в Sentry

# Опциональные
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx  # DSN для отправки ошибок
SENTRY_FILTER_BY_PROJECT=false     # Фильтровать webhook по проекту (true/false)

# База данных
DATABASE_URL=sqlite+aiosqlite:///./data/errors.db
```

### Настройка в Docker Compose

В `docker-compose.yml` переменные уже настроены. Установите их через `.env` файл или экспортируйте:

```bash
export SENTRY_PROJECT=my-project
export SENTRY_ORGANIZATION=my-org
export SENTRY_FILTER_BY_PROJECT=true

docker-compose up
```

Или создайте `.env` файл в корне проекта:

```env
SENTRY_PROJECT=my-project
SENTRY_ORGANIZATION=my-org
SENTRY_FILTER_BY_PROJECT=false
```

### Фильтрация по проекту

Если `SENTRY_FILTER_BY_PROJECT=true`, сервис будет принимать webhook только от указанного проекта (`SENTRY_PROJECT`). Ошибки от других проектов будут отклоняться с кодом 403.

## 📡 API Endpoints

### Webhook Sentry
- **POST** `/sentry/webhook` - Принимает ошибки из Sentry

### Получение ошибок
- **GET** `/errors/latest` - Последняя ошибка
- **GET** `/errors` - Все ошибки

### Информация
- **GET** `/` - Информация о сервисе и конфигурации
- **GET** `/config` - Текущая конфигурация (без секретных данных)
- **GET** `/health` - Health check

## 🔗 Настройка Webhook в Sentry

1. Перейдите в настройки проекта Sentry
2. Settings → Integrations → Webhooks
3. Добавьте URL: `http://your-server:8000/sentry/webhook`
4. Выберите события для отправки (Issue Created, Issue Resolved и т.д.)

## 📝 Формат Webhook от Sentry

```json
{
  "event_id": "uuid",
  "project": "string",
  "message": "string",
  "timestamp": 1234567890,
  "exception": {
    "type": "ValueError",
    "value": "Something bad happened",
    "stacktrace": "stacktrace here..."
  }
}
```

## 🐳 Docker

### Сборка образа

```bash
docker build -t sentry-error-collector .
```

### Запуск контейнера

```bash
docker run -p 8000:8000 \
  -e SENTRY_PROJECT=my-project \
  -e SENTRY_ORGANIZATION=my-org \
  -e SENTRY_FILTER_BY_PROJECT=false \
  -v $(pwd)/data:/data \
  sentry-error-collector
```

## 📊 База данных

SQLite база данных сохраняется в директории `./data/errors.db` (локально) или `/data/errors.db` (в Docker).

Таблица `errors` создается автоматически при первом запуске.

## 🔒 Безопасность

- Webhook не требует аутентификации (можно добавить в будущем)
- При включенной фильтрации по проекту, только указанный проект может отправлять webhook
- DSN не отображается в API endpoints

