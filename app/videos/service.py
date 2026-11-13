import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import User
from app.video_contracts import GrantList, VideoCreate, VideoOwnerStatus, VideoPatch, VideoView
from app.video_contracts import VideoGrant as GrantOutput
from app.videos.access import viewable_by
from app.videos.models import Video, VideoGrant


class VideoError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def not_found():
    return VideoError(404, "video_not_found", "Видео не найдено")


def owned_video(db: Session, video_id: UUID, owner_id: UUID, *, lock=False) -> Video:
    query = select(Video).where(
        Video.id == video_id, Video.owner_id == owner_id, Video.deleted_at.is_(None)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    video = db.scalar(query)
    if video is None:
        raise not_found()
    return video


def owner_output(video: Video) -> VideoOwnerStatus:
    return VideoOwnerStatus.model_validate(video, from_attributes=True)


def create_video(db: Session, owner_id: UUID, payload: VideoCreate) -> VideoOwnerStatus:
    video = Video(owner_id=owner_id, **payload.model_dump(mode="json"))
    db.add(video)
    db.commit()
    return owner_output(video)


def patch_video(
    db: Session, video_id: UUID, owner_id: UUID, payload: VideoPatch
) -> VideoOwnerStatus:
    video = owned_video(db, video_id, owner_id, lock=True)
    for name, value in payload.model_dump(mode="json", exclude_unset=True).items():
        setattr(video, name, value)
    db.commit()
    return owner_output(video)


def visible_video(db: Session, video_id: UUID, viewer_id: UUID | None) -> VideoView:
    video = db.scalar(select(Video).where(Video.id == video_id, viewable_by(viewer_id)))
    if video is None:
        raise not_found()
    return VideoView(
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
    )


def encode_cursor(scope: str, stamp: datetime, identifier: UUID) -> str:
    raw = json.dumps([scope, stamp.isoformat(), str(identifier)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None, scope: str):
    if value is None:
        return None
    try:
        if len(value) > 1024:
            raise ValueError
        parts = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(parts, list) or len(parts) != 3 or parts[0] != scope:
            raise ValueError
        if not all(isinstance(part, str) for part in parts):
            raise ValueError
        stamp, identifier = datetime.fromisoformat(parts[1]), UUID(parts[2])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp, identifier
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, OverflowError):
        raise VideoError(400, "invalid_cursor", "Некорректный курсор страницы") from None


def list_owned(db: Session, owner_id: UUID, limit: int, cursor: str | None):
    scope = f"owned:{owner_id}"
    position = decode_cursor(cursor, scope)
    query = select(Video).where(Video.owner_id == owner_id, Video.deleted_at.is_(None))
    if position:
        query = query.where(tuple_(Video.created_at, Video.id) < position)
    rows = list(
        db.scalars(query.order_by(Video.created_at.desc(), Video.id.desc()).limit(limit + 1))
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor(scope, page[-1].created_at, page[-1].id) if len(rows) > limit else None
    )
    return {"items": [owner_output(row) for row in page], "next_cursor": next_cursor}


def list_grants(
    db: Session, video_id: UUID, owner_id: UUID, limit: int, cursor: str | None
) -> GrantList:
    owned_video(db, video_id, owner_id)
    scope = f"grants:{video_id}"
    position = decode_cursor(cursor, scope)
    query = select(VideoGrant).where(VideoGrant.video_id == video_id)
    if position:
        query = query.where(tuple_(VideoGrant.created_at, VideoGrant.user_id) > position)
    rows = list(
        db.scalars(query.order_by(VideoGrant.created_at, VideoGrant.user_id).limit(limit + 1))
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor(scope, page[-1].created_at, page[-1].user_id) if len(rows) > limit else None
    )
    return GrantList(
        items=[GrantOutput.model_validate(row, from_attributes=True) for row in page],
        next_cursor=next_cursor,
    )


def grant_access(db: Session, video_id: UUID, owner_id: UUID, user_id: UUID) -> GrantOutput:
    # All mutations of one video's metadata/ACL take the same short row lock.
    owned_video(db, video_id, owner_id, lock=True)
    if user_id == owner_id:
        raise VideoError(
            409, "owner_grant_redundant", "Владельцу не требуется отдельное разрешение"
        )
    # Keep the recipient alive until the foreign-key write commits.
    recipient = db.scalar(
        select(User.id).where(User.id == user_id).with_for_update(read=True, key_share=True)
    )
    if recipient is None:
        raise VideoError(404, "recipient_not_found", "Пользователь не найден")
    inserted = db.scalar(
        insert(VideoGrant)
        .values(
            video_id=video_id,
            user_id=user_id,
            granted_by=owner_id,
        )
        .on_conflict_do_nothing(index_elements=[VideoGrant.video_id, VideoGrant.user_id])
        .returning(VideoGrant)
    )
    grant = inserted or db.get(VideoGrant, (video_id, user_id))
    result = GrantOutput.model_validate(grant, from_attributes=True)
    db.commit()
    return result


def revoke_access(db: Session, video_id: UUID, owner_id: UUID, user_id: UUID) -> None:
    owned_video(db, video_id, owner_id, lock=True)
    db.execute(
        delete(VideoGrant).where(VideoGrant.video_id == video_id, VideoGrant.user_id == user_id)
    )
    db.commit()
