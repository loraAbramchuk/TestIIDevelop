from uuid import UUID

from fastapi import APIRouter, Request

from app.auth_dependencies import Db
from app.auth_protection import rate_limit
from app.interactions import service
from app.interactions.schemas import CommentCreate, CommentPage, CommentView, LikeState
from app.videos.router import ERRORS, PageCursor, PageLimit, Viewer, Writer

router = APIRouter(prefix="/api/videos/{video_id}", tags=["interactions"])


def write_limit(db, request, user, *, comment=False):
    settings = request.app.state.settings
    rate_limit(db, f"interactions:write:{user.id}", settings.interaction_write_limit_per_minute, 60)
    if comment:
        rate_limit(db, f"comments:create:{user.id}", settings.comment_create_limit_per_minute, 60)


@router.get("/likes", response_model=LikeState, responses=ERRORS)
def likes(video_id: UUID, db: Db, user: Viewer):
    return service.likes(db, video_id, user.id if user else None)


@router.put("/likes", response_model=LikeState, responses=ERRORS)
def like(video_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    return service.set_like(db, video_id, user.id, enabled=True)


@router.delete("/likes", response_model=LikeState, responses=ERRORS)
def unlike(video_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    return service.set_like(db, video_id, user.id, enabled=False)


@router.get("/comments", response_model=CommentPage, responses=ERRORS)
def comments(
    video_id: UUID, db: Db, user: Viewer, limit: PageLimit = 20, cursor: PageCursor = None
):
    return service.list_comments(db, video_id, user.id if user else None, limit, cursor)


@router.post("/comments", response_model=CommentView, status_code=201, responses=ERRORS)
def add_comment(video_id: UUID, payload: CommentCreate, request: Request, db: Db, user: Writer):
    write_limit(db, request, user, comment=True)
    return service.add_comment(db, video_id, user.id, payload)


@router.delete("/comments/{comment_id}", status_code=204, responses=ERRORS)
def delete_comment(video_id: UUID, comment_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    service.delete_comment(db, video_id, comment_id, user.id)
