"""Generate video contracts and mark implemented operations from the actual router."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import video_contracts as models  # noqa: E402
from app.catalog.router import router as catalog_router  # noqa: E402
from app.catalog.schemas import CatalogPage, CatalogVideo  # noqa: E402
from app.channels.router import router as channel_router  # noqa: E402
from app.channels.schemas import (
    ChannelList,
    ChannelPatch,
    ChannelSummary,
    ChannelView,
    SubscriptionState,
)  # noqa: E402
from app.interactions.router import router as interaction_router  # noqa: E402
from app.interactions.schemas import CommentCreate, CommentPage, CommentView, LikeState  # noqa: E402
from app.playlists.router import router as playlist_router  # noqa: E402
from app.playlists.schemas import (
    PlaylistList,
    PlaylistMove,
    PlaylistTitle,
    PlaylistVideos,
    PlaylistView,
)  # noqa: E402
from app.uploads.router import router as upload_router  # noqa: E402
from app.videos.router import router as video_router  # noqa: E402

OUTPUT = Path(__file__).resolve().parent.parent / "docs/api/video.openapi.json"


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def json_response(name, description):
    return {"description": description, "content": {"application/json": {"schema": ref(name)}}}


def operation(summary, *, output=None, body=None, mutation=False, authenticated=True, status=200):
    responses = {
        str(status): json_response(output, "Успешный ответ")
        if output
        else {"description": "Без тела"},
        "401": json_response("ContractError", "Требуется вход"),
        "403": json_response("ContractError", "Ошибка CSRF или недостаточно прав"),
        "404": json_response("ContractError", "Объект не найден или недоступен"),
        "409": json_response("ContractError", "Конфликт состояния"),
        "429": json_response("ContractError", "Лимит запросов; Retry-After в секундах"),
        "503": json_response("ContractError", "Сервис временно недоступен"),
    }
    result = {
        "summary": summary,
        "tags": ["planned-video"],
        "x-implemented": False,
        "responses": responses,
    }
    if authenticated:
        result["security"] = [{"SessionCookie": []}]
    else:
        result["security"] = [{}, {"SessionCookie": []}]
    if mutation:
        result["parameters"] = [
            {"name": "X-CSRF-Token", "in": "header", "required": True, "schema": {"type": "string"}}
        ]
    if body:
        result["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": ref(body)}},
        }
        result["responses"]["422"] = {
            "description": "Ошибки полей: detail — массив loc/msg/type без input"
        }
    return result


def build_contract():
    schemas = {}
    for model in (
        models.VideoCreate,
        models.VideoPatch,
        models.VideoOwnerStatus,
        models.VideoView,
        models.UploadAccepted,
        models.VideoGrant,
        models.GrantList,
        models.OwnedVideoList,
        models.ContractError,
        CatalogPage,
        CatalogVideo,
        ChannelList,
        ChannelPatch,
        ChannelSummary,
        ChannelView,
        SubscriptionState,
        LikeState,
        CommentCreate,
        CommentView,
        CommentPage,
        PlaylistTitle,
        PlaylistMove,
        PlaylistView,
        PlaylistList,
        PlaylistVideos,
    ):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    paths = {
        "/api/studio/videos": {
            "get": operation(
                "Свои карточки, включая неготовые и скрытые; без удалённых", output="OwnedVideoList"
            ),
        },
        "/api/videos": {
            "post": operation(
                "Создать метаданные; owner берётся из сессии",
                output="VideoOwnerStatus",
                body="VideoCreate",
                mutation=True,
                status=201,
            )
        },
        "/api/videos/{video_id}": {
            "get": operation(
                "Метаданные готового доступного видео", output="VideoView", authenticated=False
            ),
            "patch": operation(
                "Изменить свои метаданные; статус обработки изменить нельзя",
                output="VideoOwnerStatus",
                body="VideoPatch",
                mutation=True,
            ),
            "delete": operation(
                "Запретить просмотр и запланировать очистку; повтор владельца для tombstone — 204",
                mutation=True,
                status=204,
            ),
        },
        "/api/videos/{video_id}/status": {
            "get": operation(
                "Статус и причина ошибки/скрытия только владельцу", output="VideoOwnerStatus"
            )
        },
        "/api/videos/{video_id}/content": {
            "put": operation(
                "Передать файл; повтор после processing/ready — 409",
                output="UploadAccepted",
                mutation=True,
                status=202,
            )
        },
        "/api/videos/{video_id}/grants": {
            "get": operation("Разрешения только владельцу", output="GrantList")
        },
        "/api/videos/{video_id}/grants/{user_id}": {
            "put": operation(
                "Идемпотентно разрешить просмотр существующему пользователю",
                output="VideoGrant",
                mutation=True,
            ),
            "delete": operation("Идемпотентно отозвать разрешение", mutation=True, status=204),
        },
    }
    upload = paths["/api/videos/{video_id}/content"]["put"]
    upload["requestBody"] = {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
    upload["responses"].update(
        {
            "400": json_response("ContractError", "Длина тела не соответствует объявленной"),
            "408": json_response("ContractError", "Таймаут передачи при сохранённом соединении"),
            "413": json_response(
                "ContractError", "Превышен размер; размер проверяется по фактическим байтам"
            ),
            "415": json_response("ContractError", "Неверный Content-Type"),
            "507": json_response("ContractError", "Недостаточно места или квоты"),
            "503": json_response("ContractError", "Очередь заполнена; Retry-After"),
        }
    )
    pagination = [
        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
        {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
    ]
    paths["/api/videos/{video_id}/grants"]["get"]["parameters"] = pagination
    paths["/api/studio/videos"]["get"]["parameters"] = pagination
    for suffix, mime in (("stream", "video/mp4"), ("thumbnail", "image/jpeg")):
        methods = {}
        for method in ("get", "head"):
            op = operation(
                f"{'Просмотр' if suffix == 'stream' else 'Превью'}; каждый запрос проходит ACL",
                authenticated=False,
            )
            op["responses"]["200"] = {
                "description": "Файл после проверки доступа; Cache-Control: private, no-store"
            }
            op["responses"]["503"] = json_response("ContractError", "Файл временно недоступен")
            op["responses"]["304"] = {"description": "Условный запрос после ACL; без тела"}
            if method == "get":
                op["responses"]["200"]["content"] = {
                    mime: {"schema": {"type": "string", "format": "binary"}}
                }
            if suffix in ("stream", "thumbnail"):
                op["parameters"] = [
                    {
                        "name": "Range",
                        "in": "header",
                        "schema": {"type": "string", "example": "bytes=0-1023"},
                    }
                ]
                op["responses"]["206"] = {
                    "description": "Диапазон, Content-Range и Accept-Ranges: bytes"
                }
                op["responses"]["416"] = {
                    "description": "Диапазон вне файла, Content-Range: bytes */size"
                }
            methods[method] = op
        paths[f"/api/videos/{{video_id}}/{suffix}"] = methods
    paths["/api/videos"]["get"] = operation(
        "Публичная лента и поиск только по доступным public-видео",
        output="CatalogPage",
        authenticated=False,
    )
    paths["/api/videos"]["get"]["parameters"] = pagination + [
        {"name": "q", "in": "query", "schema": {"type": "string", "maxLength": 200}}
    ]
    paths["/api/channels/me"] = {"get": operation("Свой канал", output="ChannelView")}
    paths["/api/channels/{channel_id}"] = {
        "get": operation("Публичный профиль канала", output="ChannelView", authenticated=False),
        "patch": operation(
            "Редактировать свой канал", output="ChannelView", body="ChannelPatch", mutation=True
        ),
    }
    paths["/api/channels/{channel_id}/videos"] = {
        "get": operation(
            "Видео канала с учётом ACL посетителя", output="CatalogPage", authenticated=False
        )
    }
    paths["/api/channels/{channel_id}/videos"]["get"]["parameters"] = pagination
    paths["/api/channels/{channel_id}/subscription"] = {
        "put": operation(
            "Идемпотентная подписка, кроме собственного канала",
            output="SubscriptionState",
            mutation=True,
        ),
        "delete": operation(
            "Идемпотентная отмена подписки", output="SubscriptionState", mutation=True
        ),
    }
    paths["/api/subscriptions"] = {"get": operation("Свои подписки", output="ChannelList")}
    paths["/api/subscriptions"]["get"]["parameters"] = pagination
    paths["/api/videos/{video_id}/likes"] = {
        "get": operation("Лайки только доступного видео", output="LikeState", authenticated=False),
        "put": operation("Идемпотентно поставить лайк", output="LikeState", mutation=True),
        "delete": operation("Идемпотентно снять лайк", output="LikeState", mutation=True),
    }
    paths["/api/videos/{video_id}/comments"] = {
        "get": operation(
            "Комментарии доступного видео, новые сначала", output="CommentPage", authenticated=False
        ),
        "post": operation(
            "Добавить текстовый комментарий",
            output="CommentView",
            body="CommentCreate",
            mutation=True,
            status=201,
        ),
    }
    paths["/api/videos/{video_id}/comments"]["get"]["parameters"] = pagination
    paths["/api/videos/{video_id}/comments/{comment_id}"] = {
        "delete": operation(
            "Удалить свой комментарий к доступному видео", mutation=True, status=204
        )
    }
    paths["/api/playlists"] = {
        "get": operation("Свои плейлисты с доступными счётчиками", output="PlaylistList"),
        "post": operation(
            "Создать публичный плейлист",
            output="PlaylistView",
            body="PlaylistTitle",
            mutation=True,
            status=201,
        ),
    }
    paths["/api/playlists"]["get"]["parameters"] = pagination
    paths["/api/playlists/{playlist_id}"] = {
        "get": operation(
            "Публичный плейлист с ACL-счётчиком", output="PlaylistView", authenticated=False
        ),
        "patch": operation(
            "Переименовать свой плейлист",
            output="PlaylistView",
            body="PlaylistTitle",
            mutation=True,
        ),
        "delete": operation("Удалить свой плейлист, сохранив видео", mutation=True, status=204),
    }
    paths["/api/playlists/{playlist_id}/items"] = {
        "get": operation(
            "Доступные видео в порядке плейлиста", output="PlaylistVideos", authenticated=False
        )
    }
    paths["/api/playlists/{playlist_id}/items"]["get"]["parameters"] = pagination
    paths["/api/playlists/{playlist_id}/items/{video_id}"] = {
        "put": operation("Идемпотентно добавить доступное видео", mutation=True, status=204),
        "delete": operation(
            "Идемпотентно убрать видео из своего плейлиста", mutation=True, status=204
        ),
    }
    paths["/api/playlists/{playlist_id}/items/{video_id}/position"] = {
        "put": operation(
            "Переместить видео перед другим или в конец",
            body="PlaylistMove",
            mutation=True,
            status=204,
        )
    }
    paths["/api/channels/{channel_id}/playlists"] = {
        "get": operation(
            "Публичные плейлисты канала с ACL-счётчиками",
            output="PlaylistList",
            authenticated=False,
        )
    }
    paths["/api/channels/{channel_id}/playlists"]["get"]["parameters"] = pagination
    implemented = {
        (route.path, method.lower())
        for router in (
            video_router,
            upload_router,
            catalog_router,
            channel_router,
            interaction_router,
            playlist_router,
        )
        for route in router.routes
        for method in route.methods
    }
    for path, item in paths.items():
        for method, op in item.items():
            op["x-implemented"] = (path, method) in implemented
            op["tags"] = ["videos" if op["x-implemented"] else "planned-video"]
        params = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string", "format": "uuid"},
            }
            for name in ("video_id", "user_id", "channel_id", "comment_id", "playlist_id")
            if "{" + name + "}" in path
        ]
        if params:
            item["parameters"] = params
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Video API contracts — stage 8 public playlists",
            "version": "0.8.0",
            "description": (
                "x-implemented отмечает реализованные операции; действующий /docs содержит "
                "только рабочие маршруты. Файлы раздаёт Nginx после проверки доступа. "
                "Secure cookie называется __Host-video_session."
            ),
        },
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "SessionCookie": {"type": "apiKey", "in": "cookie", "name": "video_session"}
            },
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(build_contract(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            parser.exit(1, "Video contract is stale; run scripts/export_video_contract.py\n")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content)


if __name__ == "__main__":
    main()
