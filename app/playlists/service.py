import base64
import binascii
import hashlib
import hmac
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, tuple_

from app.catalog.service import video_output
from app.channels.schemas import ChannelSummary
from app.errors import DomainError
from app.models import Channel, Playlist, PlaylistItem, Video
from app.pagination import decode_cursor, encode_cursor
from app.playlists.schemas import PlaylistList, PlaylistVideos, PlaylistView
from app.videos.access import viewable_by
from app.videos.service import visible_record


def get_playlist(db, identifier, *, owner_id=None, lock=None):
    query = select(Playlist).where(Playlist.id == identifier)
    if owner_id is not None:
        query = query.where(Playlist.owner_id == owner_id)
    if lock:
        query = query.with_for_update(read=lock == "read").execution_options(populate_existing=True)
    playlist = db.scalar(query)
    if playlist is None:
        raise DomainError(404, "playlist_not_found", "Плейлист не найден")
    return playlist


def visible_count(playlist_id, viewer_id):
    return (
        select(func.count())
        .select_from(PlaylistItem)
        .join(Video, Video.id == PlaylistItem.video_id)
        .where(PlaylistItem.playlist_id == playlist_id, viewable_by(viewer_id))
    )


def output(playlist, author, count, viewer_id):
    return PlaylistView(
        id=playlist.id,
        title=playlist.title,
        author=ChannelSummary.model_validate(author),
        visible_count=count,
        can_manage=playlist.owner_id == viewer_id,
        created_at=playlist.created_at,
        updated_at=playlist.updated_at,
    )


def profile(db, identifier, viewer_id):
    playlist = get_playlist(db, identifier)
    channel = db.scalar(select(Channel).where(Channel.owner_id == playlist.owner_id))
    return output(playlist, channel, db.scalar(visible_count(identifier, viewer_id)), viewer_id)


