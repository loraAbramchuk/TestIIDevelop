from uuid import UUID

from fastapi import APIRouter, Request

from app.auth_dependencies import CurrentUser, Db
from app.auth_protection import rate_limit
from app.channels.service import get_channel
from app.playlists import service
from app.playlists.schemas import (
    PlaylistList,
    PlaylistMove,
    PlaylistTitle,
    PlaylistVideos,
    PlaylistView,
)
from app.videos.router import ERRORS, PageCursor, PageLimit, Viewer, Writer

router = APIRouter(tags=["playlists"])


def write_limit(db, request, user):
    rate_limit(
        db,
        f"playlists:write:{user.id}",
        request.app.state.settings.playlist_write_limit_per_minute,
        60,
    )


@router.get("/api/playlists", response_model=PlaylistList, responses=ERRORS)
def mine(db: Db, user: CurrentUser, limit: PageLimit = 20, cursor: PageCursor = None):
    return service.list_playlists(db, user.id, user.id, limit, cursor)


@router.post("/api/playlists", response_model=PlaylistView, status_code=201, responses=ERRORS)
def create(payload: PlaylistTitle, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    rate_limit(
        db,
        f"playlists:create:{user.id}",
        request.app.state.settings.playlist_create_limit_per_hour,
        3600,
    )
    return service.create(db, user.id, payload)


@router.get("/api/playlists/{playlist_id}", response_model=PlaylistView, responses=ERRORS)
def profile(playlist_id: UUID, db: Db, user: Viewer):
    return service.profile(db, playlist_id, user.id if user else None)


@router.patch("/api/playlists/{playlist_id}", response_model=PlaylistView, responses=ERRORS)
def rename(playlist_id: UUID, payload: PlaylistTitle, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    return service.rename(db, playlist_id, user.id, payload)


@router.delete("/api/playlists/{playlist_id}", status_code=204, responses=ERRORS)
def delete(playlist_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    service.delete_playlist(db, playlist_id, user.id)


@router.get("/api/playlists/{playlist_id}/items", response_model=PlaylistVideos, responses=ERRORS)
def videos(
    playlist_id: UUID, db: Db, user: Viewer, limit: PageLimit = 20, cursor: PageCursor = None
):
    return service.list_videos(db, playlist_id, user.id if user else None, limit, cursor)


@router.put("/api/playlists/{playlist_id}/items/{video_id}", status_code=204, responses=ERRORS)
def add(playlist_id: UUID, video_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    service.add_video(
        db, playlist_id, video_id, user.id, request.app.state.settings.playlist_max_items
    )


@router.delete("/api/playlists/{playlist_id}/items/{video_id}", status_code=204, responses=ERRORS)
def remove(playlist_id: UUID, video_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    service.remove_video(db, playlist_id, video_id, user.id)


@router.put(
    "/api/playlists/{playlist_id}/items/{video_id}/position", status_code=204, responses=ERRORS
)
def move(
    playlist_id: UUID, video_id: UUID, payload: PlaylistMove, request: Request, db: Db, user: Writer
):
    write_limit(db, request, user)
    service.move_video(db, playlist_id, video_id, user.id, payload)


@router.get("/api/channels/{channel_id}/playlists", response_model=PlaylistList, responses=ERRORS)
def channel_lists(
    channel_id: UUID, db: Db, user: Viewer, limit: PageLimit = 20, cursor: PageCursor = None
):
    channel = get_channel(db, channel_id)
    return service.list_playlists(db, channel.owner_id, user.id if user else None, limit, cursor)
