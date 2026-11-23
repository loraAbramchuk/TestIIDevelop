from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.auth_dependencies import CurrentUser, Db, VerifiedUser, optional_user
from app.auth_protection import check_csrf, rate_limit
from app.models import User
from app.video_contracts import (
    ContractError,
    GrantList,
    OwnedVideoList,
    VideoCreate,
    VideoGrant,
    VideoOwnerStatus,
    VideoPatch,
    VideoView,
)
from app.videos import service
from app.videos.deletion import delete_video
from app.videos.delivery import media_response

router = APIRouter(tags=["videos"])
ERRORS = {code: {"model": ContractError} for code in (400, 401, 403, 404, 409, 429, 503)}


def mutation_user(user: VerifiedUser, _csrf: Annotated[None, Depends(check_csrf)]) -> User:
    return user


Writer = Annotated[User, Depends(mutation_user)]
Viewer = Annotated[User | None, Depends(optional_user)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(max_length=1024)]


@router.post("/api/videos", response_model=VideoOwnerStatus, status_code=201, responses=ERRORS)
def create(payload: VideoCreate, request: Request, db: Db, user: Writer):
    rate_limit(
        db, f"videos:create:{user.id}", request.app.state.settings.video_create_limit_per_hour, 3600
    )
    return service.create_video(db, user.id, payload)


@router.get(
    "/api/videos/{video_id}",
    response_model=VideoView,
    responses=ERRORS,
    description=(
        "Метаданные только готового доступного видео. Файловые запросы проходят ту же ACL."
    ),
)
def view(video_id: UUID, db: Db, user: Viewer):
    return service.visible_video(db, video_id, user.id if user else None)


@router.get("/api/videos/{video_id}/status", response_model=VideoOwnerStatus, responses=ERRORS)
def status(video_id: UUID, db: Db, user: CurrentUser):
    return service.owner_output(service.owned_video(db, video_id, user.id))


@router.patch("/api/videos/{video_id}", response_model=VideoOwnerStatus, responses=ERRORS)
def patch(video_id: UUID, payload: VideoPatch, request: Request, db: Db, user: Writer):
    rate_limit(
        db, f"videos:write:{user.id}", request.app.state.settings.video_write_limit_per_minute, 60
    )
    return service.patch_video(db, video_id, user.id, payload)


@router.delete("/api/videos/{video_id}", status_code=204, responses=ERRORS)
def delete_video_route(video_id: UUID, request: Request, db: Db, user: Writer):
    rate_limit(
        db, f"videos:write:{user.id}", request.app.state.settings.video_write_limit_per_minute, 60
    )
    delete_video(db, request.app.state.settings, video_id, user.id)


def file_responses(mime, *, head=False):
    responses = ERRORS | {
        200: {
            "description": "Файл после ACL",
            "content": {mime: {"schema": {"type": "string", "format": "binary"}}},
        },
        206: {"description": "Диапазон байтов; Content-Range"},
        304: {"description": "Условный запрос после проверки ACL; без тела"},
        416: {"description": "Диапазон вне файла; Content-Range: bytes */size"},
        503: {"model": ContractError, "description": "Файл временно недоступен"},
    }
    if head:
        responses[200].pop("content")
    return responses


@router.get(
    "/api/videos/{video_id}/stream", response_class=Response, responses=file_responses("video/mp4")
)
@router.head(
    "/api/videos/{video_id}/stream",
    response_class=Response,
    responses=file_responses("video/mp4", head=True),
)
def stream(video_id: UUID, request: Request, db: Db, user: Viewer):
    """Каждый GET/HEAD, включая Range, проходит ACL; байты раздаёт Nginx, кэш запрещён."""
    return media_response(db, request.app.state.settings, video_id, user.id if user else None)


@router.get(
    "/api/videos/{video_id}/thumbnail",
    response_class=Response,
    responses=file_responses("image/jpeg"),
)
@router.head(
    "/api/videos/{video_id}/thumbnail",
    response_class=Response,
    responses=file_responses("image/jpeg", head=True),
)
def thumbnail(video_id: UUID, request: Request, db: Db, user: Viewer):
    return media_response(
        db, request.app.state.settings, video_id, user.id if user else None, thumbnail=True
    )


@router.get("/api/videos/{video_id}/grants", response_model=GrantList, responses=ERRORS)
def grants(
    video_id: UUID, db: Db, user: CurrentUser, limit: PageLimit = 20, cursor: PageCursor = None
):
    return service.list_grants(db, video_id, user.id, limit, cursor)


@router.put("/api/videos/{video_id}/grants/{user_id}", response_model=VideoGrant, responses=ERRORS)
def grant(video_id: UUID, user_id: UUID, request: Request, db: Db, user: Writer):
    rate_limit(
        db, f"videos:write:{user.id}", request.app.state.settings.video_write_limit_per_minute, 60
    )
    return service.grant_access(db, video_id, user.id, user_id)


@router.delete("/api/videos/{video_id}/grants/{user_id}", status_code=204, responses=ERRORS)
def revoke(video_id: UUID, user_id: UUID, request: Request, db: Db, user: Writer):
    rate_limit(
        db, f"videos:write:{user.id}", request.app.state.settings.video_write_limit_per_minute, 60
    )
    service.revoke_access(db, video_id, user.id, user_id)


@router.get("/api/studio/videos", response_model=OwnedVideoList, responses=ERRORS)
def mine(db: Db, user: CurrentUser, limit: PageLimit = 20, cursor: PageCursor = None):
    return service.list_owned(db, user.id, limit, cursor)
