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
# Sentry DSN (опционально, для отправки ошибок в Sentry)
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx

# База данных
DATABASE_URL=sqlite+aiosqlite:///./data/errors.db
```

### Настройка в Docker Compose

В `docker-compose.yml` переменные уже настроены. Установите их через `.env` файл или экспортируйте:

```bash
export SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx

docker-compose up
```

Или создайте `.env` файл в корне проекта:

```env
SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx
```

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

Реальный формат webhook от Sentry имеет следующую структуру:

```json
{
  "action": "created",
  "installation": {
    "uuid": "...",
    "status": "installed"
  },
  "data": {
    "issue": {
      "id": "123456",
      "shortId": "ABC-1",
      "title": "Error message",
      "culprit": "file.py in function",
      "permalink": "https://sentry.io/...",
      "level": "error",
      "status": "unresolved",
      "project": {
        "id": "123",
        "name": "My Project",
        "slug": "my-project"
      }
    },
    "event": {
      "event_id": "abc123...",
      "message": "Error message",
      "title": "Error title",
      "platform": "python",
      "timestamp": 1234567890.123,
      "level": "error",
      "logger": "root",
      "exceptions": [
        {
          "type": "ValueError",
          "value": "Something bad happened",
          "mechanism": {...}
        }
      ],
      "stacktrace": {
        "frames": [
          {
            "filename": "file.py",
            "function": "function_name",
            "lineno": 42,
            "abs_path": "/path/to/file.py"
          }
        ]
      }
    },
    "project": {
      "id": "123",
      "name": "My Project",
      "slug": "my-project"
    }
  },
  "actor": {
    "type": "user",
    "id": "123",
    "name": "User Name"
  }
}
```

**Важно:** Сервис обрабатывает только webhook с `action: "created"` (новые ошибки). Другие действия (resolved, assigned и т.д.) игнорируются.

## 🐳 Docker

### Сборка образа

```bash
docker build -t sentry-error-collector .
```

### Запуск контейнера

```bash
docker run -p 8000:8000 \
  -e SENTRY_DSN=https://xxx@xxx.ingest.sentry.io/xxx \
  -v $(pwd)/data:/data \
  sentry-error-collector
```

## 📊 База данных

SQLite база данных сохраняется в директории `./data/errors.db` (локально) или `/data/errors.db` (в Docker).

Таблица `errors` создается автоматически при первом запуске.

## 🔒 Безопасность

- Webhook не требует аутентификации (можно добавить в будущем)
- DSN не отображается в API endpoints

