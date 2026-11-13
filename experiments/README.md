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
