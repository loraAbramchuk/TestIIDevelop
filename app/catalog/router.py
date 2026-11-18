from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.auth_dependencies import Db
from app.catalog import service
from app.catalog.schemas import CatalogPage
from app.channels.service import get_channel
from app.videos.router import ERRORS, PageCursor, PageLimit, Viewer

router = APIRouter(tags=["catalog"])
SearchQuery = Annotated[str, Query(max_length=200)]


@router.get("/api/videos", response_model=CatalogPage, responses=ERRORS)
def public_videos(db: Db, q: SearchQuery = "", limit: PageLimit = 20, cursor: PageCursor = None):
    return service.list_videos(db, query=q, limit=limit, cursor=cursor)


@router.get("/api/channels/{channel_id}/videos", response_model=CatalogPage, responses=ERRORS)
def channel_videos(
    channel_id: UUID, db: Db, user: Viewer, limit: PageLimit = 20, cursor: PageCursor = None
):
    return service.list_videos(
        db,
        viewer_id=user.id if user else None,
        channel=get_channel(db, channel_id),
        limit=limit,
        cursor=cursor,
    )
