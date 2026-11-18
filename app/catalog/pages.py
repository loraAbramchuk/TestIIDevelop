from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth_dependencies import Db
from app.catalog import service
from app.catalog.router import SearchQuery
from app.catalog.schemas import CatalogPage
from app.channels import service as channels
from app.errors import DomainError
from app.navigation import login_redirect
from app.templating import templates
from app.videos.pages import PageUser
from app.videos.router import PageCursor, PageLimit

router = APIRouter(include_in_schema=False)


def render(request, name, user, *, status=200, **context):
    return templates.TemplateResponse(
        request=request,
        name=name,
        status_code=status,
        context={"app_name": request.app.state.settings.app_name, "user": user, **context},
    )


def error_page(request, error, user=None):
    return render(request, "catalog_error.html", user, status=error.status, message=error.message)


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request, db: Db, user: PageUser, limit: PageLimit = 20, cursor: PageCursor = None
):
    try:
        page = service.list_videos(db, limit=limit, cursor=cursor)
        return render(request, "home.html", user, page=page, limit=limit)
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    db: Db,
    user: PageUser,
    q: SearchQuery = "",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    try:
        q = service.normalize_query(q)
        page = (
            service.list_videos(db, query=q, limit=limit, cursor=cursor)
            if q
            else CatalogPage(items=[])
        )
        return render(request, "search.html", user, page=page, search_query=q, limit=limit)
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/my-channel")
def mine(request: Request, db: Db, user: PageUser):
    if user is None:
        return login_redirect(request)
    try:
        channel = channels.my_channel(db, user.id)
        return RedirectResponse(f"/channels/{channel.id}", status_code=303)
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
def channel_page(
    channel_id: UUID,
    request: Request,
    db: Db,
    user: PageUser,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    try:
        viewer = user.id if user else None
        channel = channels.get_channel(db, channel_id)
        page = service.list_videos(
            db, channel=channel, viewer_id=viewer, limit=limit, cursor=cursor
        )
        return render(
            request,
            "channel.html",
            user,
            channel=channels.profile(db, channel_id, viewer),
            page=page,
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions(
    request: Request, db: Db, user: PageUser, limit: PageLimit = 20, cursor: PageCursor = None
):
    if user is None:
        return login_redirect(request)
    try:
        return render(
            request,
            "subscriptions.html",
            user,
            page=channels.list_subscriptions(db, user.id, limit, cursor),
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)
