# Эксперименты и сквозные проверки

Раздел этапа 1 проверяет отдельные решения; следующие разделы запускают
прикладные сценарии на изолированных данных. Результаты этапа 1 описаны в
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
docker compose -p stage3-verify -f compose.yaml up --no-build --detach --wait db migrate app upload-maintenance web
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


## Обработка: этап 4

Для проверки загрузки до ready/failed используйте новый отдельный Compose-контур
с работающим worker. Скрипт создаёт аккаунт, загружает настоящий H.264/AAC,
проверяет ready и закрытость приватных метаданных, затем 10-bit отказ и повтор
совместимого файла в ту же карточку. Плеер/раздача в этот этап не входят.

```bash
# После сборки актуального образа штатным scripts/start.sh:
POSTGRES_PASSWORD="$TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage4_browser HTTP_PORT=8086 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=33554432 \
MEDIA_CAPACITY_BYTES=268435456 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
docker compose -p stage4-verify -f compose.yaml up --no-build --detach --wait
node experiments/processing_browser_probe.cjs /tmp/stage1-media
```

Проверка статуса теперь ждёт автоматического обновления до ready/failed,
без нажатия кнопки. Для регрессий короткого ролика, временных меток и открытой
карточки есть дополнительная проверка. Она **останавливает и запускает только
явно указанный тестовый worker**; допускаются stage4-verify-worker-1 и
stage4-fixes-worker-1. Остальные контейнеры не затрагиваются:

```bash
PROCESSING_TEST_BASE_URL=http://127.0.0.1:8086 \
PROCESSING_TEST_WORKER_CONTAINER=stage4-verify-worker-1 \
node experiments/processing_status_browser_probe.cjs /tmp/stage1-media
```

Сценарий проверяет ready/failed на двух одновременно открытых страницах,
сохранение несохранённых метаданных, появление формы повтора без reload,
остановку опроса после трёх сетевых ошибок и возобновление кнопкой. Он также
генерирует однокадровое MP4 и MP4 со сдвигом timestamp на 5 секунд и обрабатывает
их настоящим контейнерным FFmpeg. Требуются локальные Docker CLI, FFmpeg и
Playwright. Отчёт — /tmp/stage4-fixes-browser.json.

Прежний upload_browser_probe.cjs рассчитан на остановленный worker и ожидает
processing. Если контур уже существовал с worker, сначала остановите **только
его** `docker compose -p stage3-verify stop worker`. Основной контур не затрагивайте.

SIGKILL-проверка требует отдельную пустую мигрированную БД с суффиксом `_probe`,
свежий отдельный MEDIA_ROOT и образцы из make_media_samples.py в /tmp/stage1-media:

```bash
POSTGRES_DB=stage4_crash_probe uv run --locked alembic upgrade head
POSTGRES_DB=stage4_crash_probe MEDIA_ROOT=/tmp/stage4-crash-media \
PROCESSING_LEASE_SECONDS=4 PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 \
uv run --locked python experiments/processing_crash_probe.py
```

DB_HOST/DB_PORT/POSTGRES_* задаются для этой тестовой БД. Скрипт убивает только
собственный worker в момент реального FFmpeg/ffprobe, проверяет смерть child и
освобождение блокировки, дожидается настоящего срока аренды, выполняет maintenance
--once, запускает новый worker и проверяет ready, исходник и итоговый ack.
Тестовые БД/файлы остаются для осмотра. Результаты: [этап 4](../docs/verification/stage-4.md).

## Защищённый просмотр и удаление: этап 5

Нужен свежий отдельный Compose-контур `stage5-verify` на localhost:8089,
БД stage5_verify и собранные app/web. Установлены FFmpeg, Python-зависимости
разработки, Playwright и Chromium, как в предыдущих разделах. Пароль тестовой БД
задайте отдельно в STAGE5_TEST_POSTGRES_PASSWORD. Не используйте рабочую БД.

```bash
docker compose build app web
POSTGRES_PASSWORD="$STAGE5_TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage5_verify HTTP_PORT=8089 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=67108864 \
MEDIA_CAPACITY_BYTES=536870912 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
UPLOAD_CLEANUP_INTERVAL_SECONDS=2 PROCESSING_LEASE_SECONDS=6 \
PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 \
docker compose -p stage5-verify -f compose.yaml up --no-build --detach --wait

uv run --locked python experiments/playback_http_probe.py --project stage5-verify
PLAYBACK_TEST_BASE_URL=http://127.0.0.1:8089 node experiments/playback_browser_probe.cjs
DOCS_TEST_BASE_URL=http://127.0.0.1:8089 node experiments/docs_browser_probe.cjs
```

HTTP-проверка требует Docker CLI: останавливает только worker/maintenance и
перезапускает app/web этого тестового проекта. Она проверяет точные байты Range,
HEAD, 416, ACL, отзыв и смену приватности, сохранность после restart, удаление
во время настоящего FFmpeg и восстановление очистки после запуска maintenance.
Используется локальный psql внутри stage5-verify-db-1 для проверки служебного
журнала и квот; схема/секреты основной БД не затрагиваются. Worker/maintenance
восстанавливаются в finally. Отчёт — /tmp/stage5-http.json.

