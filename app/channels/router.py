from uuid import UUID

from fastapi import APIRouter, Request

from app.auth_dependencies import CurrentUser, Db
from app.auth_protection import rate_limit
from app.channels import service
from app.channels.schemas import ChannelList, ChannelPatch, ChannelView, SubscriptionState
from app.videos.router import ERRORS, PageCursor, PageLimit, Viewer, Writer

router = APIRouter(tags=["channels"])


def write_limit(db, request, user):
    rate_limit(
        db, f"channels:write:{user.id}", request.app.state.settings.video_write_limit_per_minute, 60
    )


@router.get("/api/channels/me", response_model=ChannelView, responses=ERRORS)
def mine(db: Db, user: CurrentUser):
    return service.profile(db, service.my_channel(db, user.id).id, user.id)


@router.get("/api/channels/{channel_id}", response_model=ChannelView, responses=ERRORS)
def view(channel_id: UUID, db: Db, user: Viewer):
    return service.profile(db, channel_id, user.id if user else None)


@router.patch("/api/channels/{channel_id}", response_model=ChannelView, responses=ERRORS)
def patch(channel_id: UUID, payload: ChannelPatch, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    return service.patch(db, channel_id, user.id, payload)


@router.put(
    "/api/channels/{channel_id}/subscription", response_model=SubscriptionState, responses=ERRORS
)
def subscribe(channel_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    return service.subscribe(db, channel_id, user.id, enabled=True)


@router.delete(
    "/api/channels/{channel_id}/subscription", response_model=SubscriptionState, responses=ERRORS
)
def unsubscribe(channel_id: UUID, request: Request, db: Db, user: Writer):
    write_limit(db, request, user)
    return service.subscribe(db, channel_id, user.id, enabled=False)


@router.get("/api/subscriptions", response_model=ChannelList, responses=ERRORS)
def subscriptions(db: Db, user: CurrentUser, limit: PageLimit = 20, cursor: PageCursor = None):
    return service.list_subscriptions(db, user.id, limit, cursor)
