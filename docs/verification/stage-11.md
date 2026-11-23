# Этап 11: установка, HTTPS и восстановление

Ветка `feature/stage-11-deployment` создана от свежей `work`,
`622e448a42768a12ec2854eae65b657be9f7ef34`. Изменения локальные; push/merge и
публичный выпуск не выполнялись. Инструкция: [deployment.md](../deployment.md).

## Реализация

- Bootstrap Ubuntu 24.04: зависимости из APT, фиксируемый Git ref, Compose build,
  приватный env, миграции, ожидание worker и прикладной диагностики, затем Nginx.
- Разделены локальный HTTP и production HTTPS/ACME. HTTPS принудительно включает
  Secure `__Host-` cookie, HSTS, канонический redirect и проверку Host. Certbot
  обслуживается systemd timer; отдельный ACME webroot работает без app/БД.
- Ops CLI: start/status/tls/renew/backup/ship/restore/update. Блокировка конкурирующих
  операций; PostgreSQL 17; схема остаётся 0011, API не меняется.
- Согласованный snapshot при остановленных writers: БД + все media + env + TLS,
  manifest/checksums, age encryption, SSH с проверкой host key, проверкой хеша и
  атомарным rename. Расшифровывающий ключ хранится вне прикладного сервера.
- Restore только в пустой проект, проверка архива/версии/схемы до создания БД/env,
  транзакционный pg_restore, сохранение владельца media, запуск/recovery/диагностика.
- Update с предварительной внешней копией. При ошибке сборки возвращаются прежние
  код/образы/сервисы; после возможного начала миграций writers остаются остановлены,
  CLI указывает копию и прежний SHA. Автоматического downgrade схемы нет.
- После ревью добавлены preflight размера snapshot и контроль резерва при записи
  dump/media/шифровании/распаковке и при импорте в Docker volumes. По достижении
  резерва дочерние процессы завершаются, `.part`/snapshot удаляются, исходные
  writers backup возобновляются; неудачный restore останавливает новую БД и
  прикладные сервисы. Нехватка места отклоняется до начала downtime.
- Restore переносит абсолютные Certbot renewal paths/webroot map с сохранением
  ACME account/server и имени сертификата. Повторный installer направляет
  истёкший/скоро истекающий сертификат в Certbot, сохраняя работающий HTTPS gateway.
- Операторский JSON: диск/резерв, очередь/загрузки, worker, HTTP 4xx/5xx, срок TLS,
  последняя успешная копия. Ограниченная ротация Docker logs. Nginx не пишет query,
  cookie, credentials и внутренние storage keys; обычный Uvicorn access log выключен.

## Выполненные проверки

| Сценарий | Результат | Основание |
| --- | --- | --- |
| Регрессия Python/PostgreSQL | **passed** | `RUN_DB_TESTS=1 pytest -q`: 521 passed; отдельная тестовая БД `stage4_test`, PostgreSQL 17 |
| Проверки ops после исправлений | **passed** | `pytest -q tests/test_operations.py tests/test_ops_storage.py`: 54 passed; перенос/истечение TLS, reserve/preflight, pipe backpressure, таймауты, завершение процессов и остановка новой БД при сбое restore |
| Стиль, формат, shell, OpenAPI | **passed** | Ruff check/format, bash -n install/start, node --check, export_video_contract --check |
| Ubuntu bootstrap | **passed в контейнере** | Свежий `ubuntu:24.04`, скрипт установил Python/age/Certbot/SSH/Docker CLI/Compose/buildx, получил fixture Git ref, собрал app/web, мигрировал и запустил полноценный стек на 18111; status=ok |
| Локальный рабочий стек 8080 | **passed** | Новый start.sh запустил стек с сохранённым env; главная, Swagger, OpenAPI, live/ready и diagnostics=ok, operator status=ok |
| Повторная установка | **passed в контейнере** | Тот же ref/команда: байты `.env` сохранились, учётные данные/приватное видео/MP4 SHA-256 сохранились |
| Обновление | **passed** | Fixture Git release → новый release; перед обновлением получена age/SSH копия; env, private ACL, MP4 hash и данные сохранены |
| Ошибка сборки обновления | **passed** | Намеренный `RUN exit 77`: возврат к прежнему SHA/образам, возобновление исходных сервисов; env и private MP4 сохранены |
| systemd units | **passed синтаксис** | Извлечены 5 service/timer units, `systemd-analyze --user verify`; ExecStart и docker dependency подставлены тестовыми заглушками. Это не проверка запуска systemd на VM |
| HTTP-01 bootstrap Nginx | **passed** | Реальный ACME webroot возвращает challenge 200, обычный трафик 503, health 200; app/БД не запущены |
| HTTPS и cookies | **passed с частным тестовым CA** | Проверены chain/hostname без отключения TLS, HTTP→HTTPS, неверный Host 421, HSTS и Secure __Host-cookie |
| Отказ worker | **passed** | После истечения heartbeat diagnostics degraded, ready=200, MP4 Range=206, новая загрузка остаётся processing до восстановления worker |
| Шифрование и SSH | **passed на SSH fixture** | Реальный age и SSH с проверенным known_hosts, совпадение SHA-256, удалённый архив 0600, атомарное завершение; намеренно остановленный worker не включён backup-командой |
| Потеря источника и restore | **passed в отдельном Compose project** | Исходные БД/media volumes, env/TLS и локальный encrypted archive удалены **до** загрузки единственной оставшейся копии по SSH и восстановления |
| Данные и доступ после restore | **passed** | Сессии, роли, grants, public/private/hidden, лайки/комментарии, плейлисты, жалобы, обе истории аудита; исходные MP4/JPEG SHA-256, Range, отзыв grant и фильтрация плейлиста |
| Очередь после restore | **passed** | Задание, принятое при остановленном worker, завершилось после восстановления в новом проекте |
| Нативный браузер после restore | **passed Chromium** | Реально декодированные кадры 320×180, перемотка, HTTPS login, Secure cookie, private 404 гостю; ширины 320/390/768/1440, без JS errors |
| Certbot после полного restore | **passed реальный Certbot 2.9.0** | Восстановленный cert/chain/key/fullchain распознаны после удаления исходного каталога, renewal paths указывают на новый project; публичный ACME отдельно unrun |
| Истёкший сертификат при repeat | **passed OpenSSL + issuer fixture** | Реальный OpenSSL обнаруживает expiry; Certbot вызывается, свежий fixture cert устанавливается, Nginx reload запрашивается без замены живого HTTPS |
| Недостаток места | **passed на tmpfs 64 МиБ** | Preflight отказал до downtime. Во время записи сохранены 9 МиБ при резерве 8 МиБ, ENOSPC не достигнут; временные файлы удалены. Дополнительно unit-сценарии резкого изменения free space и отказа импорта |
| Повреждённый/опасный архив и занятый target | **passed** | Corrupted ciphertext, хеши, неизвестная схема/формат/PG major, traversal/links/special files; существующий env/проект не перезаписывается |
| Журналы | **passed** | Пароль, cookie и чувствительный query-маркер отсутствуют. После внутреннего X-Accel записывается исходный `/api/videos/…/stream`, storage key отсутствует |

