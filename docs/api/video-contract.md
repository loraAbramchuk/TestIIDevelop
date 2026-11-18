# Контракт видео: этапы 1–9

Статус: реализованы метаданные, grants, передача исходника, обработка,
защищённая раздача, удаление, каталог, каналы, подписки, лайки, комментарии и плейлисты. Схемы находятся в
`app/video_contracts.py`, `app/catalog/schemas.py`, `app/channels/schemas.py`,
`app/interactions/schemas.py`, `app/playlists/schemas.py`;
машиночитаемый контракт — [video.openapi.json](video.openapi.json).
Поле x-implemented в JSON вычисляется по действующему router. `/docs` описывает
только реализованные операции авторизации, health, videos, catalog, channels, interactions и playlists.

## Общие правила

- Идентификаторы видео и пользователей — UUID. Владельца сервер берёт из сессии.
- JSON-запросы не принимают неизвестные поля. Нельзя передавать owner_id, роль,
  статус обработки, ключ/путь файла или состояние модерации.
- Название: 1–200 символов после обрезки пробелов. Описание: до 5000 символов,
  по умолчанию пустое. Видимость: public/private, по умолчанию private.
- Текстовые метаданные, название/описание канала и имя при регистрации
  отклоняют NUL и непарные суррогаты Unicode с 422 до сохранения в БД.
- Пароли отклоняют непарные суррогаты с 422 до Argon2, не раскрываются в ошибке
  и сохраняют исходные пробелы/корректный Unicode без нормализации.
- Для всех изменений обязательны сессионная cookie и X-CSRF-Token из auth API.
  Secure-режим использует __Host-video_session, локальный HTTP — video_session.
- Неавторизованная обязательная операция — 401; ошибка CSRF — 403. Недоступное
  для просмотра видео — 404, включая скрытые/удалённые/неготовые видео.
- Ошибка предметной операции: `{"detail":"Понятное сообщение","code":"stable_code"}`.
  Существующие зависимости auth могут вернуть detail без code. Ошибки полей 422
  сохраняют формат detail-массива loc/msg/type без input/ctx и чувствительных данных.
- Ответы с данными видео, grants и файлы: Cache-Control: private, no-store.
  Ответы не содержат абсолютных путей, ключей storage, lease-токенов или паролей.

## Создание и редактирование

`POST /api/videos` → **201 VideoOwnerStatus**

```json
{"title":"Первое видео","description":"Описание","visibility":"private"}
```

Результат содержит id, owner_id, метаданные, visibility, processing_status=uploading,
moderation_status=visible, timestamps, nullable error_code/moderation_reason.
Этот ответ не означает готовность файла. Повторный POST создаёт отдельное видео;
клиент не повторяет создание автоматически при неопределённом исходе запроса.

`PATCH /api/videos/{video_id}` → **200 VideoOwnerStatus**, только владелец.
Разрешены title, description, visibility. Отсутствующее поле не меняется;
явный null недопустим. Пустой объект — отсутствие изменений. Изменение видимости
не снимает модерацию и не переводит видео в ready.

`GET /api/videos/{video_id}/status` → **200 VideoOwnerStatus**, только владелец.
Возвращает состояние uploading/processing/ready/failed и безопасную причину
ошибки/скрытия даже до готовности видео. Для чужого или удалённого объекта — 404.
Рекомендуемый опрос UI — раз в 2 секунды, с увеличением интервала при длительной обработке.

`GET /api/studio/videos?limit=20&cursor=...` → **200 OwnedVideoList**. Реализован
на этапе 2. Только свои неудалённые карточки, включая неготовые и скрытые.
Порядок created_at/id по убыванию, limit 1–100, items и nullable next_cursor.
Курсор привязан к владельцу; чужой или некорректный курсор — 400.

## Каталог, каналы и подписки

