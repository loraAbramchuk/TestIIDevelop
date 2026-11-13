"""Generate the planned OpenAPI document without adding routes to the application."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import video_contracts as models  # noqa: E402

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
        models.ContractError,
    ):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    paths = {
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
    paths["/api/videos/{video_id}/grants"]["get"]["parameters"] = [
        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
        {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
    ]
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
            if method == "get":
                op["responses"]["200"]["content"] = {
                    mime: {"schema": {"type": "string", "format": "binary"}}
                }
            if suffix == "stream":
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
    for path, item in paths.items():
        params = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string", "format": "uuid"},
            }
            for name in ("video_id", "user_id")
            if "{" + name + "}" in path
        ]
        if params:
            item["parameters"] = params
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Planned video API — NOT IMPLEMENTED",
            "version": "0.1.0",
            "description": (
                "Этап 1. Контракт будущих endpoints; действующий /docs содержит только "
                "реализованные маршруты. Cookie Secure меняет имя на __Host-video_session."
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
