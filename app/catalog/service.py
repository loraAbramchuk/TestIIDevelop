import hashlib

from sqlalchemy import select, tuple_

from app.catalog.schemas import CatalogPage, CatalogVideo
from app.channels.schemas import ChannelSummary
from app.errors import DomainError
from app.models import Channel, Video
from app.pagination import decode_cursor, encode_cursor
from app.videos.access import viewable_by


def normalize_query(value):
    if "\x00" in value:
        raise DomainError(400, "invalid_query", "Недопустимый символ в поисковом запросе")
    return value.strip()


def video_output(video, channel):
    return CatalogVideo(
        id=video.id,
        owner_id=video.owner_id,
        title=video.title,
        description=video.description,
        visibility=video.visibility,
        duration_seconds=video.duration_seconds,
        width=video.width,
        height=video.height,
        created_at=video.created_at,
        stream_url=f"/api/videos/{video.id}/stream",
        thumbnail_url=f"/api/videos/{video.id}/thumbnail",
        author=ChannelSummary.model_validate(channel),
    )


def list_videos(db, *, viewer_id=None, channel=None, query="", limit=20, cursor=None):
    query = normalize_query(query)
    if channel is None:
        # Public discovery never includes private videos, even for their owner/grantee.
        scope = "public:" + hashlib.sha256(query.encode()).hexdigest()
        conditions = [viewable_by(None), Video.visibility == "public"]
    else:
        scope = f"channel:{channel.id}:{viewer_id or 'anonymous'}"
        conditions = [viewable_by(viewer_id), Video.owner_id == channel.owner_id]
    position = decode_cursor(cursor, scope)
    statement = (
        select(Video, Channel).join(Channel, Channel.owner_id == Video.owner_id).where(*conditions)
    )
    if query:
        statement = statement.where(Video.title.icontains(query, autoescape=True))
    if position:
        statement = statement.where(tuple_(Video.created_at, Video.id) < position)
    rows = db.execute(
        statement.order_by(Video.created_at.desc(), Video.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = (
        encode_cursor(scope, page[-1][0].created_at, page[-1][0].id) if len(rows) > limit else None
    )
    return CatalogPage(
        items=[video_output(video, author) for video, author in page], next_cursor=next_cursor
    )
