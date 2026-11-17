# Проверки решений этапа 1

Эти инструменты не запускают рабочий video API. Результаты описаны в
[verification/stage-1.md](../docs/verification/stage-1.md).

## PostgreSQL-очередь

Нужны PostgreSQL 17 и отдельная **новая пустая** база, имя заканчивается на `_probe`.
Для локальной БД Compose (из корня репозитория):

```bash
uv sync --locked --group experiments
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait db
docker compose exec -T db createdb -U video stage1_queue_probe
POSTGRES_DB=stage1_queue_probe uv run --locked --group experiments python experiments/queue_probe.py
# Только после завершения проверки:
docker compose exec -T db dropdb -U video stage1_queue_probe
```

Если пользователь БД изменён, укажите его вместо video. Скрипт не создаёт и не
удаляет БД самостоятельно, отказывается применять схему в непустой базе.
Он проверяет commit/rollback на одном соединении SQLAlchemy/psycopg, запускает
отдельные процессы worker и убивает один из них SIGKILL. Ожидаемый результат —
JSON с passed-проверками и двумя выполнениями прерванного задания.
Для повторного прогона нужна новая пустая база. Пароли берутся из штатной
конфигурации, не из аргументов команды. CI имеет отдельный job `queue-probe`.

## Образцы и браузер

Нужны FFmpeg/ffprobe с libx264 и Playwright 1.62.1, доступный Node.js, а также
системный Chromium. Генерация двухсекундных файлов выполняется с одним потоком;
артефакты сохраняются вне репозитория.

```bash
uv run --locked python experiments/make_media_samples.py --output /tmp/stage1-media
# Если Playwright не установлен в инструментах среды:
npm install --prefix /tmp/stage1-playwright --save-exact playwright@1.62.1
NODE_PATH=/tmp/stage1-playwright/node_modules node experiments/media_browser_probe.cjs /tmp/stage1-media
```

В среде со штатным Playwright достаточно `node experiments/media_browser_probe.cjs`.
Путь к браузеру переопределяется `CHROMIUM_EXECUTABLE`; по умолчанию /usr/bin/chromium.
`--no-sandbox` в этом изолированном headless-эксперименте не является настройкой
продуктового браузера. Проверяются кадры, ход воспроизведения, AAC при наличии
аудио и переход на 0.8 секунды с продолжением воспроизведения.

JSON ffprobe и browser.json сохраняются в каталоге образцов. Неподдерживаемый
10-bit образец идентифицируется по ffprobe и не объявляется совместимым.
Локальный HTTP-сервер эксперимента поддерживает Range для проверки образцов;
производственная раздача Nginx и приватные ACL будут проверяться на этапе 5.

Firefox/Safari не объявляются проверенными. Отклонённая сетью загрузка Firefox
не обходится настройками proxy/TLS; для повторения нужна разрешённая установка
браузера в окружении. Ограничения описаны в [политике видео](../docs/video-policy.md).

## Экран управления этапа 2

`studio_browser_probe.cjs` запускается только против приложения с отдельной
тестовой БД: создаёт два аккаунта, карточку и проверяет редактирование/grants.
Передача файла и доступ к готовому ролику этим сценарием не проверяются.
Команда и результаты: [проверки этапа 2](../docs/verification/stage-2.md).


## Загрузка: этап 3

Все три проверки выполняются только на отдельных тестовых данных. Для UI/HTTP
нужен отдельный Compose-контур с лимитом 4 MiB (чтобы быстро проверить обе стороны
границы). Пример с отдельными именованными volumes и портом:

```bash
# Образ сначала собирается штатным scripts/start.sh.
# TEST_POSTGRES_PASSWORD задайте отдельно (не пароль рабочей БД).
POSTGRES_PASSWORD="$TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage3_browser HTTP_PORT=8083 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=20971520 \
MEDIA_CAPACITY_BYTES=67108864 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=65536 \
docker compose -p stage3-verify -f compose.yaml up --no-build --detach --wait
node experiments/upload_browser_probe.cjs /tmp/stage1-media/h264-aac.mp4
uv run --locked python experiments/upload_transport_probe.py
```

UI-проверка использует Playwright/системный Chromium как выше. HTTP-проверка
передаёт синтетические 2 MiB, обрывает TCP и проверяет ранний 413 Nginx; это
проверка транспорта, не поддержки формата. Скрипты оставляют тестовые аккаунты,
карточки и очередь в отдельном контуре. Не направляйте их в production.

UI-проверка также проверяет перенос имени из 100 символов на ширинах
320/390/768/1440 px, блокировку метаданных после 202 и после потери ответа
с последующей сверкой статуса. Изменение данных на странице управления остаётся
доступным и сохраняется после reload; после ошибки передачи форма допускает повтор.

Проверка локального Swagger блокирует все внешние запросы браузера и выполняет
GET `/health/live` через кнопку Try it out. Подходит и для основного экземпляра:

```bash
DOCS_TEST_BASE_URL=http://127.0.0.1:8080 node experiments/docs_browser_probe.cjs
```

Для SIGKILL-пробы нужна мигрированная отдельная БД с именем, оканчивающимся
на `_probe`, свободный localhost:8013 и отдельный пустой MEDIA_ROOT:

```bash
POSTGRES_DB=stage3_upload_probe uv run --locked alembic upgrade head
POSTGRES_DB=stage3_upload_probe MEDIA_ROOT=/tmp/stage3-crash-media \
uv run --locked python experiments/upload_crash_probe.py
```

Подключение к этой БД берётся из обычных DB_HOST/DB_PORT/POSTGRES_*; создать
тестовую БД нужно заранее. Скрипт запускает собственный uvicorn, убивает именно
его SIGKILL в середине потока, сдвигает истёкшую аренду в тестовой БД, запускает
maintenance --once и проверяет сохранение ранее принятого исходника/задания
после restart. БД и тестовые файлы остаются для осмотра.
