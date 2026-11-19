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

## Лайки и комментарии: этап 7

Нужен свежий изолированный проект `stage7-verify` на localhost:8092,
БД stage7_verify и актуальные app/web. Playwright/Chromium, Node.js, FFmpeg и
Docker CLI устанавливаются как в предыдущих разделах. Пароль отдельной тестовой
БД задайте в STAGE7_TEST_POSTGRES_PASSWORD, рабочую БД не используйте.

```bash
docker compose build app web
POSTGRES_PASSWORD="$STAGE7_TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage7_verify HTTP_PORT=8092 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=67108864 \
MEDIA_CAPACITY_BYTES=536870912 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
UPLOAD_CLEANUP_INTERVAL_SECONDS=2 PROCESSING_LEASE_SECONDS=6 \
PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 \
docker compose -p stage7-verify -f compose.yaml up --no-build --detach --wait

INTERACTIONS_TEST_PROJECT=stage7-verify node experiments/interactions_browser_probe.cjs
PLAYBACK_TEST_BASE_URL=http://127.0.0.1:8092 node experiments/playback_browser_probe.cjs
DOCS_TEST_BASE_URL=http://127.0.0.1:8092 node experiments/docs_browser_probe.cjs
```

Interactions создаёт два аккаунта, загружает настоящий H.264/AAC до ready и
проверяет плеер, гостевые счётчики, лайк/отмену, комментарии, XSS как текст,
чужое удаление и исчезновение комментария в другой вкладке. Проверяются
сохранение черновика, ошибки 503/429, потерянный ответ после настоящего POST,
повтор загрузки страницы комментариев и отсутствие дублей, private/grant/revoke
с очисткой обсуждения после отказа. Для пагинации тест создаёт 22 комментария
через SQL только в stage7-verify-db-1, включая строку длиной 2000 символов.
Вёрстка проверяется на 320/390/768/1440 px. Совместно с playback создаются четыре
аккаунта; для повторов учитывайте лимит регистрации 5/IP/час.

Артефакты: /tmp/stage7-interactions.json, /tmp/stage7-watch-{desktop,mobile}.png.
Playback и docs используют прежние имена артефактов, описанные выше.

Миграционная проверка использует тот же инструмент с baseline 0007.
Создайте отдельную **пустую** БД с суффиксом `_probe`:

```bash
docker compose exec -T db createdb -U video stage7_interactions_probe
POSTGRES_DB=stage7_interactions_probe uv run --locked python experiments/catalog_migration_probe.py --baseline 0007
```

Скрипт проверяет upgrade до head, повторный upgrade, downgrade до 0007 и upgrade
снова. Сравниваются пользователи/роли/сессии, профили и UUID каналов, подписки,
видео, grants, очередь/загрузки и хеши настоящих файлов. Старая сессия получает
доступ к новым лайкам/комментариям только при наличии разрешения на видео.
Отчёт: /tmp/stage7-migration.json. Downgrade удаляет взаимодействия — это
проверка сохранения прежних данных, не безопасный откат без резервной копии.

## Плейлисты: этап 8

Нужен свежий отдельный проект `stage8-verify` на localhost:8093, БД stage8_verify,
актуальные app/web, FFmpeg/ffprobe, Playwright/Chromium, Node.js и Docker CLI.
Пароль отдельной БД задайте в STAGE8_TEST_POSTGRES_PASSWORD.

```bash
docker compose build app web
POSTGRES_PASSWORD="$STAGE8_TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage8_verify HTTP_PORT=8093 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=67108864 \
MEDIA_CAPACITY_BYTES=536870912 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
UPLOAD_CLEANUP_INTERVAL_SECONDS=2 PROCESSING_LEASE_SECONDS=6 \
PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 \
docker compose -p stage8-verify -f compose.yaml up --no-build --detach --wait

PLAYLIST_TEST_PROJECT=stage8-verify node experiments/playlists_browser_probe.cjs
DOCS_TEST_BASE_URL=http://127.0.0.1:8093 node experiments/docs_browser_probe.cjs
```