Python/full suite использует реальный PostgreSQL, не SQLite. Единственное
предупреждение pytest — upstream StarletteDeprecationWarning о TestClient/httpx.
Новую схему данных этап 11 не добавляет.

Сквозная проба: [deployment_restore_probe.py](../../experiments/deployment_restore_probe.py).
Браузер: [restored_playback_probe.cjs](../../experiments/restored_playback_probe.cjs).
После ревью полный цикл повторён в `stage11-source-fixes` → `stage11-restored-fixes`
с настоящим `renewal/*.conf`, cert/chain/fullchain symlinks. Исходные volumes,
env/TLS и локальная копия удалены до restore. Проверен реальный parser Certbot
на **восстановленном** TLS-каталоге, затем нативное HTTPS-воспроизведение в Chromium.
Проверка expiry использует настоящий OpenSSL и локальный issuer fixture, не публичный CA.

Изменение объёма snapshot не требует миграции: format=1 сохранён, поля исходного
размера БД/media добавлены обратно совместимо. Старые архивы проходят прежнюю
проверку формата/хешей, размер импорта оценивается по dump и контролируется при записи.

Локальные артефакты проверки (не входят в Git):
`/tmp/stage11-fixes-tests.log`, `/tmp/stage11-fix-unit.log`,
`/tmp/stage11-restore-fixes.json`, `/tmp/stage11-browser-fixes.json`,
`/tmp/stage11-fixed-restored-certbot.log`, `/tmp/stage11-fixed-expiry.log`,
`/tmp/stage11-fixed-space.log`, `/tmp/stage11-restore.json`,
`/tmp/stage11-browser.json`, `/tmp/stage11-restored-mobile.png`,
`/tmp/stage11-ubuntu.log`, `/tmp/stage11-ubuntu-repeat-data.log`,
`/tmp/stage11-ubuntu-update.log`, `/tmp/stage11-bad-update.log`.
Фикстуры используют отдельные аккаунты/ключи/CA; секреты не коммитятся.

## Не проведено на целевой инфраструктуре

| Проверка | Статус / причина |
| --- | --- |
| Чистая Ubuntu **VM**, запуск Docker daemon/systemd и перезагрузка хоста | **unrun**: доступен контейнер, использован существующий rootless Docker daemon с VFS; флаги `--no-systemd --use-existing-docker`. Контейнерная установка не доказывает работу kernel/cgroups/firewall/boot units |
| Публичный DNS, сертификат Let's Encrypt и его настоящее продление | **unrun**: владелец ещё не предоставил домен/email/доступную VM. HTTPS проверен с частным CA, HTTP-01 webroot — отдельно; renew ordering проверен unit-тестами |
| Физически внешний backup-сервер и restore на другой машине | **unrun**: SSH fixture и новые независимые volumes находятся на том же Docker host. Проверены протокол и полная потеря исходного проекта, но не потеря физического хоста |
| Полная браузерная матрица, нагрузка и публичный выпуск | Следующий этап 12 |

В облачной сети Ubuntu-контейнеру потребовались доверенный CA/proxy и корректное
разрешение имени proxy. Проверка TLS не отключалась. Docker используется из Ubuntu
APT; зависимости от недоступного здесь download.docker.com нет.

Перед публичным запуском выполните оставшиеся инфраструктурные проверки по
инструкции на предоставленных серверах. Предусмотрите место под незашифрованный
временный snapshot и encrypted archive, хранение recovery identity вне сервера,
внешние алерты на failed backup/renew, срок TLS и устаревшую копию. Резервное
копирование MVP останавливает writers и даёт окно недоступности.
