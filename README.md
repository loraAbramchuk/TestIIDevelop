# Видеоплатформа

Каркас Python 3.12 + FastAPI + PostgreSQL 17. Nginx принимает HTTP-запросы,
Alembic управляет схемой базы, Jinja2 отображает простую главную страницу.

Реализованы инфраструктура, первая миграция пользователей с ролями `user`,
`moderator`, `admin`, Swagger и проверки готовности. Регистрация, загрузка видео,
обработка и воспроизведение пока не реализованы.

Требования: [спецификация](docs/requirements.md), [сценарии приёмки](docs/acceptance.md).

## Локальный запуск

Нужны Docker Engine с Compose v2 (`--wait`) и Python 3.12 для разработки
(для генерации конфигурации достаточно Python 3 из стандартного пакета Ubuntu).
Из корня репозитория:

```bash
bash scripts/start.sh
```

Скрипт создаёт `.env` с уникальным случайным паролем БД и правами `0600`, если
файл ещё отсутствует. Затем собирает образ, запускает PostgreSQL, применяет
миграции отдельным контейнером и ожидает готовности приложения и Nginx.
Существующая конфигурация сохраняется. `.env` не коммитится.

На своей машине откройте порт 8080: главная `/`, Swagger `/docs`, схема
`/openapi.json`. Порт привязан только к loopback. Изменить порт можно через
`HTTP_PORT` в `.env`. БД и Python-приложение не публикуют порты наружу.

Это запуск каркаса с установленным Docker. Установщик зависимостей для чистой
Ubuntu, публичный HTTPS и обработчик видео будут добавлены в следующих этапах.

## Разработка без пересборки образа

Нужны Python 3.12 и [uv](https://docs.astral.sh/uv/). Из корня репозитория:

```bash
python3 scripts/init_env.py
uv sync --locked
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait db
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`compose.dev.yaml` открывает PostgreSQL только на `127.0.0.1:5432`.
Приложение читает `.env`, `DB_HOST` по умолчанию — `localhost`. Локальные файлы
хранятся в игнорируемом `.local/media`. Каталог видео не подключён как публичная
статика. Контейнерный вариант использует отдельный постоянный volume `media`.

В управляемой облачной среде каталог кэша uv может быть недоступен для записи:
используйте `UV_CACHE_DIR=/tmp/video-uv-cache` перед командами uv. Для сборки
через доверенный прокси `start.sh` автоматически использует `compose.cloud.yaml`,
если задан `CODEX_PROXY_CERT`. Можно также задать `BUILD_CA_BUNDLE` — путь к
доверенному объединённому CA bundle на машине сборки. Сертификат передаётся как
BuildKit secret; проверка TLS остаётся включённой.

## Проверки

```bash
uv run --locked ruff check app migrations tests scripts
uv run --locked ruff format --check app migrations tests scripts
uv run --locked pytest -m 'not integration'
# После запуска dev-БД и alembic upgrade head:
RUN_DB_TESTS=1 uv run --locked pytest
uv run --locked alembic check
```

Интеграционные тесты используют настоящий PostgreSQL и транзакции с откатом.
Запускайте их на БД разработки или отдельной тестовой БД, не в production.
Без `RUN_DB_TESTS=1` проверки PostgreSQL явно пропускаются.

`/health/live` проверяет HTTP-процесс. `/health/ready` возвращает 200, только если
доступна прикладная схема БД и удалось записать временный файл в хранилище;
иначе — 503 без раскрытия сведений о подключении. Обработчик видео пока не
существует, поэтому его готовность здесь не заявляется. Главная — пустое
состояние, не проверка содержимого видеокаталога.

## Миграции и зависимости

```bash
uv run --locked alembic revision --autogenerate -m 'describe change'
uv run --locked alembic upgrade head
```

Проверяйте сгенерированные миграции перед применением. Приложение не вызывает
`create_all` при запуске. Пользователи и пароли по умолчанию не создаются.
Модель пользователя — основа следующего этапа, не готовая авторизация.

`uv.lock` фиксирует все зависимости, `requirements.txt` — сгенерированный
runtime-экспорт с хешами для Docker. После изменения зависимостей:

```bash
uv lock
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

## Управление сервисами и данными

```bash
docker compose ps
docker compose logs --tail=100 app migrate web
docker compose stop
bash scripts/start.sh
```

Повторный запуск применяет только новые миграции. База и хранилище живут в
именованных volumes и сохраняются при пересоздании контейнеров.
Не используйте `docker compose down -v` для обычной остановки: это удалит данные.
Не меняйте пароль в `.env` уже созданной БД без отдельной смены пароля в PostgreSQL.
Резервное копирование и восстановление ещё не реализованы.

## Структура

```text
app/                 настройки, подключение к БД, API, шаблоны и статика
migrations/          версионируемая схема PostgreSQL
deploy/              конфигурация Nginx
scripts/             генерация локальной конфигурации и запуск
tests/               HTTP-проверки и интеграционные тесты PostgreSQL
docs/                требования и сценарии приёмки
```
