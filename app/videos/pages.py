from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth_dependencies import Db, optional_user
from app.models import User
from app.templating import templates
from app.videos import service
from app.videos.router import PageCursor, PageLimit

router = APIRouter(include_in_schema=False)
PageUser = Annotated[User | None, Depends(optional_user)]


@router.get("/watch/{video_id}", response_class=HTMLResponse)
def watch_page(video_id: UUID, request: Request, db: Db, user: PageUser):
    try:
        video = service.visible_video(db, video_id, user.id if user else None)
    except service.VideoError as error:
        if error.status != 404:
            raise
        return templates.TemplateResponse(
            request=request,
            name="video_unavailable.html",
            status_code=404,
            context={"app_name": request.app.state.settings.app_name, "user": user},
        )
    return templates.TemplateResponse(
        request=request,
        name="watch.html",
        context={"app_name": request.app.state.settings.app_name, "user": user, "video": video},
    )


@router.get("/studio", response_class=HTMLResponse)
def studio(
    request: Request, db: Db, user: PageUser, limit: PageLimit = 20, cursor: PageCursor = None
):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    page = service.list_owned(db, user.id, limit, cursor)
    return templates.TemplateResponse(
        request=request,
        name="studio.html",
        context={"app_name": request.app.state.settings.app_name, "user": user, "page": page},
    )


@router.get("/studio/videos/{video_id}", response_class=HTMLResponse)
def edit_page(
    video_id: UUID,
    request: Request,
    db: Db,
    user: PageUser,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    video = service.owned_video(db, video_id, user.id)
    grants = service.list_grants(db, video_id, user.id, limit, cursor)
    return templates.TemplateResponse(
        request=request,
        name="video_edit.html",
        context={
            "app_name": request.app.state.settings.app_name,
            "user": user,
            "video": video,
            "grants": grants,
            "max_upload_bytes": request.app.state.settings.max_upload_bytes,
        },
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, user: PageUser):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context={
            "app_name": request.app.state.settings.app_name,
            "user": user,
            "max_upload_bytes": request.app.state.settings.max_upload_bytes,
        },
    )