Сценарий отказывается работать без выбора тестового проекта. Создаёт два аккаунта
и три настоящих видео через upload/worker. Проверяет создание/переименование/
удаление плейлиста, безопасный вывод названия, idempotent add/remove, просмотр
MP4 из списка, права владельца и добавление приватного видео только после grant.
Проверяет перестановку с limit=1, догрузку якоря, 503 и повтор страницы, 409 после
изменения порядка в другой вкладке, сохранение черновиков, список на канале,
отзыв grant, hidden/deleted и сохранность файла после удаления плейлиста.
Hidden задаётся SQL только внутри stage8-verify-db-1. Проверяются 320/390/768/1440 px
и название из 200 символов без пробелов. Запускать только на изолированных данных.

Артефакты: /tmp/stage8-browser.json, /tmp/stage8-playlist-{desktop,mobile}.png.
Повтор создаёт ещё два аккаунта; учитывайте лимит регистрации 5/IP/час.

Для проверки миграции создайте отдельную **пустую** БД с суффиксом `_probe`:

```bash
docker compose exec -T db createdb -U video stage8_playlists_probe
POSTGRES_DB=stage8_playlists_probe uv run --locked python experiments/catalog_migration_probe.py --baseline 0008
```

Инструмент создаёт данные этапа 7, выполняет upgrade/repeat upgrade,
downgrade до 0008 и upgrade снова. Сравнивает пользователей/роли, сессии,
UUID/профили каналов, подписки, видео/grants, очередь/загрузки, лайки, комментарии
и хеши реальных файлов. Старая сессия работает с новым плейлистом без расширения
прав на его приватные элементы. Отчёт: /tmp/stage8-migration.json.
Downgrade удаляет только новые плейлисты, но для сохранения их данных нужен backup.

### Восстановления интерфейса после ревью этапа 8

Для `playlist_recovery_browser_probe.cjs` нужен свежий отдельный проект
`stage8-fixes` на localhost:8094, БД stage8_fixes и лимит **один элемент**.
Соберите актуальные app/web; пароль отдельной БД задайте в STAGE8_FIXES_DB_PASSWORD.

```bash
POSTGRES_PASSWORD="$STAGE8_FIXES_DB_PASSWORD" POSTGRES_DB=stage8_fixes HTTP_PORT=8094 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=67108864 \
MEDIA_CAPACITY_BYTES=536870912 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
UPLOAD_CLEANUP_INTERVAL_SECONDS=2 PROCESSING_LEASE_SECONDS=6 \
PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 PLAYLIST_MAX_ITEMS=1 \
docker compose -p stage8-fixes -f compose.yaml up --no-build --detach --wait

PLAYLIST_RECOVERY_TEST_PROJECT=stage8-fixes node experiments/playlist_recovery_browser_probe.cjs
```

Сценарий создаёт один аккаунт и два настоящих видео. Проверяет чистое/изменённое
поле названия после переименования в другой вкладке, подтверждённые записи при
последующем GET 503, ручное перечитывание без повторного PUT, удаление списка
в другой вкладке и отключение управления после 404. Для воспроизведения полного
плейлиста недоступное видео скрывается SQL только внутри stage8-fixes-db-1.
Проверяются отмена подтверждения очистки, явная очистка без раскрытия состава,
освобождение лимита и сохранность самого видео. Некорректный Unicode в пароле
проверяется через реальные HTTP auth endpoints: 422 без отражения пароля.

Нужны те же Playwright/Chromium, Node.js, Docker CLI и образец
/tmp/stage1-media/h264-aac.mp4. Повтор создаёт ещё один аккаунт; учитывайте лимит
регистрации тестовой БД. Артефакты: /tmp/stage8-fixes-recovery.json,
/tmp/stage8-fixes-confirmed-write.png, /tmp/stage8-fixes-cleanup-mobile.png.

## Жалобы и модерация: этап 9

Используйте свежий изолированный проект `stage9-verify` на localhost:8095,
БД `stage9_verify`, актуальные app/web, Playwright/Chromium, Node.js, Docker CLI
и образцы `/tmp/stage1-media/h264-aac.mp4` из проверки pipeline.
Пароль отдельной БД задайте в STAGE9_TEST_POSTGRES_PASSWORD.

