from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.channels.schemas import ChannelSummary
from app.errors import DomainError
from app.interactions.schemas import CommentPage, CommentView, LikeState
from app.models import Channel, Comment, Like, Video
from app.pagination import decode_cursor, encode_cursor
from app.videos.service import visible_record


def accessible_video(db, video_id, viewer_id, *, lock=False):
    if lock:
        # Access changes and deletion also lock this row. Check ACL in a NEW
        # statement after acquiring the lock so a waited-for revocation is visible.
        db.scalar(select(Video.id).where(Video.id == video_id).with_for_update())
    return visible_record(db, video_id, viewer_id)


def like_output(db, video_id, viewer_id):
    count = db.scalar(select(func.count()).select_from(Like).where(Like.video_id == video_id))
    liked = viewer_id is not None and db.get(Like, (video_id, viewer_id)) is not None
    return LikeState(video_id=video_id, like_count=count, is_liked=liked)


def likes(db, video_id, viewer_id):
    accessible_video(db, video_id, viewer_id)
    return like_output(db, video_id, viewer_id)


def set_like(db, video_id, user_id, *, enabled):
    accessible_video(db, video_id, user_id, lock=True)
    if enabled:
        db.execute(insert(Like).values(video_id=video_id, user_id=user_id).on_conflict_do_nothing())
    else:
        db.execute(delete(Like).where(Like.video_id == video_id, Like.user_id == user_id))
    result = like_output(db, video_id, user_id)
    db.commit()
    return result


def comment_output(comment, channel, viewer_id):
    return CommentView(
        id=comment.id,
        video_id=comment.video_id,
        text=comment.text,
        created_at=comment.created_at,
        author=ChannelSummary.model_validate(channel),
        can_delete=comment.user_id == viewer_id,
    )


def list_comments(db, video_id, viewer_id, limit, cursor):
    accessible_video(db, video_id, viewer_id)
    scope = f"comments:{video_id}:{viewer_id or 'anonymous'}"
    position = decode_cursor(cursor, scope)
    query = (
        select(Comment, Channel)
        .join(Channel, Channel.owner_id == Comment.user_id)
        .where(Comment.video_id == video_id)
    )
    if position:
        query = query.where(tuple_(Comment.created_at, Comment.id) < position)
    rows = db.execute(
        query.order_by(Comment.created_at.desc(), Comment.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = (
        encode_cursor(scope, page[-1][0].created_at, page[-1][0].id) if len(rows) > limit else None
    )
    count = db.scalar(select(func.count()).select_from(Comment).where(Comment.video_id == video_id))
    return CommentPage(
        items=[comment_output(comment, channel, viewer_id) for comment, channel in page],
        next_cursor=next_cursor,
        total_count=count,
    )


def add_comment(db, video_id, user_id, payload):
    accessible_video(db, video_id, user_id, lock=True)
    comment = Comment(video_id=video_id, user_id=user_id, text=payload.text)
    db.add(comment)
    db.flush()
    channel = db.scalar(select(Channel).where(Channel.owner_id == user_id))
    result = comment_output(comment, channel, user_id)
    db.commit()
    return result


def delete_comment(db, video_id, comment_id, user_id):
    accessible_video(db, video_id, user_id, lock=True)
    comment = db.scalar(
        select(Comment).where(
            Comment.id == comment_id, Comment.video_id == video_id, Comment.user_id == user_id
        )
    )
    if comment is None:
        raise DomainError(404, "comment_not_found", "Комментарий не найден")
    db.delete(comment)
    db.commit()