def list_playlists(db, owner_id, viewer_id, limit, cursor):
    scope = f"playlists:{owner_id}:{viewer_id or 'anonymous'}"
    position = decode_cursor(cursor, scope)
    count = visible_count(Playlist.id, viewer_id).correlate(Playlist).scalar_subquery()
    query = (
        select(Playlist, Channel, count)
        .join(Channel, Channel.owner_id == Playlist.owner_id)
        .where(Playlist.owner_id == owner_id)
    )
    if position:
        query = query.where(tuple_(Playlist.created_at, Playlist.id) < position)
    rows = db.execute(
        query.order_by(Playlist.created_at.desc(), Playlist.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    return PlaylistList(
        items=[output(p, c, n, viewer_id) for p, c, n in page],
        next_cursor=encode_cursor(scope, page[-1][0].created_at, page[-1][0].id)
        if len(rows) > limit
        else None,
    )


def create(db, user_id, payload):
    playlist = Playlist(owner_id=user_id, title=payload.title)
    db.add(playlist)
    db.commit()
    return profile(db, playlist.id, user_id)


def rename(db, identifier, user_id, payload):
    playlist = get_playlist(db, identifier, owner_id=user_id, lock="write")
    playlist.title = payload.title
    db.commit()
    return profile(db, identifier, user_id)


def delete_playlist(db, identifier, user_id):
    db.delete(get_playlist(db, identifier, owner_id=user_id, lock="write"))
    db.commit()


def revision_tag(playlist):
    return hashlib.sha256(playlist.revision.bytes).hexdigest()


def cursor_signature(playlist, payload):
    # The random per-playlist revision is secret; only its one-way tag is public.
    # It persists across app restarts and rotates on every membership/order change.
    data = b"playlist-items-v1:" + json.dumps(payload, separators=(",", ":")).encode()
    return hmac.new(playlist.revision.bytes, data, hashlib.sha256).hexdigest()


def item_cursor(playlist, viewer_id, last_video_id):
    payload = [
        str(playlist.id),
        str(viewer_id or "anonymous"),
        revision_tag(playlist),
        str(last_video_id),
    ]
    payload.insert(3, cursor_signature(playlist, payload))
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def cursor_anchor(cursor, playlist, viewer_id):
    try:
        if len(cursor) > 1024:
            raise ValueError
        parts = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(parts, list)
            or len(parts) != 5
            or not all(isinstance(p, str) for p in parts)
        ):
            raise ValueError
        if parts[:2] != [str(playlist.id), str(viewer_id or "anonymous")]:
            raise ValueError
        anchor = UUID(parts[4])
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise DomainError(400, "invalid_cursor", "Некорректный курсор страницы") from None
    if parts[2] != revision_tag(playlist):
        raise DomainError(409, "playlist_changed", "Порядок плейлиста изменился. Обновите список.")
    try:
        valid = hmac.compare_digest(
            parts[3].encode(), cursor_signature(playlist, parts[:3] + parts[4:]).encode()
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise DomainError(400, "invalid_cursor", "Некорректный курсор страницы")
    return anchor


def list_videos(db, identifier, viewer_id, limit, cursor):
    playlist = get_playlist(db, identifier, lock="read")
    query = (
        select(Video, Channel)
        .join(PlaylistItem, PlaylistItem.video_id == Video.id)
        .join(Channel, Channel.owner_id == Video.owner_id)
        .where(PlaylistItem.playlist_id == identifier, viewable_by(viewer_id))
    )
    if cursor:
        anchor = cursor_anchor(cursor, playlist, viewer_id)
        item = db.get(PlaylistItem, (identifier, anchor))
        if item is None:
            raise DomainError(400, "invalid_cursor", "Некорректный курсор страницы")
        query = query.where(PlaylistItem.position > item.position)
    rows = db.execute(query.order_by(PlaylistItem.position).limit(limit + 1)).all()
    page = rows[:limit]
    return PlaylistVideos(
        items=[video_output(v, c) for v, c in page],
        next_cursor=item_cursor(playlist, viewer_id, page[-1][0].id) if len(rows) > limit else None,
        visible_count=db.scalar(visible_count(identifier, viewer_id)),
    )


def lock_visible_videos(db, identifiers, user_id):
    # Take video locks in UUID order, then check ACL in new READ COMMITTED statements.
    for identifier in sorted(set(identifiers)):
        db.scalar(select(Video.id).where(Video.id == identifier).with_for_update())
    for identifier in identifiers:
        visible_record(db, identifier, user_id)


def items(db, identifier):
    return list(
        db.scalars(
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == identifier)
            .order_by(PlaylistItem.position)
        )
    )


def changed(playlist):
    playlist.revision = uuid4()


def reorder(db, rows):
    db.execute(text("SET CONSTRAINTS uq_playlist_items_position DEFERRED"))
    for position, row in enumerate(rows):
        row.position = position
    db.flush()


def add_video(db, identifier, video_id, user_id, maximum):
    playlist = get_playlist(db, identifier, owner_id=user_id, lock="write")
    lock_visible_videos(db, [video_id], user_id)
    if db.get(PlaylistItem, (identifier, video_id)) is None:
        rows = items(db, identifier)
        if len(rows) >= maximum:
            raise DomainError(409, "playlist_full", "Достигнут лимит видео в плейлисте")
        position = rows[-1].position + 1 if rows else 0
        db.add(PlaylistItem(playlist_id=identifier, video_id=video_id, position=position))
        changed(playlist)
    db.commit()


def remove_video(db, identifier, video_id, user_id):
    playlist = get_playlist(db, identifier, owner_id=user_id, lock="write")
    row = db.get(PlaylistItem, (identifier, video_id))
    if row is not None:
        db.delete(row)
        db.flush()
        reorder(db, items(db, identifier))
        changed(playlist)
    db.commit()


def move_video(db, identifier, video_id, user_id, payload):
    playlist = get_playlist(db, identifier, owner_id=user_id, lock="write")
    before = payload.before_video_id
    rows = items(db, identifier)
    by_id = {row.video_id: row for row in rows}
    if video_id not in by_id or (before is not None and before not in by_id):
        raise DomainError(404, "playlist_item_not_found", "Видео в плейлисте не найдено")
    if before == video_id:
        raise DomainError(409, "invalid_order", "Нельзя переместить видео перед самим собой")
    lock_visible_videos(db, [video_id] + ([before] if before else []), user_id)
    original = [row.video_id for row in rows]
    moving = by_id[video_id]
    rows.remove(moving)
    rows.insert(rows.index(by_id[before]) if before else len(rows), moving)
    if [row.video_id for row in rows] != original:
        reorder(db, rows)
        changed(playlist)
    db.commit()