```bash
docker compose build app web
POSTGRES_PASSWORD="$STAGE9_TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage9_verify HTTP_PORT=8095 \
MAX_UPLOAD_BYTES=4194304 OWNER_MEDIA_QUOTA_BYTES=67108864 \
MEDIA_CAPACITY_BYTES=536870912 MEDIA_MIN_FREE_BYTES=0 PREVIEW_BUDGET_BYTES=262144 \
UPLOAD_CLEANUP_INTERVAL_SECONDS=2 PROCESSING_LEASE_SECONDS=6 \
PROCESSING_HEARTBEAT_SECONDS=1 PROCESSING_RETRY_SECONDS=1 \
docker compose -p stage9-verify -f compose.yaml up --no-build --detach --wait

MODERATION_TEST_PROJECT=stage9-verify node experiments/moderation_browser_probe.cjs
DOCS_TEST_BASE_URL=http://127.0.0.1:8095 node experiments/docs_browser_probe.cjs
PLAYBACK_TEST_BASE_URL=http://127.0.0.1:8095 node experiments/playback_browser_probe.cjs
```

Основной сценарий создаёт три настоящих аккаунта и два ролика через UI/worker.
Отказывается работать без MODERATION_TEST_PROJECT=stage9-verify и loopback URL.
Назначает и отзывает роль SQL **только** в `stage9-verify-db-1`/`stage9_verify`;
скрытие, восстановление, жалобы и решения выполняет через рабочие UI/API.
Проверяет запрет приватного просмотра/названия для сотрудника без grant, повтор
открытой жалобы, 503 с сохранением черновика, закрытие/фильтр/пагинацию, причины
и исполнителя в аудите, hidden на всех ролях/поверхностях, owner status/metadata,
сохранение приватности, отзыв роли в открытом экране, HTML как текст и 320–1440 px.
Отдельный playback-сценарий создаёт ещё два аккаунта: суммарно пять, в пределах
лимита регистрации 5/IP/час. Повтор требует свежих изолированных данных или
сброса лимита **только в тестовой базе**. Production не используйте.

Артефакты: `/tmp/stage9-browser.json`, `/tmp/stage9-moderation-{390,1440}.png`, `/tmp/stage9-owner-{390,1440}.png`;
Swagger выводит JSON в stdout и `/tmp/swagger-local.png`; регрессия плеера —
`/tmp/stage5-browser.json` и `/tmp/stage5-watch-{desktop,mobile}.png`.

Миграционная проба требует пустую отдельную БД с суффиксом `_probe`:

```bash
docker compose exec -T db createdb -U video stage9_moderation_probe
POSTGRES_DB=stage9_moderation_probe uv run --locked python experiments/catalog_migration_probe.py --baseline 0009
```

Сравнивает данные этапа 8 до/после upgrade, повторного upgrade, downgrade и
re-upgrade. Включает роли/сессии/каналы/подписки/видео/grants/очередь/uploads,
лайки/комментарии, плейлисты/revision/порядок и хеши файлов. Старая moderator
сессия создаёт жалобу, скрывает/восстанавливает и закрывает дело с тремя событиями
аудита. Проверяет private ACL, alembic check и пустые reports/audit после
re-upgrade. Отчёт: `/tmp/stage9-migration.json`. Downgrade теряет жалобы/историю,
но сохраняет поля модерации videos; для сохранения данных необходим backup.

После проверки остановите только тестовый проект, сохранив volumes:

```bash
POSTGRES_PASSWORD="$STAGE9_TEST_POSTGRES_PASSWORD" POSTGRES_DB=stage9_verify HTTP_PORT=8095 \
docker compose -p stage9-verify -f compose.yaml stop
```


### Восстановление модерации после повторной проверки

`moderation_browser_probe.cjs` также вызывает
`moderation_recovery_browser_probe.cjs` на тех же изолированных аккаунтах и
публичном видео, до отзыва роли. Дополнительные регистрации не нужны. Модуль
проверяет malformed/null/array/неполные 200, отсутствие changed и неверный
тип/статус, потерю ответа после реального commit, повтор без дубля, no-op из
другой вкладки без ложного аудита, применение актуальной private ACL после
решения, report_closed с неуспешным GET и ручным восстановлением без записи,
сохранение длинного HTML-подобного черновика, video_deleted и общий API helper
на плейлисте/лайке/комментарии/204. Артефакт:
`/tmp/stage9-recovery-conflict-390.png`; результаты входят в
`/tmp/stage9-browser.json`. Модуль завершает проверку удалением тестового
публичного видео, поэтому использовать его можно только в изолированном сценарии.

