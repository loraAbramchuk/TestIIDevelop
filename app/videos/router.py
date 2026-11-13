from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.auth_dependencies import CurrentUser, Db, optional_user
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

router = APIRouter(tags=["videos"])
ERRORS = {code: {"model": ContractError} for code in (400, 401, 403, 404, 409, 429)}


def mutation_user(user: CurrentUser, _csrf: Annotated[None, Depends(check_csrf)]) -> User:
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
        "Метаданные только готового доступного видео. "
        "Stream и thumbnail будут реализованы на этапе 5."
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
