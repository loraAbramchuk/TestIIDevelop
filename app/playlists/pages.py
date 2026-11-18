from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth_dependencies import Db
from app.catalog.pages import error_page, render
from app.errors import DomainError
from app.navigation import login_redirect
from app.playlists import service
from app.videos.pages import PageUser
from app.videos.router import PageCursor, PageLimit

router = APIRouter(include_in_schema=False)


@router.get("/my-playlists", response_class=HTMLResponse)
def mine(
    request: Request, db: Db, user: PageUser, limit: PageLimit = 20, cursor: PageCursor = None
):
    if user is None:
        return login_redirect(request)
    try:
        page = service.list_playlists(db, user.id, user.id, limit, cursor)
        return render(request, "my_playlists.html", user, page=page, limit=limit)
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/playlists/{playlist_id}", response_class=HTMLResponse)
def playlist(
    playlist_id: UUID,
    request: Request,
    db: Db,
    user: PageUser,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    try:
        viewer = user.id if user else None
        page = service.list_videos(db, playlist_id, viewer, limit, cursor)
        return render(
            request,
            "playlist.html",
            user,
            playlist=service.profile(db, playlist_id, viewer),
            page=page,
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)