## Администрирование: этап 10

Браузерная проба использует отдельную БД `stage10_browser` и приложение на
**localhost:8097**, Playwright/Chromium и `.venv`. Она отказывается работать на
другом URL или без POSTGRES_DB=stage10_browser или stage10_fixes_browser. Пример с отдельной dev-БД:

```bash
# На отдельном экземпляре PostgreSQL, не в production:
docker compose exec -T db createdb -U video stage10_browser
POSTGRES_DB=stage10_browser uv run --locked alembic upgrade head
POSTGRES_DB=stage10_browser MEDIA_ROOT=/tmp/stage10-browser-media \
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8097
# В другом терминале с теми же DB_HOST/DB_PORT/POSTGRES_USER/POSTGRES_PASSWORD:
POSTGRES_DB=stage10_browser node experiments/administration_browser_probe.cjs
DOCS_TEST_BASE_URL=http://127.0.0.1:8097 node experiments/docs_browser_probe.cjs
```

CLI bootstrap вызывается настоящим кодом с паролем через stdin тестового адаптера,
без пароля в аргументах или выводе. Далее роли меняются только рабочими API/UI.
Проба создаёт admin, двух пользователей и синтетические private/hidden объекты
для проверки отказа доступа. Проверяет поиск/пагинацию, роль текущей сессии,
последнего admin, self-demotion, stale role и потерю прав в открытой панели,
malformed/потерянный ответ после commit/GET 503, сохранение текста, закрытый аудит,
XSS и 320–1440 px. Требует свежую БД: повтор добавляет ещё bootstrap admin,
и проверка единственного администратора уже не будет корректной.
Артефакты: `/tmp/stage10-browser.json`, `/tmp/stage10-users-390.png`.

Миграция проверяется только на пустой выделенной БД с суффиксом `_probe`:

```bash
docker compose exec -T db createdb -U video stage10_administration_probe
POSTGRES_DB=stage10_administration_probe uv run --locked python experiments/administration_migration_probe.py
```

Проба создаёт прежние users/roles/password_hash/sessions/channels/videos/grants,
плейлист/элемент, лайк/комментарий, жалобу и событие аудита на схеме 0010.
Сравнивает все строки public/video_queue и хеши файлов после upgrade, repeat,
downgrade/re-upgrade. Старая admin session использует новый список, private
остаётся закрытым гостю, обе истории разделены. Отчёт `/tmp/stage10-migration.json`.
Downgrade удаляет только два индекса; прямой возврат старого кода требует
сохранить ограничения доступа к истории ролей.


### Восстановление администрирования после ревью

Основной `administration_browser_probe.cjs` вызывает
`administration_recovery_browser_probe.cjs` на тех же аккаунтах. Для повторения
можно использовать свежую БД `stage10_fixes_browser` вместо `stage10_browser`
(одинаковая настройка сервера и клиента, порт строго 8097). Дополнительные
регистрации не нужны. Модуль проверяет сходимость выбранной роли и последующее
обновление из другой вкладки, сохранение настоящего черновика, реальный CSRF
403 с прежней ролью admin, ошибки проверки сессии и ручное восстановление.
Он действительно ожидает четыре 15-секундных таймаута: PATCH до выполнения,
PATCH после commit с потерянным ответом, GET карточки, GET csrf. Ещё один таймаут
проверяет локальный HTTP-сервер с полученными заголовками и незавершённым JSON.
Сервер открывает случайный loopback-порт только на время проверки и закрывает
сокеты после завершения. Результаты включены в `/tmp/stage10-browser.json`.
Полная браузерная проба занимает больше минуты; PATCH после неопределённого
результата автоматически не повторяется.

### Этап 11: зашифрованная копия и восстановление после потери источника

`deployment_restore_probe.py` разрушает **только свои** проекты `stage11-source`
и `stage11-restored`, включая исходные volumes/env/TLS и локальную копию. Нужны
предварительно собранные текущие app/web images, age/age-keygen, SSH fixture и
образец `/tmp/stage1-media/h264-aac.mp4`. Основные volumes приложения не используются.