Браузерный сценарий проверяет загрузку, появление ссылки просмотра, защищённый
JPEG, native MP4-плеер, декодированные кадры, ход времени и перемотку, grants,
отзыв, public/private и подтверждение удаления. Оба сценария создают по два
аккаунта; для повторных прогонов учитывайте лимиты регистрации тестовой БД.
JSON и снимки — /tmp/stage5-browser.json, /tmp/stage5-watch-{desktop,mobile}.png.
Используется маленький двухсекундный MP4; HTTP-удаление проверяется также на
100-секундном клипе. Нагрузка и максимальные файлы здесь не проверяются.

Отдельный прогон регрессий восстановления загрузки и сохранности черновиков
в карточке (на свежем stage5-verify, с теми же настройками выше):

```bash
RECOVERY_TEST_PROJECT=stage5-verify node experiments/upload_recovery_browser_probe.cjs
```

Сценарий создаёт три аккаунта, останавливает/запускает только тестовый worker и
держит два настоящих receiving PUT, чтобы получить 429. Проверяет потерю ответа
после реального 202, автоопрос до ready, сохранение причины отказа, повтор той
же карточки, grant/revoke без reload и без потери названия/описания/видимости.
Также проверяются идемпотентный grant, limit/cursor, ручное восстановление списка
после имитированного 503 и сообщение имитированного 507 без заполнения диска.
Нужны Docker CLI/psql в тестовом db, Playwright/Chromium и образец H.264/AAC.
В finally тестовый worker запускается, открытые запросы закрываются.
Три аккаунта входят в общий лимит регистрации вместе с основными проверками;
для отдельного прогона нужен свежий тестовый контур.
Артефакты: /tmp/stage5-recovery.json, /tmp/stage5-recovery-grants.png.

## Каталог, каналы и подписки: этап 6

Нужен свежий отдельный проект **stage6-verify**, порт **8091**, БД stage6_verify.
Сценарий отказывается запускаться без CATALOG_TEST_PROJECT=stage6-verify.
Заранее соберите актуальные образы app/web и создайте образцы командой выше.
Пароль отдельной БД задайте в STAGE6_TEST_POSTGRES_PASSWORD.

```bash
docker compose build app web
POSTGRES_PASSWORD="$STAGE6_TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage6_verify HTTP_PORT=8091 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=67108864 \
MEDIA_CAPACITY_BYTES=536870912 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
UPLOAD_CLEANUP_INTERVAL_SECONDS=2 PROCESSING_LEASE_SECONDS=6 \
PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 \
docker compose -p stage6-verify -f compose.yaml up --no-build --detach --wait

CATALOG_TEST_PROJECT=stage6-verify node experiments/catalog_browser_probe.cjs
AUTH_TEST_PROJECT=stage6-verify node experiments/auth_recovery_browser_probe.cjs
PLAYBACK_TEST_BASE_URL=http://127.0.0.1:8091 node experiments/playback_browser_probe.cjs
DOCS_TEST_BASE_URL=http://127.0.0.1:8091 node experiments/docs_browser_probe.cjs
```

Нужны Playwright/Chromium, FFmpeg/ffprobe, Node.js и Docker CLI. Каталог создаёт
два аккаунта и настоящие видео, одно тестовое hidden-состояние задаёт SQL внутри
тестового db. Проверяются публичная лента, native-воспроизведение из карточки,
превью, поиск с literal-символами и кириллицей, пагинация, редактирование канала
с сохранением ввода при 503, подписки, отсутствие доступа по подписке и grant/revoke.
Проверяются ширины 320/390/768/1440 px. Совместно с auth recovery и playback
создаются пять аккаунтов — в пределах лимита регистрации 5/IP/час. Для повторов нужен новый тестовый
контур или истечение окна лимита; основную БД для обхода лимитов не изменяйте.
Артефакты: /tmp/stage6-browser.json, /tmp/stage6-home-{desktop,mobile}.png,
/tmp/stage6-channel-mobile.png. Playback сохраняет /tmp/stage5-browser.json;
docs печатает JSON в stdout и сохраняет /tmp/swagger-local.png.

Auth recovery использует один новый аккаунт и проверяет HTML/пустые ошибки от
CSRF/регистрации/входа, JSON null, отсутствие CSRF-токена, обрыв соединения и 429.
После ошибки ввод сохраняется, кнопка снова доступна; реальная повторная попытка
завершается возвращением к каналу после регистрации и входа. Проверяется перенос
`next` между экранами и отклонение внешних адресов возврата, а также 422 для NUL
и непарных суррогатов в профиле при сохранении корректных emoji.
Если каталог пуст, сценарий использует `/my-channel` как исходную защищённую страницу.
Артефакты: /tmp/stage6-auth-recovery.json и /tmp/stage6-auth-friendly-error.png.

Проверка миграции требует отдельной **пустой** БД с суффиксом `_probe` и
Python-зависимостей разработки. Создайте её заранее в dev PostgreSQL:

```bash
docker compose exec -T db createdb -U video stage6_catalog_probe
POSTGRES_DB=stage6_catalog_probe uv run --locked python experiments/catalog_migration_probe.py
```

Подключение берётся из штатных DB_HOST/DB_PORT/POSTGRES_*; для контейнерной БД
без открытого порта подключите compose.dev.yaml, как в разделе разработки README.
Скрипт отказывается работать с непустой БД. Создаёт схему 0006 и старые аккаунты,
сессии, видео, grant, загрузку и задание с настоящими файлами; проверяет upgrade,
повторный upgrade, запросы со старой сессией, downgrade/re-upgrade и alembic check.
Результат JSON печатается в stdout; временные файлы и БД остаются для осмотра.
Downgrade проверяется только в эксперименте: он удаляет профили каналов и подписки.