| Метод | Endpoint | Ответ и доступ |
| --- | --- | --- |
| GET | `/api/videos?q=...&limit=20&cursor=...` | 200 CatalogPage; только публичная выдача |
| GET | `/api/channels/me` | 200 ChannelView; канал текущего пользователя, иначе 401 |
| GET | `/api/channels/{channel_id}` | 200 ChannelView; публичный профиль |
| PATCH | `/api/channels/{channel_id}` | 200 ChannelView; только владелец, иначе 404 |
| GET | `/api/channels/{channel_id}/videos` | 200 CatalogPage; готовые видео с ACL посетителя |
| PUT | `/api/channels/{channel_id}/subscription` | 200 SubscriptionState; подписаться, сессия и CSRF |
| DELETE | `/api/channels/{channel_id}/subscription` | 200 SubscriptionState; отменить, сессия и CSRF |
| GET | `/api/subscriptions` | 200 ChannelList; только свои подписки, иначе 401 |

CatalogPage и ChannelList содержат items и nullable next_cursor. CatalogVideo
расширяет VideoView полем author: `{id, owner_id, name}` канала.
ChannelView содержит id, owner_id, name, description, created_at, updated_at,
subscriber_count и is_subscribed (для гостя false). Email и список подписчиков
не публикуются. SubscriptionState: channel_id, is_subscribed, subscriber_count.
Для неизвестного канала — 404 channel_not_found.