Для повторения подготовьте приватный `/tmp/stage11-dr` (0700): `recovery-key`
от `age-keygen`, `ssh-key` (0600) и `known_hosts` с ключом выделенного SSH-сервера
на **127.0.0.1:22228**. На сервере разрешён только этот ключ для `root`, есть
пустой постоянный каталог `/backups`. В облачной проверке использован отдельный
Ubuntu SSH-контейнер с volume; это моделирует внешний сервер, но не другую машину.
Не используйте реальные ключи/сертификаты или существующие проекты этих имён.

```bash
STAGE11_TEST_PROJECT=stage11-source uv run --locked python experiments/deployment_restore_probe.py
node experiments/restored_playback_probe.cjs
```

Python-проба создаёт частный CA и проверяет TLS hostname/chain без отключения TLS,
проверяет Secure cookie, роли, private/hidden ACL, Range, социальные данные,
очередь при остановленном worker, согласованность age snapshot и SSH/hash/rename.
Затем удаляет исходник, получает **единственную оставшуюся** копию по SSH,
отклоняет повреждённый архив, восстанавливает отдельные БД/media/TLS, проверяет
сессии/права, хеши MP4/JPEG, аудит и завершение ожидавшего задания. Занятый target
перезаписывать нельзя. Отчёт `/tmp/stage11-restore.json`.

Браузерная проба читает приватный `/tmp/stage11-dr/native.json` от первой пробы,
разрешает `video.test` в loopback и доверяет только SPKI конкретного тестового
сертификата. Проверяет вход, Secure cookie, настоящие декодированные кадры,
перемотку, private 404 для гостя, адаптивные размеры и отсутствие storage keys
в журнале. Нужен Chromium с MP4/H.264, например `/usr/bin/chromium`.
Отчёт `/tmp/stage11-browser.json`, снимок `/tmp/stage11-restored-mobile.png`.
Никакие тестовые CA/ключи/учётные данные в Git не добавляются.

Проверка Ubuntu выполнялась в свежем `ubuntu:24.04` с установленными скриптом
APT-зависимостями, локальным fixture Git origin, проброшенным Docker socket и
флагами `--no-systemd --use-existing-docker`. Пути checkout монтируются в одинаковые
абсолютные пути на хосте и в контейнере (bind mounts разрешает Docker daemon).
В этой облачной сети дополнительно нужны её CA и доступный из контейнера proxy;
TLS verification сохраняется. Это не проверка systemd, установки daemon на VM,
firewall, публичного DNS/ACME или физически удалённого backup-хоста. Эти проверки
выполняются отдельно на предоставленной инфраструктуре по docs/deployment.md.


### Исправления этапа 11: независимый повтор восстановления

Для повторной проверки без удаления прежнего `stage11-restored` предусмотрен
фиксированный suffix `-fixes`. Подготовьте отдельный приватный `/tmp/stage11-dr-fixes`
с такими же **тестовыми** age/SSH ключами и known_hosts. SSH fixture должен быть
запущен вместе с sshd, порты 8189/8449/8190/8450 свободны:

```bash
STAGE11_TEST_SUFFIX=-fixes STAGE11_TEST_PROJECT=stage11-source-fixes uv run --locked python experiments/deployment_restore_probe.py
STAGE11_TEST_SUFFIX=-fixes node experiments/restored_playback_probe.cjs
```

Проба создаёт полноценные cert/chain/fullchain/private-key symlinks и Certbot
`renewal/*.conf`. После удаления source и восстановления проверяет перенос путей.
Для независимой проверки парсера используйте настоящий Certbot с восстановленным
`--config-dir`: `certbot certificates`; конфигурация не должна ссылаться на source.
Это не заменяет публичный `renew --dry-run`: частный домен video.test не проходит ACME.
Отчёты suffix-варианта: `/tmp/stage11-restore-fixes.json`, `/tmp/stage11-browser-fixes.json`.

`tests/test_ops_storage.py` проверяет реальный обмен pipe при больших stdout/stderr,
ограничение памяти ошибки, завершение дочерних процессов, таймауты, обратное давление
при импорте и отказ до запуска при достижении резерва. Проверки preflight/изменения
свободного места, извлечения архива и переноса/истечения сертификатов — в
`tests/test_operations.py`. Тесты не расходуют весь реальный диск приложения.