Публичная выдача включает только public + ready + visible + отсутствие удаления,
даже для владельца или получателя grant. q — до 200 символов, внешние пробелы
обрезаются, поиск по части названия без учёта регистра. `%`, `_`, `/` и `\`
не превращаются в шаблоны SQL; NUL отклоняется с 400 invalid_query.
Пустой q в API означает публичную ленту; HTML `/search` показывает приглашение
ввести название. Выдача канала использует общую политику public/владелец/grant,
без привилегий moderator/admin и без расширения доступа по подписке.

Все три списка принимают limit 1–100 (20 по умолчанию), cursor до 1024 символов.
Видео сортируются по created_at/id по убыванию; подписки — по времени подписки
и channel_id. Курсор публичной выдачи привязан к обрезанному q, канала — к каналу
и посетителю, списка подписок — к пользователю. Чужой контекст или повреждённый
курсор — 400 invalid_cursor. Каждый запрос заново проверяет доступ.

PATCH принимает name (1–100 символов после trim) и description (до 2000).
Пропущенные поля не меняются, `{}` допустим; null, NUL и неизвестные поля — 422.
Повтор подписки/отмены идемпотентен; уникальная пара исключает дубли при гонках.
Самоподписка — 409 self_subscription. PUT/DELETE возвращают JSON-состояние,
в том числе при повторе отмены. Изменения профиля и подписок имеют отдельный
общий счётчик каналов: VIDEO_WRITE_LIMIT_PER_MINUTE на пользователя; 429 с Retry-After.

Ответы каталога/каналов и HTML используют no-store и Vary: Cookie.
Недоступность БД — 503 service_unavailable с безопасным сообщением; HTML
получает страницу ошибки. Некорректные параметры HTML-каталога — страница 422.

## Лайки и комментарии

| Метод | Endpoint | Ответ |
| --- | --- | --- |
| GET | `/api/videos/{video_id}/likes` | 200 LikeState |
| PUT | `/api/videos/{video_id}/likes` | 200 LikeState; идемпотентный лайк |
| DELETE | `/api/videos/{video_id}/likes` | 200 LikeState; идемпотентная отмена |
| GET | `/api/videos/{video_id}/comments` | 200 CommentPage |
| POST | `/api/videos/{video_id}/comments` | 201 CommentView |
| DELETE | `/api/videos/{video_id}/comments/{comment_id}` | 204; удалить свой комментарий |

Все операции, включая счётчики, требуют ready + visible + отсутствия удаления +
public/владелец/grant. Роли и подписки не обходят эту проверку. GET доступен гостю
для публичного видео; запись требует сессии и CSRF. Недоступное видео — одинаковый
404 video_not_found до чтения взаимодействий. Изменения сериализуются строкой
видео, затем повторно проверяют актуальную ACL после ожидания блокировки.

LikeState: video_id, like_count, is_liked. Для гостя is_liked=false. Уникальная
пара video_id/user_id исключает дубли, повтор PUT/DELETE возвращает текущее
состояние с JSON-телом, включая отмену отсутствующего лайка.

POST принимает только `{"text":"Комментарий"}`: 1–2000 символов после trim.
Пустой текст, null, NUL, непарный суррогат, неверный тип и лишние поля — 422.
Вложения, HTML-разметка и ответы на комментарии не поддерживаются; HTML-подобный
текст допустим и отображается буквально. POST создаёт отдельную запись при
каждом успешном вызове; автоматически повторять запрос после потери ответа нельзя.

CommentView: id, video_id, text, created_at, author (ChannelSummary), can_delete.
Автор представлен публичным каналом, без email. can_delete=true только у автора
комментария. Владелец видео, moderator и admin не могут удалять чужие комментарии.
Чужой, отсутствующий или относящийся к другому видео комментарий —
404 comment_not_found; после потери доступа даже свой комментарий — 404 video_not_found.

CommentPage: items, next_cursor, total_count. Порядок created_at/id по убыванию,
limit 1–100 (20 по умолчанию), cursor до 1024 символов. Курсор привязан к видео и
посетителю; неверный контекст — 400 invalid_cursor. ACL заново проверяется до
разбора курсора и подсчёта строк на каждой странице. total_count — число
комментариев этого видео, не количество текущей страницы.

Изменения имеют общий лимит INTERACTION_WRITE_LIMIT_PER_MINUTE (120) на
пользователя; POST дополнительно COMMENT_CREATE_LIMIT_PER_MINUTE (10).
Лимиты хранятся в PostgreSQL, не зависят от HTTP-процесса; 429 с Retry-After.
Ответы используют private, no-store и Vary: Cookie, включая ошибки.

## Публичные плейлисты

| Метод | Endpoint | Ответ |
| --- | --- | --- |
| GET | `/api/playlists` | 200 PlaylistList; только свои списки, сессия обязательна |
| POST | `/api/playlists` | 201 PlaylistView; создать свой список |
| GET | `/api/playlists/{playlist_id}` | 200 PlaylistView; публичные метаданные |
| PATCH | `/api/playlists/{playlist_id}` | 200 PlaylistView; переименовать свой список |
| DELETE | `/api/playlists/{playlist_id}` | 204; удалить свой список |
| GET | `/api/playlists/{playlist_id}/items` | 200 PlaylistVideos; доступные видео в порядке списка |
| PUT | `/api/playlists/{playlist_id}/items/{video_id}` | 204; идемпотентно добавить доступное видео |
| DELETE | `/api/playlists/{playlist_id}/items/{video_id}` | 204; идемпотентно удалить элемент |
| DELETE | `/api/playlists/{playlist_id}/unavailable-items` | 204; убрать недоступные владельцу элементы |
| PUT | `/api/playlists/{playlist_id}/items/{video_id}/position` | 204; переместить элемент |
| GET | `/api/channels/{channel_id}/playlists` | 200 PlaylistList; публичные списки канала |

POST и PATCH принимают `{"title":"Название"}` — 1–200 символов после trim.
Название обязательно; `{}`, null, NUL, некорректный Unicode и лишние поля — 422.
Поля owner_id/visibility/positions не принимаются. В MVP все плейлисты публичные.
PlaylistView: id, title, author (ChannelSummary), visible_count, can_manage,
created_at, updated_at. Список PlaylistList: items и nullable next_cursor.
PlaylistVideos: items (CatalogVideo), next_cursor, visible_count.

Чтение и счётчики фильтруют видео по ready/visible/отсутствию удаления и
public/владелец/grant текущего посетителя. Плейлист, подписка и роль не дают
дополнительного доступа. Количество всех сохранённых элементов, сырые позиции
и метаданные недоступных видео не выдаются даже владельцу плейлиста.

Все изменения требуют сессии, CSRF и владения плейлистом. Чужой плейлист для
изменения — 404 playlist_not_found, в том числе для admin/moderator.
Добавление требует текущего доступа к видео, повтор существующего элемента
не меняет порядок. Удаление из своего списка допустимо и после потери доступа
к видео; отсутствие элемента также даёт 204 без раскрытия его существования.
Удаление элемента/плейлиста не удаляет видео, лайки, комментарии или grants.
Удаление уже отсутствующего плейлиста — 404.

DELETE unavailable-items — явная идемпотентная операция владельца с CSRF и
общим лимитом записи. Она блокирует плейлист и все его видео в порядке UUID,
затем заново проверяет ACL. Удаляются только недоступные владельцу элементы;
восстановленное за время ожидания разрешение учитывается. Видео, media-файлы,
grants, лайки, комментарии и другие плейлисты не меняются. Ответ не содержит
количества удалённых элементов и их идентификаторов. При изменении состава
старые курсоры дают 409; если удалять нечего, версия не меняется.

Перемещение принимает `{"before_video_id":"UUID"}`; null или `{}` — в конец.
Оба элемента должны входить в плейлист и быть доступны владельцу. Чужой/неточный
элемент — 404 playlist_item_not_found; недоступное видео — 404 video_not_found;
перенос перед самим собой — 409 invalid_order. Транзакция блокирует плейлист и
обновляет позиции с отложенной проверкой уникальности, сохраняя состав.

Списки принимают limit 1–100 (20), cursor до 1024 символов. Списки метаданных
сортируются по created_at/id по убыванию, курсор привязан к владельцу и посетителю.
Элементы выдаются по внутренней позиции; подписанный курсор привязан к плейлисту,
посетителю и версии порядка, не содержит номеров позиций. Подмена якоря/подписи
или чужой контекст — 400 invalid_cursor; изменение состава/порядка —
409 playlist_changed с предложением обновить список. Изменение ACL учитывается
на следующем запросе, включая ранее разрешённый якорь курсора.

Лимиты на пользователя: PLAYLIST_CREATE_LIMIT_PER_HOUR=30 и
PLAYLIST_WRITE_LIMIT_PER_MINUTE=120. Ответ 429 содержит Retry-After.
PLAYLIST_MAX_ITEMS=1000 ограничивает состав (настройка 1–10000);
новый элемент при заполнении — 409 playlist_full, повтор существующего — 204.
Ответы API и страниц используют private, no-store/Vary: Cookie.

## Передача файла

Реализован на этапе 3; задания сохраняются в video_queue, worker этапа 4 подготавливает MP4/превью и задаёт ready/failed.

`PUT /api/videos/{video_id}/content`

- Только владелец, состояния uploading/failed без занятой действующей аренды.
- Content-Type: application/octet-stream, тело — бинарный файл, не JSON/base64. Профиль MP4 проверяет worker.
- Content-Length допустим, но не обязателен. Размер проверяется по фактической
  записи. При известной длине фактический размер должен ей соответствовать.
- Для неизвестной длины резервируется максимально допустимый файл. Приложение
  не начинает чтение до проверки сессии, CSRF, квот и получения аренды.
- Возобновление с байтовой позиции не поддерживается. Повтор после failed
  начинает новый временный файл и новую попытку. Повтор для processing запрещён;
  отмена активной загрузки/обработки выполняется через DELETE видео.
- Одновременная передача того же видео и повтор после processing/ready → 409.
- Обрыв/таймаут не публикует файл. Клиент проверяет status перед повтором,
  особенно если соединение оборвалось после отправки последнего байта.

После завершённого файла и согласованной записи задания → **202 UploadAccepted**:

```json
{"id":"00000000-0000-0000-0000-000000000001","processing_status":"processing","status_url":"/api/videos/00000000-0000-0000-0000-000000000001/status"}
```

Размер → 413; неверный Content-Type → 415; таймаут при сохранённом соединении →
408; квота/место → 507; заняты все слоты → 429; переполнена очередь → 503.
Неверная/пустая длина → 400. Nginx может вернуть 413 HTML до обращения к API.
Для временного ограничения возвращается Retry-After. На этапе 4 ошибка формата
после ffprobe переводит processing в failed с безопасным error_code. Ошибка
обработки не меняет приватность и не открывает частичные файлы.

Прогресс UI показывает передачу файла (через upload progress), а не проценты
работы FFmpeg. Processing показывается отдельным состоянием.

## Просмотр и превью

`GET /api/videos/{video_id}` → **200 VideoView** только при ready + visible +
отсутствии удаления + public/владелец/grant. Данные: id, owner_id, название,
описание, видимость, длительность, ширина, высота, created_at и относительные
stream_url/thumbnail_url. Список получателей доступа не раскрывается.

`GET/HEAD /api/videos/{video_id}/stream` → **200/206/416**.
`GET/HEAD /api/videos/{video_id}/thumbnail` → **200/206/416**.

Метаданные и файловые маршруты реализованы. Браузер открывает `/watch/{id}`,
а владелец управляет видео и grants на `/studio/videos/{id}`.

Каждый запрос проходит ту же проверку доступа. Nginx читает файл через внутренний
location после X-Accel-Redirect. Прямой запрос внутреннего location — 404.
Для видео: video/mp4, Accept-Ranges: bytes, Content-Length, для 206 — Content-Range,
для 416 — Content-Range: bytes */size. HEAD не содержит тела. Новые Range-запросы
учитывают отзыв доступа; уже переданные/буферизованные байты отозвать невозможно.
Модератор и администратор не обходят private или hidden.

Поддерживаются один диапазон, открытый конец и суффикс. Несколько диапазонов
игнорируются с полным ответом 200 (`max_ranges 1`). Условный запрос может вернуть
304 только после успешной ACL; отзыв доступа всегда даёт 404, даже с Range и
условными заголовками. ETag выключен, If-Modified-Since игнорируется; no-store
распространяется на файлы и ошибки. При недоступном/изменённом файле после
успешной ACL — 503 media_unavailable без раскрытия пути. Без Nginx приложение
возвращает только внутренний X-Accel-Redirect, а не байты файла.

## Разрешения

- `GET /api/videos/{id}/grants?limit=20&cursor=...` → **200 GrantList**. Только
  владелец, limit 1–100, стабильный порядок по created_at/user_id. Список пустой
  при отсутствии разрешений; курсор непрозрачен и не является разрешением.
- `PUT /api/videos/{id}/grants/{user_id}` → **200 VideoGrant**. Только владелец,
  существующий получатель. Повтор возвращает то же разрешение без дубля и без
  изменения created_at. Разрешение владельцу избыточно и отвечает 409.
- `DELETE /api/videos/{id}/grants/{user_id}` → **204**, включая отсутствующий grant.

Grant содержит user_id и created_at. Для несуществующего получателя — 404.
Grants можно подготовить до ready или для public-видео: после смены на private
они продолжают действовать. Grant не снимает hidden и не открывает processing.
Отзыв применяется к новым запросам после commit. Выдача доступа не отправляет
письма или сообщения.

## Удаление

Реализовано на этапе 5.

`DELETE /api/videos/{video_id}` → **204**, только владелец. Транзакция ставит
deleted_at, отменяет публикацию обработки и записывает очистку. Новый просмотр
сразу запрещён. Ответ не означает, что worker уже освободил диск. Резерв квоты
освобождается по факту удаления файлов, не в момент выдачи 204.
Повтор владельца для сохранившейся tombstone — 204; чужой/неизвестный UUID — 404.

Очистка записывается в существующий журнал upload_attempts: все неочищенные
попытки становятся cleanup. Поколения processing_runs теряют аренду и право
публикации, библиотечное задание отменяется либо получает abort_requested.
Это одна транзакция с deleted_at; файловых операций в ней нет. Maintenance
удаляет производные и исходники с учётом flock, затем освобождает резерв.
Остановка worker/maintenance не теряет очистку; повтор безопасен.

## Генерация и проверка

```bash
uv run --locked python scripts/export_video_contract.py
uv run --locked python scripts/export_video_contract.py --check
uv run --locked pytest tests/test_video_contracts.py
```

Схемы и JSON-контракт проверяются вместе. Тест сверяет x-implemented для каждой
операции с действующим OpenAPI; GET/HEAD файлов и DELETE отмечены реализованными.


## Жалобы, модерация и аудит

Все изменения требуют сессии и CSRF. Служебные операции — moderator/admin;
обычный пользователь получает 403, гость — 401. Ответы private/no-store, Vary: Cookie.

| Метод | Endpoint | Ответ |
| --- | --- | --- |
| POST | `/api/videos/{video_id}/reports` | 201 ReportReceipt, 200 при повторе открытой жалобы; только доступное видео |
| GET | `/api/moderation/reports?status=open&limit=20&cursor=...` | 200 ReportPage |
| GET | `/api/moderation/reports/{report_id}` | 200 StaffReport |
| POST | `/api/moderation/reports/{report_id}/decision` | 200 StaffReport |
| GET | `/api/moderation/videos/{video_id}` | 200 ModerationVideo |
| POST | `/api/moderation/videos/{video_id}/visibility` | 200 VideoModerationResult |
| GET | `/api/moderation/audit?video_id=...&limit=20&cursor=...` | 200 AuditPage |

ReportCreate: reason=spam/prohibited/copyright/other и необязательное explanation
до 2000 символов (по умолчанию пустая строка). ReportReceipt: id, video_id,
reason, explanation, status, created_at; без UUID отправителя и сведений решения.
Повтор открытой жалобы не меняет reason/explanation; после закрытия возможна новая.
StaffReport дополнительно содержит reporter_id, decided_at/decided_by/decision_reason
и video: ModerationVideo. Статусы жалобы open/resolved/dismissed.

ReportDecision: status=resolved/dismissed, reason. VideoModeration:
status=hidden/visible, reason. Причина после trim — 1–2000 символов. Неизвестные
поля, null, NUL, непарные суррогаты — 422; пробельная причина — 422.
VideoModerationResult расширяет ModerationVideo полем changed: true только при
реальном переходе состояния и записи нового аудита; false при уже установленном
состоянии. Повтор возвращает 200 без нового аудита/замены причины.
GET-профиль видео по-прежнему возвращает ModerationVideo без changed.
Повтор решения с теми же status/reason возвращает первоначальное решение;
изменение рассмотренной жалобы — 409 report_closed. Причина восстановления
остаётся в истории; текущий moderation_reason после восстановления — null.

ModerationVideo: id, owner_id, visibility, processing_status, moderation_status,
moderation_reason, deleted, can_watch, nullable title/watch_url. Последние два
выдаются только при обычном праве просмотра; ни роль, ни жалоба его не расширяют.
Служебное чтение не содержит описания, poster/stream URL, grants или storage keys.
Удалённое видео остаётся в деле, его восстановление — 409 video_deleted.
Решение по жалобе на удалённое видео допустимо. Неизвестные UUID — 404
video_not_found/report_not_found. Недоступное для жалобы видео — 404.

ReportPage/AuditPage: items, nullable next_cursor. Статус очереди:
open/resolved/dismissed/all, по умолчанию open; неверный — 422.
Сортировка created_at/id по убыванию, limit 1–100 (20 по умолчанию), cursor
до 1024 символов, привязан к сотруднику и фильтру; чужой/испорченный — 400 invalid_cursor.
video_id истории необязателен, фильтрует действия с видео и решения его жалоб.
AuditView: id, actor_id, object_type, object_id, action, reason, details, created_at.
Действия video.hidden/video.restored/report.resolved/report.dismissed,
details — previous_status/status и video_id для решения жалобы. Удаление объектов
не каскадирует UUID аудита. Действие и его аудит записываются одной транзакцией.
API изменения или удаления аудита нет.

REPORT_CREATE_LIMIT_PER_HOUR=10 и MODERATION_WRITE_LIMIT_PER_MINUTE=120 —
персональные лимиты; превышение — 429 с Retry-After. Попытки учитываются также
при неуспешной предметной операции, повторные запросы расходуют лимит.
